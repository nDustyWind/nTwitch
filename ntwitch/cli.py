"""nTwitch - watch Twitch in VLC with live chat in your terminal."""
import argparse
import asyncio
import logging
import signal
import sys
from pathlib import Path
from typing import Callable, List, Optional, Tuple, Union

# Python packages must exist before anything below imports them (first run installs them).
from ntwitch.bootstrap import ensure_python_packages

ensure_python_packages()

from rich.console import Console  # noqa: E402
from rich.panel import Panel  # noqa: E402
from rich.text import Text  # noqa: E402

from ntwitch import theme  # noqa: E402
from ntwitch.chat_client import ChatManager, ChatMessage  # noqa: E402
from ntwitch.clip_manager import ClipManager  # noqa: E402
from ntwitch.config_manager import ConfigManager  # noqa: E402
from ntwitch.recording_manager import RecordingManager  # noqa: E402
from ntwitch.setup import run_setup  # noqa: E402
from ntwitch.stream_manager import MAX_CHANNELS, StreamManager, parse_channel_input  # noqa: E402
from ntwitch.terminal_ui import ChannelSelectionApp, MultiChannelUI, SimpleUI  # noqa: E402
from ntwitch.tools import ToolSet, ensure_tools, find_ffmpeg  # noqa: E402

logger = logging.getLogger("nTwitch")

A = theme.ACCENT
M = theme.MUTED

STOP_MESSAGES = {
    "closed": "The video window for #{ch} was closed.",
    "offline": "#{ch} went offline.",
    "max_retries": "Lost the video stream for #{ch} (gave up after several retries).",
    "connection_error": "Connection lost for #{ch}.",
}


def configure_logging(debug: bool) -> None:
    """Silent by default. `--debug` writes a log file; nothing is ever printed to the terminal."""
    root = logging.getLogger()
    root.handlers.clear()
    logging.raiseExceptions = False
    if debug:
        handler = logging.FileHandler(Path.home() / ".twitch-terminal.log", encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s"))
        root.addHandler(handler)
        root.setLevel(logging.DEBUG)
    else:
        root.addHandler(logging.NullHandler())
        root.setLevel(logging.CRITICAL)


class TwitchViewer:
    """Runs one viewing session: video windows, chat, recording and clips."""

    def __init__(self, console: Console, config: dict, channels: List[str], tools: ToolSet,
                 no_chat: bool = False, quality: str = "best", use_simple_ui: bool = False,
                 prechecked: bool = False, persist: Optional[Callable[[dict], None]] = None,
                 notices: Optional[List[Tuple[str, str]]] = None):
        self.console = console
        self.config = config
        self.channels = channels
        self.tools = tools
        self.no_chat = no_chat
        self.quality = quality
        self.use_simple_ui = use_simple_ui
        self.prechecked = prechecked
        self.persist = persist
        self.settings_applied = False

        self.stream_manager: Optional[StreamManager] = None
        self.chat_manager: Optional[ChatManager] = None
        self.ui: Optional[Union[MultiChannelUI, SimpleUI]] = None
        self.recording_manager: Optional[RecordingManager] = None
        self.clip_manager: Optional[ClipManager] = None

        self.running = False
        self.exit_code = 0
        self.monitor_task: Optional[asyncio.Task] = None
        self.switch_channels: Optional[List[str]] = None
        self.notices: List[Tuple[str, str]] = list(notices or [])

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        self.running = True
        try:
            await self._run()
        finally:
            await self.stop()

    async def _run(self) -> None:
        self.stream_manager = StreamManager(vlc_path=self.config["vlc_path"], quality=self.quality)

        label = f"Opening {len(self.channels)} stream{'s' if len(self.channels) != 1 else ''}…"
        with self.console.status(f"[{A}]{label}[/]", spinner="dots", spinner_style=A):
            results = await asyncio.to_thread(
                self.stream_manager.launch_streams, self.channels, self.prechecked
            )

        live = [ch for ch, (ok, _) in results.items() if ok]
        failed = [(ch, msg) for ch, (ok, msg) in results.items() if not ok]

        if not live:
            self._explain_no_streams(failed)
            self.exit_code = 1
            return

        self.notices.append(("Video windows are opening - VLC can take a few seconds to start.", "information"))
        for ch, msg in failed:
            self.notices.append((f"#{ch}: {msg}. Skipped.", "warning"))

        try:
            self.recording_manager = RecordingManager(quality=self.quality,
                                                      recording_path=self.config.get("recording_path"))
        except OSError:
            self.notices.append(("Recording is unavailable: can't write to the recordings folder.", "warning"))
        try:
            self.clip_manager = ClipManager(quality=self.quality, buffer_minutes=10,
                                            clips_path=self.config.get("clips_path"))
        except OSError:
            self.notices.append(("Clips are unavailable: can't write to the clips folder.", "warning"))

        if self.no_chat:
            self.console.print(Panel(
                Text.assemble(("Streams are open.\n", f"bold {A}"),
                              ("Close the video windows or press Ctrl+C to quit.", M)),
                border_style=theme.BORDER_DIM, expand=False))
            self.monitor_task = asyncio.create_task(self._monitor_streams())
            while self.running:
                await asyncio.sleep(0.5)
            return

        await self._run_chat(live)

    async def _run_chat(self, channels: List[str]) -> None:
        token = self.config.get("twitch_oauth", "")
        username = self.config.get("twitch_username", "")
        authenticated = bool(token and username)

        if self.use_simple_ui:
            self.ui = SimpleUI(channels, authenticated)
            self.ui.start()
        else:
            self.ui = MultiChannelUI(channels, authenticated, self.recording_manager, self.clip_manager,
                                     username, notices=self.notices,
                                     favorites=self.config.get("favorites", []),
                                     favorites_callback=self._save_favorites)
            self.ui.set_callbacks(
                send_callback=self._send_message,
                recording_callback=self._toggle_recording,
                clip_callback=self._save_clip,
                switch_callback=self._switch_streamers,
                channel_change_callback=self._on_channel_change,
                settings_provider=self._settings_values,
                settings_callback=self._apply_settings,
            )

        self.chat_manager = ChatManager(
            channels=channels,
            message_callback=self._on_message,
            token=token if authenticated else None,
            username=username if authenticated else None,
            status_callback=self._notify,
        )
        try:
            await self.chat_manager.connect()
        except Exception as e:
            logger.error("Chat connection failed: %s", e)
            self._notify(self._friendly_chat_error(str(e)), "error")

        self.monitor_task = asyncio.create_task(self._monitor_streams())
        if isinstance(self.ui, MultiChannelUI):
            await self.ui.run_async()  # blocks until the user quits
        else:
            while self.running:
                await asyncio.sleep(0.5)

    async def stop(self) -> None:
        """Close every window and background process. Safe to call more than once."""
        self.running = False
        if self.monitor_task:
            self.monitor_task.cancel()
            try:
                await self.monitor_task
            except (asyncio.CancelledError, Exception):
                pass
            self.monitor_task = None
        if self.chat_manager:
            try:
                await self.chat_manager.disconnect()
            except Exception:
                pass
            self.chat_manager = None
        for component in (self.recording_manager, self.clip_manager, self.stream_manager):
            if component:
                try:
                    component.cleanup()
                except Exception as e:
                    logger.error("Cleanup error: %s", e)
        self.recording_manager = self.clip_manager = self.stream_manager = None

    # ------------------------------------------------------------------ helpers

    def _notify(self, message: str, severity: str = "information") -> None:
        """Show a message in the UI, or print it when there is no UI."""
        if self.ui:
            self.ui.set_status(message, severity)
        else:
            self.console.print(f"[{M}]{message}[/]")

    @staticmethod
    def _friendly_chat_error(error: str) -> str:
        if "getaddrinfo failed" in error or "11002" in error:
            return "Chat is unavailable: can't reach Twitch. Check your internet connection."
        if "timed out" in error:
            return "Chat timed out. A firewall may be blocking it. Video still works."
        if "refused" in error:
            return "Twitch refused the chat connection. Try again later."
        return "Chat is unavailable right now. Video still works."

    def _explain_no_streams(self, failed: List[Tuple[str, str]]) -> None:
        body = Text()
        for ch, msg in failed:
            body.append(f"#{ch}", style="bold")
            body.append(f"  {msg}\n", style=M)
        body.append("\nCheck the spelling, or try again when they are live.", style=theme.WARN)
        self.console.print(Panel(body, title="Nothing to watch", title_align="left",
                                 border_style=theme.WARN, padding=(1, 2), expand=False))

    # ------------------------------------------------------------------ callbacks

    def _on_message(self, message: ChatMessage) -> None:
        if self.ui:
            self.ui.add_message(message)

    async def _on_channel_change(self, channel: str) -> None:
        if self.clip_manager and not self.clip_manager.is_buffering(channel):
            self.clip_manager.start_buffer(channel)

    async def _switch_streamers(self, channel_input: str) -> None:
        new_channels = parse_channel_input(channel_input)[:MAX_CHANNELS]
        if not new_channels:
            return
        self.switch_channels = new_channels
        if isinstance(self.ui, MultiChannelUI):
            self.ui.exit()

    def _save_favorites(self, favorites: List[str]) -> None:
        self.config["favorites"] = favorites
        if self.persist:
            self.persist({"favorites": favorites})

    def _settings_values(self) -> dict:
        home = Path.home() / "Videos"
        return {
            "streamlink_quality": self.quality,
            "recording_path": self.config.get("recording_path") or str(home / "TwitchRecordings"),
            "clips_path": self.config.get("clips_path") or str(home / "TwitchClips"),
            "twitch_username": self.config.get("twitch_username", ""),
            "protected": bool(self.config.get("token_in_credential_store")),
        }

    async def _apply_settings(self, result: dict) -> None:
        """Save what changed in the settings screen, then restart the session so it takes effect."""
        current = self._settings_values()
        changed = {}
        for key in ("streamlink_quality", "recording_path", "clips_path"):
            if result.get(key) and result[key] != current[key]:
                changed[key] = result[key]
        if "twitch_username" in result:  # the screen only sends these on login or logout
            if result["twitch_username"] != current["twitch_username"] or result.get("twitch_oauth"):
                changed["twitch_username"] = result["twitch_username"]
                changed["twitch_oauth"] = result.get("twitch_oauth", "")
        if not changed:
            self._notify("No changes to apply.")
            return
        if self.persist:
            self.persist(changed)
        self.config.update(changed)
        self.settings_applied = True
        self.switch_channels = list(self.channels)
        if isinstance(self.ui, MultiChannelUI):
            self.ui.exit()

    async def _toggle_recording(self, channel: str) -> Tuple[bool, str]:
        if not self.recording_manager:
            return False, "Recording isn't available."
        recording, message = self.recording_manager.toggle_recording(channel)
        if recording:
            return True, f"Recording #{channel}. Press F2 again to stop."
        return False, message

    async def _save_clip(self, channel: str) -> Tuple[bool, str]:
        if not self.clip_manager:
            return False, "Clips aren't available."
        if not self.clip_manager.is_buffering(channel):
            self.clip_manager.start_buffer(channel)
            return True, (f"Buffering #{channel} from now on. Press F3 again at any point to save "
                          f"everything buffered (up to the last 10 minutes).")
        ok, message, _ = await asyncio.to_thread(self.clip_manager.save_clip, channel)
        return ok, message

    async def _monitor_streams(self) -> None:
        """Report video windows that closed, streams that ended, and restart dropped ones."""
        try:
            while self.running and self.stream_manager:
                dead = await asyncio.to_thread(self.stream_manager.check_streams)
                for channel, reason in dead:
                    text = STOP_MESSAGES.get(reason, STOP_MESSAGES["connection_error"]).format(ch=channel)
                    self._notify(text, "warning")
                if self.no_chat and self.stream_manager and not self.stream_manager.streams:
                    self.running = False  # nothing left to watch in video-only mode
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("Stream monitor error: %s", e)

    async def _send_message(self, channel: str, content: str) -> None:
        if not self.chat_manager:
            return
        await self.chat_manager.send_message(channel, content)
        username = self.config.get("twitch_username")
        if self.ui and username:
            # Twitch does not echo our own messages back, so show them ourselves.
            self.ui.add_message(ChatMessage(channel=channel, author=username, content=content,
                                            color="#00FF7F", badges=[]))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="nTwitch",
        description="Watch Twitch in VLC with live chat in your terminal.",
    )
    parser.add_argument("channels_positional", nargs="*", metavar="CHANNEL",
                        help="channels to watch (same as --channels)")
    parser.add_argument("--channels", "-c", type=str, help="comma-separated channel list, skips the picker")
    parser.add_argument("--quality", "-q", type=str, default=None,
                        help="stream quality: best, 1080p60, 720p60, 720p, 480p, 360p (default: your saved setting)")
    parser.add_argument("--no-chat", action="store_true", help="video only, no chat")
    parser.add_argument("--anon", action="store_true", help="read-only chat even if you are logged in")
    parser.add_argument("--simple-ui", action="store_true", help="plain scrolling chat instead of split panes")
    parser.add_argument("--setup", "--config", dest="setup", action="store_true",
                        help="re-run setup (chat login, save folders)")
    parser.add_argument("--debug", action="store_true",
                        help="write a diagnostic log to ~/.twitch-terminal.log")
    args = parser.parse_args()
    if args.channels_positional and not args.channels:
        args.channels = ",".join(args.channels_positional)
    return args


def main() -> int:
    args = parse_args()
    configure_logging(args.debug)
    console = Console(highlight=False)

    config_manager = ConfigManager()
    config_manager.migrate_secrets()  # older versions kept the chat token in the config file
    config = config_manager.load()
    first_run = not config

    if args.setup:
        config["skip_ffmpeg_install"] = False  # let setup retry a failed ffmpeg install

    # 1. Tools: found or installed automatically. Prints nothing when everything is already there.
    want_ffmpeg = not config.get("skip_ffmpeg_install")
    tools = ensure_tools(console, vlc_hint=config.get("vlc_path"), want_ffmpeg=want_ffmpeg)
    if not tools.vlc or not tools.streamlink:
        console.print(f"\n[{theme.ERR}]nTwitch can't run without VLC and Streamlink.[/] "
                      f"[{M}]Install the missing tool(s) above, then start nTwitch again.[/]")
        return 1
    # Remember a failed ffmpeg install so it isn't retried on every launch (--setup retries it).
    if want_ffmpeg:
        config["skip_ffmpeg_install"] = not tools.ffmpeg
    elif find_ffmpeg():
        config["skip_ffmpeg_install"] = False  # the user installed it themselves
    config["vlc_path"] = tools.vlc

    # 2. Setup: only on the very first run, or when asked.
    if first_run or args.setup:
        if not run_setup(console, config_manager, tools, config, first_run):
            return 1
        config = config_manager.load()
        if args.setup:
            console.print(f"[{M}]Start watching any time with[/] [bold]nTwitch[/]\n")
            return 0
    elif config != config_manager.load():
        config_manager.save(config)

    def persist(updates: dict) -> None:
        """Merge settings into the saved config (used by the UI for favourites and the settings screen)."""
        config.update(updates)
        config_manager.save(config)

    # 3. Which channels?
    prechecked = False
    if args.channels:
        channels = parse_channel_input(args.channels)
        if not channels:
            console.print(f"[{theme.ERR}]Those don't look like channel names.[/] Try:  nTwitch xqc")
            return 1
        if len(channels) > MAX_CHANNELS:
            console.print(f"[{theme.WARN}]Showing the first {MAX_CHANNELS} channels (the limit).[/]")
            channels = channels[:MAX_CHANNELS]
    else:
        selected = ChannelSelectionApp.get_channels(
            initial=config.get("last_channels", ""),
            favorites=config.get("favorites", []),
            favorites_callback=lambda favs: persist({"favorites": favs}),
        )
        if not selected:
            console.print(f"[{M}]Goodbye![/]")
            return 0
        channels = parse_channel_input(selected)
        prechecked = True  # the picker only returns channels it saw live

    persist({"last_channels": ", ".join(channels)})

    # 4. Watch. Switching channels (F4) or applying settings (F5) restarts the loop.
    current: dict = {"viewer": None}

    def on_signal(sig, frame):
        if current["viewer"]:
            current["viewer"].running = False

    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)

    exit_code = 0
    cli_quality = args.quality
    notices: List[Tuple[str, str]] = []
    while True:
        run_config = dict(config)
        if args.anon:
            run_config["twitch_oauth"] = ""
            run_config["twitch_username"] = ""
        quality = cli_quality or config.get("streamlink_quality") or "best"

        viewer = TwitchViewer(console, run_config, channels, tools, no_chat=args.no_chat, quality=quality,
                              use_simple_ui=args.simple_ui, prechecked=prechecked, persist=persist,
                              notices=notices)
        current["viewer"] = viewer
        try:
            asyncio.run(viewer.start())
        except KeyboardInterrupt:
            break
        except Exception as e:
            logger.exception("Unexpected error")
            console.print(f"\n[{theme.ERR}]Something went wrong:[/] {e}")
            console.print(f"[{M}]Run with --debug and check ~/.twitch-terminal.log for details.[/]")
            exit_code = 1
            break
        if viewer.switch_channels:
            channels, prechecked = viewer.switch_channels, True
            persist({"last_channels": ", ".join(channels)})
            notices = []
            if viewer.settings_applied:
                cli_quality = None  # a quality chosen in Settings replaces the command-line one
                notices = [("Settings applied.", "information")]
            continue
        exit_code = viewer.exit_code
        break

    console.print(f"[{M}]Goodbye![/]")
    return exit_code


def run() -> None:
    """Console entry point."""
    for stream in (sys.stdout, sys.stderr):  # legacy Windows consoles default to cp1252, which can't draw our symbols
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    try:
        sys.exit(main())
    except (KeyboardInterrupt, EOFError):
        print()
        print("Cancelled.")
        sys.exit(130)
