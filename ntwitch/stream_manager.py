import json
import logging
import re
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from enum import Enum
from typing import Dict, List, Optional, Tuple

from . import tools

logger = logging.getLogger(__name__)

MAX_CHANNELS = 4  # panes the UI can show


class StreamStatus(Enum):
    """Stream status enum."""
    LIVE = "live"
    OFFLINE = "offline"
    NOT_FOUND = "not_found"
    ERROR = "error"


@dataclass
class StreamProcess:
    """Represents a running stream process."""
    channel: str
    process: subprocess.Popen
    pid: int


class StreamManager:
    """Manages multiple VLC stream processes."""

    STREAM_LAUNCH_DELAY = 1  # seconds between opening VLC windows

    def __init__(self, vlc_path: str, quality: str = "best", auto_restart: bool = True):
        """Initialize stream manager."""
        self.vlc_path = vlc_path
        self.quality = quality
        self.streams: List[StreamProcess] = []
        self.auto_restart = auto_restart
        self._restart_count: Dict[str, int] = {}
        self._max_restarts_per_channel = 3

    # ------------------------------------------------------------------ status checks

    def check_stream_status(self, channel: str) -> Tuple[StreamStatus, str]:
        """Check whether a channel is live. Returns (StreamStatus, short message).

        Streamlink reports a nonexistent channel exactly like an offline one, so those
        two cases cannot be told apart and share one friendly message.
        """
        try:
            result = subprocess.run(
                tools.streamlink_cmd() + ["--json", f"twitch.tv/{channel}"],
                capture_output=True, text=True, timeout=30,
                stdin=subprocess.DEVNULL,
                **({"creationflags": tools.NO_WINDOW} if tools.IS_WIN else {}),
            )
        except subprocess.TimeoutExpired:
            return StreamStatus.ERROR, "Timed out reaching Twitch"
        except FileNotFoundError:
            return StreamStatus.ERROR, "Streamlink is not installed"
        except Exception as e:
            return StreamStatus.ERROR, f"Check failed: {e}"

        if result.returncode == 0:
            try:
                data = json.loads(result.stdout)
            except json.JSONDecodeError:
                return StreamStatus.ERROR, "Unexpected reply from Streamlink"
            if data.get("streams"):
                return StreamStatus.LIVE, "Live"
            return StreamStatus.OFFLINE, "Not live"

        text = (result.stderr + result.stdout).lower()
        if any(s in text for s in ("unable to open url", "connection", "name or service", "getaddrinfo",
                                   "timed out", "max retries")):
            return StreamStatus.ERROR, "Can't reach Twitch - check your internet connection"
        if "no plugin can handle" in text:
            return StreamStatus.NOT_FOUND, "Not a valid channel name"
        return StreamStatus.OFFLINE, "Offline, or the channel doesn't exist"

    def check_many(self, channels: List[str]) -> Dict[str, Tuple[StreamStatus, str]]:
        """Check several channels at once."""
        if not channels:
            return {}
        with ThreadPoolExecutor(max_workers=min(len(channels), 8)) as pool:
            results = list(pool.map(self.check_stream_status, channels))
        return dict(zip(channels, results))

    # ------------------------------------------------------------------ launching

    def launch_streams(self, channels: List[str], prechecked: bool = False) -> Dict[str, Tuple[bool, str]]:
        """Open a VLC window per live channel. Returns {channel: (success, message)}.

        With `prechecked=True` the channels are already known to be live, so the status check is skipped.
        """
        if prechecked:
            statuses = {c: (StreamStatus.LIVE, "Live") for c in channels}
        else:
            statuses = self.check_many(channels)

        results: Dict[str, Tuple[bool, str]] = {}
        launched = 0
        for channel in channels:
            status, message = statuses[channel]
            if status != StreamStatus.LIVE:
                results[channel] = (False, message)
                continue
            if launched:
                time.sleep(self.STREAM_LAUNCH_DELAY)
            process = self._launch_single_stream(channel)
            if process:
                self.streams.append(StreamProcess(channel=channel, process=process, pid=process.pid))
                results[channel] = (True, "Opened")
                launched += 1
            else:
                results[channel] = (False, "Could not open the video player")
        return results

    def _launch_single_stream(self, channel: str) -> Optional[subprocess.Popen]:
        """Launch one VLC window fed by Streamlink."""
        vlc_args = [
            "--no-video-title-show",
            f"--video-title={channel}",
            "--network-caching=1500",      # 1.5 s buffer: raise for stability, lower for latency
            "--no-audio-time-stretch",
            "--no-drop-late-frames",
            "--no-skip-frames",
            "--no-sub-autodetect-file",
        ]
        cmd = tools.streamlink_cmd() + [
            f"--player={self.vlc_path}",
            f"--player-args={' '.join(vlc_args)}",
            "--loglevel=none",
            "--stream-timeout=120",
            "--retry-streams=10",
            "--retry-max=20",
            "--retry-open=5",
            "--ringbuffer-size=32M",
            f"twitch.tv/{channel}",
            self.quality,
        ]
        try:
            process = subprocess.Popen(cmd, **tools.popen_kwargs())
            time.sleep(0.5)  # give it a moment to fail if it is going to
            if process.poll() is not None:
                return None
            return process
        except Exception as e:
            logger.error("Error launching stream for %s: %s", channel, e)
            return None

    # ------------------------------------------------------------------ monitoring

    def check_streams(self) -> List[Tuple[str, str]]:
        """Look for streams that ended. Blocking: call from a worker thread.

        Returns (channel, reason) pairs; reason is "closed" (user closed the player),
        "offline", "connection_error" or "max_retries".
        """
        dead: List[Tuple[str, str]] = []
        for stream in list(self.streams):
            code = stream.process.poll()
            if code is None:
                continue
            self.streams.remove(stream)

            if code == 0:
                # Streamlink exits cleanly when the player window is closed: respect that.
                dead.append((stream.channel, "closed"))
                continue

            status, _ = self.check_stream_status(stream.channel)
            if status in (StreamStatus.OFFLINE, StreamStatus.NOT_FOUND):
                dead.append((stream.channel, "offline"))
            elif self.auto_restart and self._restart_count.get(stream.channel, 0) < self._max_restarts_per_channel:
                if not self._restart_stream(stream.channel):
                    dead.append((stream.channel, "connection_error"))
            elif self.auto_restart:
                dead.append((stream.channel, "max_retries"))
            else:
                dead.append((stream.channel, "connection_error"))
        return dead

    def _restart_stream(self, channel: str) -> bool:
        self._restart_count[channel] = self._restart_count.get(channel, 0) + 1
        process = self._launch_single_stream(channel)
        if not process:
            return False
        self.streams.append(StreamProcess(channel=channel, process=process, pid=process.pid))
        return True

    def cleanup(self):
        """Close every VLC window and helper process."""
        for stream in self.streams:
            tools.kill_tree(stream.process)
        self.streams.clear()

    def get_running_channels(self) -> List[str]:
        """Get list of currently running channel names."""
        return [s.channel for s in self.streams]


_VALID_NAME = re.compile(r"^[a-z0-9_]{1,25}$")


def parse_channel_input(input_str: str) -> List[str]:
    """Turn whatever the user typed into a clean, de-duplicated list of channel names.

    Accepts commas and/or spaces as separators, '@'/'#' prefixes and full twitch.tv URLs.
    """
    channels: List[str] = []
    for part in re.split(r"[,\s]+", input_str.strip()):
        channel = part.lower()
        channel = re.sub(r"^https?://", "", channel)
        channel = re.sub(r"^(www\.|m\.)?twitch\.tv/", "", channel)
        channel = channel.split("/")[0].split("?")[0].lstrip("@#")
        if channel and _VALID_NAME.match(channel) and channel not in channels:
            channels.append(channel)
    return channels
