"""Chat token storage in the operating system's credential store.

Windows Credential Manager, macOS Keychain or the Linux Secret Service, via the
`keyring` package. Every function degrades gracefully: if there is no usable store
the caller keeps the token in the config file instead.
"""
import logging
from typing import Optional

logger = logging.getLogger(__name__)

SERVICE = "nTwitch"


def _keyring():
    try:
        import keyring
        return keyring
    except Exception:
        return None


def available() -> bool:
    """True if a real credential store backs `keyring` on this machine."""
    kr = _keyring()
    if kr is None:
        return False
    try:
        backend = kr.get_keyring()
        return not backend.__class__.__module__.endswith(("fail", "null"))
    except Exception:
        return False


def get_token(username: str) -> Optional[str]:
    kr = _keyring()
    if kr is None or not username:
        return None
    try:
        return kr.get_password(SERVICE, username.lower())
    except Exception as e:
        logger.debug("credential read failed: %s", e)
        return None


def set_token(username: str, token: str) -> bool:
    kr = _keyring()
    if kr is None or not username or not token or not available():
        return False
    try:
        kr.set_password(SERVICE, username.lower(), token)
        return kr.get_password(SERVICE, username.lower()) == token
    except Exception as e:
        logger.debug("credential write failed: %s", e)
        return False


def delete_token(username: str) -> None:
    kr = _keyring()
    if kr is None or not username:
        return
    try:
        kr.delete_password(SERVICE, username.lower())
    except Exception as e:  # not found / no store: nothing to do
        logger.debug("credential delete: %s", e)
