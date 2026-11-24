"""First-run and `--setup` wizard.

Tools are already found/installed before this runs, so the only real question
is whether to log in to chat. Everything else has a sensible default.
"""
import asyncio
import webbrowser
from pathlib import Path
from typing import Any, Dict

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.text import Text

from . import credentials, theme
from .chat_client import verify_login
from .config_manager import ConfigManager
from .tools import ToolSet

TOKEN_URL = "https://twitchtokengenerator.com/"
A = theme.ACCENT
M = theme.MUTED


def _heading(console: Console, text: str) -> None:
    console.print(f"\n[bold {A}]{text}[/]")


def _tool_status(console: Console, tools: ToolSet) -> None:
    _heading(console, "Tools")
    console.print(f"  [{theme.OK}]✓[/] VLC           [{M}]{tools.vlc}[/]")
    console.print(f"  [{theme.OK}]✓[/] Streamlink    [{M}]{' '.join(tools.streamlink or [])}[/]")
    if tools.ffmpeg:
        console.print(f"  [{theme.OK}]✓[/] ffmpeg        [{M}]{tools.ffmpeg}[/]")
    else:
        console.print(f"  [{theme.WARN}]–[/] ffmpeg        [{M}]not installed: clips will be saved as raw .ts files[/]")


def _chat_login(console: Console, config: Dict[str, Any]) -> None:
    _heading(console, "Chat")
    logged_in = bool(config.get("twitch_oauth") and config.get("twitch_username"))

    if logged_in:
        console.print(f"  Logged in as [bold]{config['twitch_username']}[/].")
        choice = Prompt.ask("  What would you like to do?", choices=["keep", "change", "logout"], default="keep")
        if choice == "keep":
            return
        if choice == "logout":
            config["twitch_oauth"] = ""
            config["twitch_username"] = ""
            console.print(f"  [{theme.OK}]✓[/] Logged out. Chat will be read-only.")
            return
    else:
        console.print(f"  [{M}]You can read chat without an account. Log in only if you want to send messages.[/]")
        if not Confirm.ask("  Log in to chat now?", default=False):
            console.print(f"  [{M}]No problem. Run [bold]nTwitch --setup[/bold] any time to add it.[/]")
            return

    if Confirm.ask("  Open the token page in your browser?", default=True):
        try:
            webbrowser.open(TOKEN_URL)
        except Exception:
            pass
    console.print(f"\n  [{M}]At {TOKEN_URL}:[/]")
    console.print(f"  [{M}]1. Choose [bold]Bot Chat Token[/bold], then authorize with Twitch[/]")
    console.print(f"  [{M}]2. Copy the [bold]Access Token[/bold] and paste it below (it stays hidden)[/]\n")

    for _ in range(3):
        token = Prompt.ask("  Access token (Enter to skip)", password=True, default="", show_default=False).strip()
        if not token:
            console.print(f"  [{M}]Skipped. Chat stays read-only.[/]")
            return
        username = Prompt.ask("  Your Twitch username").strip().lower().lstrip("@")
        if not username:
            continue
        if not token.startswith("oauth:"):
            token = f"oauth:{token}"

        with console.status(f"[{A}]Checking with Twitch…[/]", spinner="dots", spinner_style=A):
            ok, message = asyncio.run(verify_login(token, username))
        if ok:
            config["twitch_oauth"] = token
            config["twitch_username"] = username
            console.print(f"  [{theme.OK}]✓[/] Logged in as [bold]{username}[/].")
            if credentials.available():
                console.print(f"  [{M}]The token is kept in your system's credential store, not in the config file.[/]")
            else:
                console.print(f"  [{M}]No credential store found: the token is saved in plain text in your config file.[/]")
            return
        console.print(f"  [{theme.ERR}]✗[/] {message}")
        if not Confirm.ask("  Try again?", default=True):
            if Confirm.ask("  Save these details anyway?", default=False):
                config["twitch_oauth"] = token
                config["twitch_username"] = username
            return


def _save_locations(console: Console, config: Dict[str, Any]) -> None:
    _heading(console, "Recordings")
    console.print(f"  [{M}]Recordings:[/] {config.get('recording_path')}")
    console.print(f"  [{M}]Clips:[/]      {config.get('clips_path') or Path.home() / 'Videos' / 'TwitchClips'}")
    if not Confirm.ask("  Change these folders?", default=False):
        return
    for key, label in (("recording_path", "Recordings folder"), ("clips_path", "Clips folder")):
        current = config.get(key) or str(Path.home() / "Videos" / ("TwitchClips" if key == "clips_path" else "TwitchRecordings"))
        value = Prompt.ask(f"  {label}", default=current).strip()
        try:
            Path(value).mkdir(parents=True, exist_ok=True)
            config[key] = value
        except OSError as e:
            console.print(f"  [{theme.ERR}]✗[/] Can't use that folder ({e}). Keeping {current}.")


def run_setup(console: Console, config_manager: ConfigManager, tools: ToolSet,
              current: Dict[str, Any], first_run: bool) -> bool:
    """Run the wizard and save the config. Returns False if the config could not be saved."""
    config = config_manager.get_default_config()
    config.update(current)
    config["vlc_path"] = tools.vlc or config.get("vlc_path", "")

    if first_run:
        console.print(Panel(
            Text.assemble(("Welcome to nTwitch\n", f"bold {A}"),
                          ("Watch streams in VLC, with live chat in your terminal.", M)),
            border_style=theme.BORDER_DIM, padding=(1, 3), expand=False,
        ))
    else:
        console.print(f"[bold {A}]nTwitch setup[/]")

    _tool_status(console, tools)
    _chat_login(console, config)
    if not first_run:
        _save_locations(console, config)

    if not config_manager.save(config):
        console.print(f"\n[{theme.ERR}]Could not save settings to {config_manager.config_path}[/]")
        return False
    console.print(f"\n[{theme.OK}]✓ All set.[/] [{M}]Settings saved to {config_manager.config_path}[/]\n")
    return True
