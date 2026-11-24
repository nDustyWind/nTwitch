"""Configuration management for nTwitch."""
import json
import os
from pathlib import Path
from typing import Any, Dict, Optional

from . import credentials


class ConfigManager:
    """Reads and writes the JSON config file (default: ~/.twitch-terminal-config.json).

    The chat token lives in the OS credential store when one is available; the file then
    only records that fact. Callers still just see `config["twitch_oauth"]`.
    """

    def __init__(self, config_path: Optional[str] = None, use_keyring: bool = True):
        if config_path is None:
            self.config_path = Path.home() / ".twitch-terminal-config.json"
        else:
            self.config_path = Path(config_path)
        self.use_keyring = use_keyring

    # ------------------------------------------------------------------ files

    def _read_raw(self) -> Dict[str, Any]:
        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def load(self) -> Dict[str, Any]:
        """Load the config. A missing or unreadable file yields {} (which triggers first-run setup)."""
        data = self._read_raw()
        username = data.get("twitch_username", "")
        if self.use_keyring and username and not data.get("twitch_oauth"):
            token = credentials.get_token(username)
            if token:
                data["twitch_oauth"] = token
        return data

    def save(self, config: Dict[str, Any]) -> bool:
        """Save atomically so a crash can never leave a half-written file."""
        out = dict(config)
        username = out.get("twitch_username", "")
        token = out.get("twitch_oauth", "")

        if self.use_keyring:
            previous = self._read_raw().get("twitch_username", "")
            if previous and (previous != username or not token):
                credentials.delete_token(previous)  # logged out or switched account
            if username and token and credentials.set_token(username, token):
                out["twitch_oauth"] = ""            # the secret now lives in the credential store
                out["token_in_credential_store"] = True
            else:
                out.pop("token_in_credential_store", None)

        tmp = self.config_path.with_suffix(".tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(out, f, indent=2)
            try:
                os.chmod(tmp, 0o600)
            except OSError:
                pass
            os.replace(tmp, self.config_path)
            return True
        except OSError:
            return False

    def migrate_secrets(self) -> bool:
        """Move a plaintext token from an older config file into the credential store."""
        if not self.use_keyring or not credentials.available():
            return False
        raw = self._read_raw()
        if raw.get("twitch_oauth") and raw.get("twitch_username"):
            return self.save(self.load())
        return False

    def token_is_protected(self) -> bool:
        return bool(self._read_raw().get("token_in_credential_store"))

    def exists(self) -> bool:
        return self.config_path.exists()

    def get_default_config(self) -> Dict[str, Any]:
        return {
            "vlc_path": "",
            "twitch_username": "",
            "twitch_oauth": "",
            "streamlink_quality": "best",
            "chat_colors_enabled": True,
            "recording_path": str(Path.home() / "Videos" / "TwitchRecordings"),
            "last_channels": "",
            "favorites": [],
            "skip_ffmpeg_install": False,
        }
