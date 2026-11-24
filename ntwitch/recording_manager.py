"""Recording manager for capturing Twitch streams."""
import subprocess
import threading
import logging
from pathlib import Path
from datetime import datetime
from typing import Dict, Optional
from dataclasses import dataclass

from . import tools


logger = logging.getLogger(__name__)


@dataclass
class Recording:
    """Represents an active recording."""
    channel: str
    process: subprocess.Popen
    output_file: Path
    start_time: datetime


class RecordingManager:
    """Manages stream recordings via Streamlink."""
    
    def __init__(self, quality: str = "best", recording_path: Optional[str] = None):
        """Initialize recording manager."""
        self.quality = quality
        self.recordings: Dict[str, Recording] = {}
        self.base_path = self._get_recordings_path(recording_path)
        
        # Create base recordings directory
        self.base_path.mkdir(parents=True, exist_ok=True)
        logger.info(f"Recording path: {self.base_path}")
    
    def _get_recordings_path(self, custom_path: Optional[str] = None) -> Path:
        """Get the recordings directory path."""
        if custom_path:
            return Path(custom_path)
        # Default: Videos folder
        videos_folder = Path.home() / "Videos"
        recordings_folder = videos_folder / "TwitchRecordings"
        return recordings_folder
    
    def is_recording(self, channel: str) -> bool:
        """Check if a channel is currently being recorded."""
        return channel in self.recordings
    
    def start_recording(self, channel: str) -> bool:
        """Start recording a channel."""
        if self.is_recording(channel):
            logger.warning(f"Already recording {channel}")
            return False
        
        try:
            # Create channel-specific folder
            channel_folder = self.base_path / channel
            channel_folder.mkdir(parents=True, exist_ok=True)
            
            # Generate filename with timestamp
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"{channel}_{timestamp}.ts"  # remuxed to mp4 when you stop
            output_file = channel_folder / filename
            
            # Build streamlink command for recording
            cmd = tools.streamlink_cmd() + [
                "--force",  # Overwrite if file exists
                "--loglevel=none",
                f"twitch.tv/{channel}",
                self.quality,
                "-o", str(output_file)
            ]
            
            # Start recording process (output discarded: an undrained pipe would stall streamlink)
            process = subprocess.Popen(cmd, **tools.popen_kwargs())
            
            # Store recording info
            self.recordings[channel] = Recording(
                channel=channel,
                process=process,
                output_file=output_file,
                start_time=datetime.now()
            )
            
            logger.info(f"Started recording {channel} to {output_file}")
            return True
            
        except Exception as e:
            logger.error(f"Error starting recording for {channel}: {e}")
            return False
    
    def stop_recording(self, channel: str) -> Optional[Path]:
        """Stop recording a channel. Returns path to recorded file."""
        if not self.is_recording(channel):
            logger.warning(f"Not recording {channel}")
            return None
        
        try:
            recording = self.recordings[channel]
            
            tools.kill_tree(recording.process, timeout=5)
            
            # Remove from active recordings
            del self.recordings[channel]
            
            # Calculate duration
            duration = datetime.now() - recording.start_time
            minutes = int(duration.total_seconds() / 60)
            
            logger.info(f"Stopped recording {channel} after {minutes} minutes")
            return self._finalize(recording.output_file)
            
        except Exception as e:
            logger.error(f"Error stopping recording for {channel}: {e}")
            return None
    
    def _finalize(self, ts_file: Path) -> Path:
        """Convert the raw stream to mp4 in the background (needs ffmpeg). Returns the path the user should look for."""
        ffmpeg = tools.find_ffmpeg()
        if not ffmpeg or not ts_file.exists():
            return ts_file
        mp4 = ts_file.with_suffix(".mp4")

        def remux() -> None:
            part = mp4.with_suffix(".mp4.part")
            try:
                result = subprocess.run([ffmpeg, "-y", "-i", str(ts_file), "-c", "copy", "-f", "mp4", str(part)],
                                        **tools.popen_kwargs())
                if result.returncode == 0 and part.exists():
                    part.replace(mp4)
                    ts_file.unlink(missing_ok=True)
                else:
                    part.unlink(missing_ok=True)  # keep the .ts, it is still playable
            except Exception as e:
                logger.error("remux failed: %s", e)

        threading.Thread(target=remux, daemon=True).start()
        return mp4

    def toggle_recording(self, channel: str) -> tuple[bool, str]:
        """
        Toggle recording for a channel.
        
        Returns: (is_now_recording, message)
        """
        if self.is_recording(channel):
            output_file = self.stop_recording(channel)
            if output_file:
                return False, f"Recording stopped: {output_file.name}"
            else:
                return False, "Failed to stop recording"
        else:
            success = self.start_recording(channel)
            if success:
                return True, f"Recording started"
            else:
                return False, "Failed to start recording"
    
    def get_recording_info(self, channel: str) -> Optional[str]:
        """Get recording duration info for a channel."""
        if not self.is_recording(channel):
            return None
        
        recording = self.recordings[channel]
        duration = datetime.now() - recording.start_time
        minutes = int(duration.total_seconds() / 60)
        seconds = int(duration.total_seconds() % 60)
        
        return f"{minutes:02d}:{seconds:02d}"
    
    def get_all_recording_status(self) -> Dict[str, str]:
        """Get status of all recordings."""
        status = {}
        for channel in self.recordings:
            duration_str = self.get_recording_info(channel)
            status[channel] = duration_str or "00:00"
        return status
    
    def stop_all_recordings(self):
        """Stop all active recordings."""
        channels = list(self.recordings.keys())
        for channel in channels:
            self.stop_recording(channel)
        logger.info("All recordings stopped")
    
    def cleanup(self):
        """Clean up all recordings on shutdown."""
        self.stop_all_recordings()
