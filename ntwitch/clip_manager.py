"""Rolling buffer for instant clips.

Streamlink's output for the selected channel is drained by a background thread into
short segment files. Old segments are deleted, so disk use stays bounded to roughly
`buffer_minutes` of video. Saving a clip glues the buffered segments together and
(when ffmpeg is available) remuxes them to mp4.
"""
import logging
import shutil
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import BinaryIO, Callable, Deque, Optional, Tuple

from . import tools

logger = logging.getLogger(__name__)

TS_PACKET = 188
MIN_CLIP_SECONDS = 10


@dataclass
class Segment:
    path: Path
    start: float          # wall-clock time the segment began
    offset: int           # (bytes written before this segment) % 188, to realign the first packet
    size: int = 0


class SegmentWriter(threading.Thread):
    """Reads a byte stream and writes it as rolling, time-sliced segment files."""

    def __init__(self, stream: BinaryIO, directory: Path, keep_seconds: float,
                 segment_seconds: float = 20.0, clock: Callable[[], float] = time.time):
        super().__init__(daemon=True)
        self.stream = stream
        self.directory = directory
        self.keep_seconds = keep_seconds
        self.segment_seconds = segment_seconds
        self.clock = clock
        self.segments: Deque[Segment] = deque()
        self.lock = threading.Lock()
        self.total = 0
        self.ended = False
        self._current = None

    def run(self) -> None:
        read = self.stream.read1 if hasattr(self.stream, "read1") else self.stream.read
        try:
            while True:
                data = read(TS_PACKET * 512)
                if not data:
                    break
                self._write(data)
        except (OSError, ValueError) as e:
            logger.debug("segment reader stopped: %s", e)
        finally:
            self.ended = True
            with self.lock:
                self._close_current()

    def _write(self, data: bytes) -> None:
        now = self.clock()
        with self.lock:
            if self._current is None or now - self.segments[-1].start >= self.segment_seconds:
                self._close_current()
                path = self.directory / f"seg_{self.total:012d}.ts"
                self._current = open(path, "wb")
                self.segments.append(Segment(path=path, start=now, offset=self.total % TS_PACKET))
                self._prune(now)
            self._current.write(data)
            self._current.flush()
            self.segments[-1].size += len(data)
            self.total += len(data)

    def _close_current(self) -> None:
        if self._current is not None:
            try:
                self._current.close()
            except OSError:
                pass
            self._current = None

    def _prune(self, now: float) -> None:
        """Drop segments that lie entirely outside the buffer window (lock must be held)."""
        while len(self.segments) > 1 and self.segments[1].start <= now - self.keep_seconds:
            old = self.segments.popleft()
            try:
                old.path.unlink()
            except OSError:
                pass

    def buffered_seconds(self) -> float:
        with self.lock:
            if not self.segments:
                return 0.0
            return min(self.keep_seconds, self.clock() - self.segments[0].start)

    def concatenate(self, destination: Path) -> int:
        """Write all buffered segments, in order, into one transport-stream file. Returns its size."""
        with self.lock:
            with open(destination, "wb") as out:
                for index, seg in enumerate(self.segments):
                    try:
                        with open(seg.path, "rb") as f:
                            if index == 0 and seg.offset:
                                f.seek((TS_PACKET - seg.offset) % TS_PACKET)  # start on a packet boundary
                            shutil.copyfileobj(f, out)
                    except OSError as e:
                        logger.debug("skipping segment %s: %s", seg.path, e)
        return destination.stat().st_size

    def discard(self) -> None:
        with self.lock:
            self._close_current()
            for seg in self.segments:
                try:
                    seg.path.unlink()
                except OSError:
                    pass
            self.segments.clear()


@dataclass
class ClipBuffer:
    """An active buffer for one channel."""
    channel: str
    process: subprocess.Popen
    writer: SegmentWriter
    start_time: float = field(default_factory=time.time)


class ClipManager:
    """Keeps a rolling buffer for one channel at a time and saves clips from it."""

    def __init__(self, quality: str = "best", buffer_minutes: int = 10, clips_path: Optional[str] = None):
        self.quality = quality
        self.buffer_minutes = max(1, buffer_minutes)
        self.active_buffer: Optional[ClipBuffer] = None
        self.clips_path = Path(clips_path) if clips_path else Path.home() / "Videos" / "TwitchClips"
        self.temp_path = self.clips_path / ".temp"

        self.clips_path.mkdir(parents=True, exist_ok=True)
        self.temp_path.mkdir(parents=True, exist_ok=True)
        self._cleanup_temp_files()

    # ------------------------------------------------------------------ housekeeping

    def _cleanup_temp_files(self) -> None:
        """Remove leftovers from a previous run."""
        try:
            for item in self.temp_path.iterdir():
                if item.is_dir():
                    shutil.rmtree(item, ignore_errors=True)
                else:
                    item.unlink(missing_ok=True)
        except OSError as e:
            logger.debug("temp cleanup failed: %s", e)

    def is_buffering(self, channel: str) -> bool:
        return self.active_buffer is not None and self.active_buffer.channel == channel

    def get_active_buffer_channel(self) -> Optional[str]:
        return self.active_buffer.channel if self.active_buffer else None

    # ------------------------------------------------------------------ buffering

    def start_buffer(self, channel: str) -> bool:
        """Start buffering `channel` (any previous buffer is discarded)."""
        if self.is_buffering(channel):
            return True
        self._stop_buffer_process()
        try:
            directory = self.temp_path / f"{channel}_{datetime.now():%Y%m%d_%H%M%S}"
            directory.mkdir(parents=True, exist_ok=True)

            cmd = tools.streamlink_cmd() + [
                "--loglevel=none", "--stdout", f"twitch.tv/{channel}", self.quality,
            ]
            kwargs = tools.popen_kwargs()
            kwargs["stdout"] = subprocess.PIPE
            process = subprocess.Popen(cmd, **kwargs)

            writer = SegmentWriter(process.stdout, directory, keep_seconds=self.buffer_minutes * 60)
            writer.start()
            self.active_buffer = ClipBuffer(channel=channel, process=process, writer=writer)
            return True
        except Exception as e:
            logger.error("Could not start buffer for %s: %s", channel, e)
            return False

    def _stop_buffer_process(self) -> None:
        buf = self.active_buffer
        if not buf:
            return
        self.active_buffer = None
        tools.kill_tree(buf.process)
        buf.writer.join(timeout=3)
        buf.writer.discard()
        shutil.rmtree(buf.writer.directory, ignore_errors=True)

    def stop_buffer(self, channel: str) -> bool:
        if not self.is_buffering(channel):
            return False
        self._stop_buffer_process()
        return True

    def get_buffer_status(self, channel: str) -> Optional[str]:
        """Short text like '02:15/10' for the status bar."""
        if not self.is_buffering(channel):
            return None
        seconds = int(self.active_buffer.writer.buffered_seconds())
        if seconds >= self.buffer_minutes * 60:
            return f"{self.buffer_minutes}:00 (full)"
        return f"{seconds // 60:02d}:{seconds % 60:02d}/{self.buffer_minutes}"

    # ------------------------------------------------------------------ saving

    def save_clip(self, channel: str) -> Tuple[bool, str, Optional[Path]]:
        """Save what is buffered (up to the last `buffer_minutes`). Blocking: run in a worker thread."""
        if not self.is_buffering(channel) or not self.active_buffer:
            return False, f"Not buffering #{channel} yet.", None

        writer = self.active_buffer.writer
        seconds = writer.buffered_seconds()
        if seconds < MIN_CLIP_SECONDS:
            return False, f"Only {int(seconds)}s buffered. Wait at least {MIN_CLIP_SECONDS}s and try again.", None

        raw: Optional[Path] = None
        try:
            channel_folder = self.clips_path / channel
            channel_folder.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            minutes = max(1, round(seconds / 60))
            raw = self.temp_path / f"{channel}_{stamp}.ts"
            if writer.concatenate(raw) < 100_000:
                return False, "Nothing recorded yet. Is the stream running?", None

            final: Optional[Path] = None
            ffmpeg = tools.find_ffmpeg()
            if ffmpeg:
                candidate = channel_folder / f"{channel}_clip_{stamp}_{minutes}min.mp4"
                result = subprocess.run(
                    [ffmpeg, "-y", "-i", str(raw), "-c", "copy", str(candidate)],
                    timeout=300, **tools.popen_kwargs(),
                )
                if result.returncode == 0 and candidate.exists():
                    final = candidate
                else:
                    candidate.unlink(missing_ok=True)
            if final is None:  # no ffmpeg (or it failed): keep the raw stream under an honest name
                final = channel_folder / f"{channel}_clip_{stamp}_{minutes}min.ts"
                shutil.move(str(raw), final)
            return True, f"Clip saved: {final.name} ({final.stat().st_size / 1_048_576:.0f} MB)", final
        except Exception as e:
            logger.error("Error saving clip: %s", e)
            return False, f"Could not save the clip: {e}", None
        finally:
            if raw is not None:
                raw.unlink(missing_ok=True)

    def cleanup(self) -> None:
        self._stop_buffer_process()
        self._cleanup_temp_files()
