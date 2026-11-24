"""Finding and installing the external tools nTwitch needs (VLC, Streamlink, ffmpeg).

Everything here is stdlib-only so it works before any pip package is installed.
"""
import glob
import importlib.util
import logging
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from typing import Callable, List, Optional

logger = logging.getLogger(__name__)

IS_WIN = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"
FROZEN = bool(getattr(sys, "frozen", False))  # running as a PyInstaller exe

# Keeps helper processes from flashing console windows on Windows.
NO_WINDOW = 0x08000000 if IS_WIN else 0


def popen_kwargs() -> dict:
    """Common kwargs for background helper processes: silent and detached from our console."""
    kw = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if IS_WIN:
        kw["creationflags"] = NO_WINDOW
    return kw


def kill_tree(proc: subprocess.Popen, timeout: float = 3.0) -> None:
    """Stop a process *and its children* (streamlink -> VLC)."""
    if proc.poll() is not None:
        return
    try:
        if IS_WIN:
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                creationflags=NO_WINDOW, capture_output=True, timeout=timeout + 2,
            )
        else:
            proc.terminate()
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
    except Exception as e:  # never let cleanup raise
        logger.debug("kill_tree failed: %s", e)


# --------------------------------------------------------------------------- PATH

def refresh_path() -> None:
    """Pick up PATH entries added by installers that ran after this process started."""
    if not IS_WIN:
        return
    try:
        import winreg
        entries: List[str] = []
        for hive, sub in (
            (winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
            (winreg.HKEY_CURRENT_USER, "Environment"),
        ):
            try:
                with winreg.OpenKey(hive, sub) as key:
                    value, _ = winreg.QueryValueEx(key, "Path")
                    entries += os.path.expandvars(value).split(";")
            except OSError:
                pass
        current = os.environ.get("PATH", "").split(";")
        known = {c.lower().rstrip("\\") for c in current}
        extra = [e for e in entries if e and e.lower().rstrip("\\") not in known]
        os.environ["PATH"] = ";".join(current + extra)
    except Exception as e:
        logger.debug("refresh_path failed: %s", e)


# --------------------------------------------------------------------------- finders

def find_vlc(hint: Optional[str] = None) -> Optional[str]:
    """Locate the VLC executable. `hint` (e.g. from the config file) is tried first."""
    candidates: List[str] = []
    if hint:
        candidates.append(hint)

    if IS_WIN:
        for var in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432"):
            base = os.environ.get(var)
            if base:
                candidates.append(os.path.join(base, "VideoLAN", "VLC", "vlc.exe"))
        local = os.environ.get("LOCALAPPDATA")
        if local:
            candidates.append(os.path.join(local, "Programs", "VideoLAN", "VLC", "vlc.exe"))
        try:
            import winreg
            for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
                for sub in (r"SOFTWARE\VideoLAN\VLC", r"SOFTWARE\WOW6432Node\VideoLAN\VLC"):
                    try:
                        with winreg.OpenKey(hive, sub) as key:
                            install_dir, _ = winreg.QueryValueEx(key, "InstallDir")
                            candidates.append(os.path.join(install_dir, "vlc.exe"))
                    except OSError:
                        pass
        except ImportError:
            pass
    elif IS_MAC:
        candidates.append("/Applications/VLC.app/Contents/MacOS/VLC")
        candidates.append(os.path.expanduser("~/Applications/VLC.app/Contents/MacOS/VLC"))
    else:
        candidates += ["/usr/bin/vlc", "/usr/local/bin/vlc", "/snap/bin/vlc"]

    on_path = shutil.which("vlc")
    if on_path:
        candidates.append(on_path)

    for c in candidates:
        if c and os.path.isfile(c):
            return c
    return None


def find_streamlink() -> Optional[List[str]]:
    """Return the command prefix that runs Streamlink, or None if it is not available."""
    if not FROZEN and importlib.util.find_spec("streamlink_cli") is not None:
        return [sys.executable, "-m", "streamlink_cli"]
    on_path = shutil.which("streamlink")
    if on_path:
        return [on_path]
    if IS_WIN:
        for var in ("ProgramFiles", "ProgramFiles(x86)"):
            base = os.environ.get(var)
            if base:
                p = os.path.join(base, "Streamlink", "bin", "streamlink.exe")
                if os.path.isfile(p):
                    return [p]
    return None


def find_ffmpeg() -> Optional[str]:
    """Locate ffmpeg (only needed for saving clips)."""
    on_path = shutil.which("ffmpeg")
    if on_path:
        return on_path
    if IS_WIN:
        local = os.environ.get("LOCALAPPDATA", "")
        program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
        for link in (os.path.join(local, "Microsoft", "WinGet", "Links", "ffmpeg.exe"),     # user-scope winget
                     os.path.join(program_files, "WinGet", "Links", "ffmpeg.exe")):         # machine-scope winget
            if os.path.isfile(link):
                return link
        pattern = os.path.join(local, "Microsoft", "WinGet", "Packages", "*FFmpeg*", "**", "bin", "ffmpeg.exe")
        hits = glob.glob(pattern, recursive=True)
        if hits:
            return hits[0]
        for p in (r"C:\ProgramData\chocolatey\bin\ffmpeg.exe", r"C:\ffmpeg\bin\ffmpeg.exe"):
            if os.path.isfile(p):
                return p
    return None


def streamlink_cmd() -> List[str]:
    """Command prefix for running Streamlink (falls back to the bare name)."""
    return find_streamlink() or ["streamlink"]


# --------------------------------------------------------------------------- installers

def _run_quiet(cmd: List[str], timeout: int = 1800) -> bool:
    try:
        kw = {"creationflags": NO_WINDOW} if IS_WIN else {}
        result = subprocess.run(
            cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout, **kw
        )
        if result.returncode != 0:
            logger.debug("%s -> %s\n%s%s", cmd, result.returncode, result.stdout[-500:], result.stderr[-500:])
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired) as e:
        logger.debug("%s failed: %s", cmd, e)
        return False


def _is_admin() -> bool:
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _winget(package_id: str) -> bool:
    if not shutil.which("winget"):
        return False
    cmd = [
        "winget", "install", "--id", package_id, "-e", "--silent",
        "--accept-package-agreements", "--accept-source-agreements", "--disable-interactivity",
    ]
    if _is_admin():
        cmd += ["--scope", "machine"]  # winget refuses user-scope installs from an elevated shell
    _run_quiet(cmd)
    # winget uses non-zero codes for "already installed", so judge by the finders instead.
    return True


def install_vlc() -> bool:
    if IS_WIN:
        _winget("VideoLAN.VLC")
    elif IS_MAC:
        if shutil.which("brew"):
            _run_quiet(["brew", "install", "--cask", "vlc"])
    else:
        _linux_install("vlc")
    refresh_path()
    return find_vlc() is not None


def install_streamlink() -> bool:
    if not FROZEN and pip_install(["streamlink>=6.0.0"]):
        return find_streamlink() is not None
    if IS_WIN:
        _winget("Streamlink.Streamlink")
    elif IS_MAC and shutil.which("brew"):
        _run_quiet(["brew", "install", "streamlink"])
    else:
        _linux_install("streamlink")
    refresh_path()
    return find_streamlink() is not None


def install_ffmpeg() -> bool:
    if IS_WIN:
        _winget("Gyan.FFmpeg")
    elif IS_MAC:
        if shutil.which("brew"):
            _run_quiet(["brew", "install", "ffmpeg"])
    else:
        _linux_install("ffmpeg")
    refresh_path()
    return find_ffmpeg() is not None


def _linux_install(package: str) -> None:
    """Best effort on Linux; needs sudo, so the terminal is inherited for the password prompt."""
    for manager, args in (
        ("apt-get", ["install", "-y"]),
        ("dnf", ["install", "-y"]),
        ("pacman", ["-S", "--noconfirm"]),
        ("zypper", ["install", "-y"]),
    ):
        if shutil.which(manager):
            prefix = [] if (hasattr(os, "geteuid") and os.geteuid() == 0) else ["sudo"]
            try:
                subprocess.run(prefix + [manager] + args + [package], timeout=1800)
            except (OSError, subprocess.TimeoutExpired):
                pass
            return


def manual_hint(tool: str) -> str:
    """One line telling the user how to install `tool` themselves."""
    if tool == "vlc":
        if IS_WIN:
            return "winget install VideoLAN.VLC   (or https://www.videolan.org/vlc/)"
        if IS_MAC:
            return "brew install --cask vlc   (or https://www.videolan.org/vlc/)"
        return "sudo apt install vlc   (or your distro's package manager)"
    if tool == "streamlink":
        return "pip install streamlink   (or https://streamlink.github.io/install.html)"
    if tool == "ffmpeg":
        if IS_WIN:
            return "winget install Gyan.FFmpeg"
        if IS_MAC:
            return "brew install ffmpeg"
        return "sudo apt install ffmpeg"
    return ""


# --------------------------------------------------------------------------- pip

def pip_install(specs: List[str]) -> bool:
    """Install Python packages into the running interpreter's environment."""
    if FROZEN:
        return False
    base = [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "--quiet"]
    result = _try(base + specs)
    if result is True:
        return True
    if result == "no-pip":
        # uv-created virtualenvs ship without pip: bootstrap it, or let uv do the install.
        if _run_quiet([sys.executable, "-m", "ensurepip", "--upgrade"]):
            if _try(base + specs) is True:
                return True
        uv = shutil.which("uv")
        if uv:
            return _run_quiet([uv, "pip", "install", "--quiet", "--python", sys.executable] + specs)
    return False


def _try(cmd: List[str]):
    try:
        kw = {"creationflags": NO_WINDOW} if IS_WIN else {}
        r = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=1800, **kw)
    except (OSError, subprocess.TimeoutExpired):
        return False
    if r.returncode == 0:
        return True
    if "No module named pip" in (r.stderr + r.stdout):
        return "no-pip"
    logger.debug("pip failed: %s%s", r.stdout[-800:], r.stderr[-800:])
    return False


# --------------------------------------------------------------------------- orchestration

@dataclass
class ToolSet:
    vlc: Optional[str] = None
    streamlink: Optional[List[str]] = None
    ffmpeg: Optional[str] = None


def ensure_tools(
    console,
    vlc_hint: Optional[str] = None,
    want_ffmpeg: bool = True,
) -> ToolSet:
    """Find every tool, installing whatever is missing. Silent when nothing needs doing.

    `console` is a rich Console. ffmpeg is optional: failing to get it only disables clips.
    """
    from .theme import ACCENT, ERR, MUTED, OK, WARN  # local import: keeps module stdlib-only at import time

    refresh_path()
    tools = ToolSet(
        vlc=find_vlc(vlc_hint),
        streamlink=find_streamlink(),
        ffmpeg=find_ffmpeg() if want_ffmpeg else None,
    )

    plan: List[tuple] = []  # (key, label, installer, required)
    if not tools.vlc:
        plan.append(("vlc", "VLC media player", install_vlc, True))
    if not tools.streamlink:
        plan.append(("streamlink", "Streamlink", install_streamlink, True))
    if want_ffmpeg and not tools.ffmpeg:
        plan.append(("ffmpeg", "ffmpeg (for clips)", install_ffmpeg, False))

    if not plan:
        return tools

    console.print(f"\n[bold {ACCENT}]First-time setup[/]  [{MUTED}]installing what nTwitch needs…[/]")
    for key, label, installer, required in plan:
        with console.status(f"[{ACCENT}]Installing {label}…[/]", spinner="dots", spinner_style=ACCENT):
            ok = installer()
        if ok:
            console.print(f"  [{OK}]✓[/] {label} installed")
        elif required:
            console.print(f"  [{ERR}]✗[/] Could not install {label} automatically.")
            console.print(f"    [{MUTED}]Install it yourself with:[/] {manual_hint(key)}")
        else:
            console.print(f"  [{WARN}]![/] Could not install {label}; saving clips will be unavailable.")
            console.print(f"    [{MUTED}]To enable it later:[/] {manual_hint(key)}")

    tools.vlc = tools.vlc or find_vlc()
    tools.streamlink = tools.streamlink or find_streamlink()
    tools.ffmpeg = (tools.ffmpeg or find_ffmpeg()) if want_ffmpeg else None
    return tools
