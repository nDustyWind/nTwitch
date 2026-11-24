"""Textual user interface: chat panes, channel picker, title/status bars."""
import asyncio
import colorsys
import logging
import re
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from rich.text import Text
from textual import events, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.widget import Widget
from textual.widgets import Button, Footer, Input, Label, RichLog, Select, SelectionList, Static
from textual.widgets.selection_list import Selection

from . import theme
from .chat_client import ChatMessage, verify_login
from .stream_manager import MAX_CHANNELS, StreamManager, StreamStatus, parse_channel_input

logger = logging.getLogger(__name__)

SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

# Colours for chatters who never picked one (stable per name).
FALLBACK_COLORS = ["#ff9bb3", "#ffc48c", "#f5e58c", "#a6f0a6", "#8ce6e6", "#9bc2ff", "#c9a6ff", "#ff9be6"]

# (badge name, glyph, colour) - first match wins; shown before the username.
BADGES = (
    ("broadcaster", "★", theme.ERR),
    ("moderator", "◆", theme.OK),
    ("vip", "♦", "#f9baff"),
)


# Chat text is untrusted: drop every control character (including ESC) so nobody can send
# terminal escape sequences (title changes, screen clears, ...) through chat.
_CONTROL_CHARS = dict.fromkeys(list(range(0, 32)) + list(range(127, 160)))


def clean_text(text: str) -> str:
    return text.translate(_CONTROL_CHARS)


def readable_color(value: Optional[str]) -> Optional[str]:
    """Lighten a Twitch username colour if it would be hard to read on a dark background."""
    if not value:
        return None
    c = value.lstrip("#")
    if not re.fullmatch(r"[0-9a-fA-F]{6}", c):
        return None
    r, g, b = (int(c[i:i + 2], 16) / 255 for i in (0, 2, 4))
    h, l, s = colorsys.rgb_to_hls(r, g, b)
    l = max(l, 0.62)
    r, g, b = colorsys.hls_to_rgb(h, l, s)
    return "#{:02x}{:02x}{:02x}".format(round(r * 255), round(g * 255), round(b * 255))


def format_message(message: ChatMessage, my_username: Optional[str] = None) -> Text:
    """Render a chat message as styled text. Built from Text so chat content is never parsed as markup."""
    line = Text()

    if message.author == "SYSTEM":
        line.append("⚠ ", style=theme.WARN)
        line.append(clean_text(message.content), style=theme.WARN)
        return line

    for badge, glyph, color in BADGES:
        if badge in message.badges:
            line.append(f"{glyph} ", style=color)
            break

    color = readable_color(message.color) or FALLBACK_COLORS[sum(map(ord, message.author)) % len(FALLBACK_COLORS)]
    content = message.content
    is_action = content.startswith("\x01ACTION ")
    if is_action:
        content = content[len("\x01ACTION "):].rstrip("\x01")
    content = clean_text(content)
    author = clean_text(message.author)

    if is_action:
        line.append(f"{author} ", style=f"bold italic {color}")
        start = len(line)
        line.append(content, style=f"italic {color}")
    else:
        line.append(author, style=f"bold {color}")
        line.append(": ", style=theme.MUTED)
        start = len(line)
        line.append(content)

    if my_username:
        pattern = re.compile(rf"(?<!\w)@?{re.escape(my_username)}(?!\w)", re.IGNORECASE)
        for m in pattern.finditer(content):
            line.stylize(f"bold {theme.BG} on {theme.ACCENT}", start + m.start(), start + m.end())
    return line


class ChatInput(Input):
    """Chat message box."""


class TitleBar(Widget):
    """Slim bar: app name (in the accent) on the left, chat mode and clock on the right."""

    DEFAULT_CSS = f"""
    TitleBar {{
        height: 1;
        background: {theme.BG};
        color: {theme.MUTED};
        padding: 0 1;
    }}
    """

    def __init__(self, right: str = ""):
        super().__init__()
        self.right = right

    def on_mount(self) -> None:
        self.set_interval(1, self.refresh)

    def render(self) -> Text:
        right = f"{self.right}  {datetime.now().strftime('%H:%M')}" if self.right else datetime.now().strftime('%H:%M')
        left = "◆ nTwitch"
        gap = max(2, self.content_size.width - len(left) - len(right))
        text = Text(no_wrap=True, overflow="ellipsis")
        text.append(left, style=f"bold {theme.ACCENT}")
        text.append(" " * gap + right)
        return text


class StatusBar(Widget):
    """One line under the chat: what is selected, what is recording, and a contextual hint."""

    DEFAULT_CSS = f"""
    StatusBar {{
        height: 1;
        background: {theme.BG};
        color: {theme.TEXT};
        padding: 0 1;
    }}
    """

    left = reactive("")
    right = reactive("")

    def render(self) -> Text:
        gap = max(2, self.content_size.width - len(self.left) - len(self.right))
        text = Text(no_wrap=True, overflow="ellipsis")
        text.append(self.left)
        text.append(" " * gap)
        text.append(self.right, style=theme.MUTED)
        return text


class ChannelPane(Vertical):
    """One channel's chat. The border carries the channel name and recording state."""

    DEFAULT_CSS = f"""
    ChannelPane {{
        width: 1fr;
        height: 100%;
        background: {theme.SURFACE};
        border: round {theme.BORDER_DIM};
        border-title-color: {theme.MUTED};
        border-title-style: bold;
        border-subtitle-color: {theme.MUTED};
    }}
    ChannelPane.active {{
        border: round {theme.ACCENT};
        border-title-color: {theme.ACCENT};
    }}
    ChannelPane.recording {{
        border: round {theme.REC};
        border-title-color: {theme.REC};
        border-subtitle-color: {theme.REC};
        border-subtitle-style: bold;
    }}
    ChannelPane > RichLog {{
        background: {theme.SURFACE};
        padding: 0 1;
        overflow-x: hidden;
        scrollbar-size-vertical: 1;
        scrollbar-background: {theme.SURFACE};
        scrollbar-background-hover: {theme.SURFACE};
        scrollbar-background-active: {theme.SURFACE};
        scrollbar-color: {theme.BORDER_DIM};
        scrollbar-color-hover: {theme.MUTED};
        scrollbar-color-active: {theme.ACCENT};
    }}
    """

    def __init__(self, channel: str, max_messages: int = 200):
        super().__init__()
        self.channel = channel
        self.max_messages = max_messages
        self.messages: deque = deque(maxlen=max_messages)
        self.my_username: Optional[str] = None
        self.is_active = False
        self.is_recording = False
        self.recording_duration = ""
        self._log: Optional[RichLog] = None

    def compose(self) -> ComposeResult:
        self._log = RichLog(max_lines=self.max_messages, wrap=True, markup=False, highlight=False, min_width=10)
        self._log.can_focus = False
        yield self._log

    def on_mount(self) -> None:
        self._refresh_title()
        # Replay anything that arrived before the pane existed.
        for message in self.messages:
            self._log.write(format_message(message, self.my_username))

    def add_message(self, message: ChatMessage, my_username: Optional[str] = None) -> None:
        self.my_username = my_username
        self.messages.append(message)
        if self._log is not None and self.is_mounted:
            self._log.write(format_message(message, my_username))

    def clear_messages(self) -> None:
        self.messages.clear()
        if self._log is not None:
            self._log.clear()

    def set_active(self, active: bool) -> None:
        self.is_active = active
        self.set_class(active, "active")
        self._refresh_title()

    def set_recording(self, recording: bool, duration: str = "") -> None:
        self.is_recording = recording
        self.recording_duration = duration
        self.set_class(recording, "recording")
        self._refresh_title()

    def _refresh_title(self) -> None:
        self.border_title = f"{'●' if self.is_active else '○'} {self.channel.upper()}"
        self.border_subtitle = f"● REC {self.recording_duration}" if self.is_recording else ""


class FavoriteList(SelectionList):
    """Favourite channels; Enter starts watching just like in the text box."""

    BINDINGS = [Binding("enter", "watch", "Watch", show=False)]

    def action_watch(self) -> None:
        submit = getattr(self.screen, "action_submit", None)
        if submit:
            submit()


class ChannelPickerScreen(ModalScreen[Optional[str]]):
    """Ask which channels to watch and check that they are live.

    Dismisses with a comma-separated string of the live channels, or None if cancelled.
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", priority=True),
        Binding("ctrl+c", "cancel", "Cancel", show=False, priority=True),
        Binding("f2", "toggle_favorite", "Favourite", priority=True),
    ]

    DEFAULT_CSS = f"""
    ChannelPickerScreen {{
        align: center middle;
        background: {theme.BG} 85%;
    }}
    ChannelPickerScreen #favorites-title {{
        margin-top: 1;
        color: {theme.ACCENT};
        text-style: bold;
    }}
    ChannelPickerScreen #favorites {{
        height: auto;
        max-height: 7;
        background: {theme.BG};
        border: none;
        padding: 0;
    }}
    ChannelPickerScreen #favorites:focus {{
        border: none;
    }}
    ChannelPickerScreen #card {{
        width: 76;
        max-width: 96%;
        max-height: 100%;
        overflow-y: auto;
        height: auto;
        background: {theme.BG};
        border: round {theme.BORDER_DIM};
        padding: 1 3;
    }}
    ChannelPickerScreen #brand {{
        width: 100%;
        content-align: center middle;
        color: {theme.ACCENT};
        text-style: bold;
    }}
    ChannelPickerScreen #tagline {{
        width: 100%;
        content-align: center middle;
        color: {theme.MUTED};
        margin-bottom: 1;
    }}
    ChannelPickerScreen #prompt {{
        color: {theme.TEXT};
        text-style: bold;
    }}
    ChannelPickerScreen Input {{
        margin: 1 0 0 0;
        border: round {theme.BORDER_DIM};
        background: {theme.SURFACE};
    }}
    ChannelPickerScreen Input:focus {{
        border: round {theme.ACCENT};
        background-tint: transparent;
    }}
    ChannelPickerScreen #hint {{
        color: {theme.MUTED};
    }}
    ChannelPickerScreen #results {{
        height: auto;
        margin-top: 1;
    }}
    ChannelPickerScreen #keys {{
        margin-top: 1;
        color: {theme.MUTED};
    }}
    """

    def __init__(self, initial: str = "", prompt: str = "Which channels do you want to watch?",
                 cancel_label: str = "cancel", favorites: Optional[List[str]] = None,
                 favorites_callback: Optional[Callable[[List[str]], None]] = None):
        super().__init__()
        self.initial = initial
        self.prompt = prompt
        self.cancel_label = cancel_label
        self.favorites: List[str] = list(favorites or [])
        self.favorites_callback = favorites_callback
        self._live_favorites: set = set()
        self._busy = False
        self._finish_timer = None
        self._spinner_index = 0
        self._checking_label = ""
        self._done = False

    def compose(self) -> ComposeResult:
        with Vertical(id="card"):
            yield Label("◆  n T w i t c h", id="brand")
            yield Label("Twitch in your terminal", id="tagline")
            yield Label(self.prompt, id="prompt")
            yield Input(value=self.initial, placeholder="xqc, shroud   (or paste a twitch.tv link)", id="channel-input")
            yield Label(f"Separate channels with commas  ·  up to {MAX_CHANNELS} at once", id="hint")
            yield Label("★ Favourites", id="favorites-title")
            yield FavoriteList(id="favorites")
            yield Static("", id="results")
            yield Label(Text.assemble(("Enter", f"bold {theme.ACCENT}"), " watch    ",
                                      ("Tab", f"bold {theme.ACCENT}"), " favourites    ",
                                      ("F2", f"bold {theme.ACCENT}"), " ★ add/remove    ",
                                      ("Esc", f"bold {theme.ACCENT}"), f" {self.cancel_label}"), id="keys")

    def on_mount(self) -> None:
        self.query_one(Input).focus()
        self.set_interval(0.1, self._tick)
        self._render_favorites()
        if self.favorites:
            self._check_favorites()

    # ------------------------------------------------------------------ favourites

    def _typed(self) -> List[str]:
        return parse_channel_input(self.query_one("#channel-input", Input).value)

    def _render_favorites(self) -> None:
        """Rebuild the favourites list (live ones first), keeping the ticked state in sync with the input."""
        title = self.query_one("#favorites-title", Label)
        box = self.query_one("#favorites", FavoriteList)
        title.display = box.display = bool(self.favorites)
        typed = set(self._typed())
        ordered = sorted(self.favorites, key=lambda c: c not in self._live_favorites)  # stable: live first
        previous = box.highlighted or 0
        box.clear_options()
        box.add_options([
            Selection(
                Text.assemble((f"{c}", "bold"),
                              ("  ● live", theme.OK) if c in self._live_favorites else ("", "")),
                c, c in typed,
            )
            for c in ordered
        ])
        if ordered:
            box.highlighted = min(previous, len(ordered) - 1)  # keep a cursor so Tab, Space just works

    @work(exclusive=True, group="fav-live")
    async def _check_favorites(self) -> None:
        manager = StreamManager(vlc_path="")
        statuses = await asyncio.to_thread(manager.check_many, self.favorites[:12])
        self._live_favorites = {c for c, (s, _) in statuses.items() if s == StreamStatus.LIVE}
        if not self._done:
            self._render_favorites()

    def on_selection_list_selected_changed(self, event: SelectionList.SelectedChanged) -> None:
        """Ticking a favourite adds it to the input; unticking removes it."""
        event.stop()
        box = self.query_one("#favorites", FavoriteList)
        entry = self.query_one("#channel-input", Input)
        selected = set(box.selected)
        typed = self._typed()
        typed_favs = {c for c in typed if c in self.favorites}
        if typed_favs == selected:
            return  # already consistent (e.g. the change came from typing)
        kept = [c for c in typed if c not in self.favorites or c in selected]
        kept += [c for c in self.favorites if c in selected and c not in kept]
        entry.value = ", ".join(kept)

    def action_toggle_favorite(self) -> None:
        """F2: star (or un-star) the channels currently typed in the box."""
        typed = self._typed()
        results = self.query_one("#results", Static)
        if not typed:
            results.update(Text("Type a channel first, then press F2 to star it.", style=theme.WARN))
            return
        if all(c in self.favorites for c in typed):
            self.favorites = [c for c in self.favorites if c not in typed]
            message = f"Removed from favourites: {', '.join(typed)}"
        else:
            self.favorites += [c for c in typed if c not in self.favorites]
            message = f"★ Saved to favourites: {', '.join(typed)}"
        if self.favorites_callback:
            self.favorites_callback(list(self.favorites))
        self._render_favorites()
        results.update(Text(message, style=theme.OK))
        if self.favorites:
            self._check_favorites()

    def _tick(self) -> None:
        if self._busy:
            self._spinner_index = (self._spinner_index + 1) % len(SPINNER)
            self.query_one("#results", Static).update(
                Text.assemble((SPINNER[self._spinner_index] + " ", theme.ACCENT), self._checking_label)
            )

    def on_input_changed(self, event: Input.Changed) -> None:
        # Editing after a result cancels the pending auto-continue.
        if self._finish_timer is not None:
            self._finish_timer.stop()
            self._finish_timer = None
        if not self._busy:
            self.query_one("#results", Static).update("")
        # Keep the favourites ticks in step with what was typed.
        typed = set(parse_channel_input(event.value))
        box = self.query_one("#favorites", FavoriteList)
        current = set(box.selected)
        for fav in self.favorites:
            if fav in typed and fav not in current:
                box.select(fav)
            elif fav not in typed and fav in current:
                box.deselect(fav)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self.action_submit()

    def action_submit(self) -> None:
        if self._busy:
            return
        channels = self._typed()
        results = self.query_one("#results", Static)
        if not channels:
            results.update(Text("That doesn't look like a channel name - try something like  xqc", style=theme.WARN))
            return
        self._check(channels[:MAX_CHANNELS], len(channels) > MAX_CHANNELS)

    @work(exclusive=True)
    async def _check(self, channels: List[str], truncated: bool) -> None:
        self._busy = True
        self._checking_label = f"Checking {len(channels)} channel{'s' if len(channels) != 1 else ''}…"
        manager = StreamManager(vlc_path="")
        statuses = await asyncio.to_thread(manager.check_many, channels)
        self._busy = False

        lines = Text()
        live: List[str] = []
        for channel in channels:
            status, message = statuses[channel]
            if status == StreamStatus.LIVE:
                live.append(channel)
                lines.append("✓ ", style=theme.OK)
                lines.append(f"{channel}", style="bold")
                lines.append("  live\n", style=theme.OK)
            elif status == StreamStatus.ERROR:
                lines.append("! ", style=theme.ERR)
                lines.append(f"{channel}", style="bold")
                lines.append(f"  {message}\n", style=theme.ERR)
            else:
                lines.append("○ ", style=theme.WARN)
                lines.append(f"{channel}", style="bold")
                lines.append(f"  {message}\n", style=theme.MUTED)
        if truncated:
            lines.append(f"Only the first {MAX_CHANNELS} channels are used.\n", style=theme.MUTED)
        if not live:
            lines.append("\nNone of these are live right now. Edit the list and press Enter to try again.",
                         style=theme.WARN)
        lines.rstrip()
        self.query_one("#results", Static).update(lines)

        if live:
            delay = 0.5 if len(live) == len(channels) and not truncated else 2.0
            self._finish_timer = self.set_timer(delay, lambda: self._finish(",".join(live)))

    def _finish(self, value: Optional[str]) -> None:
        if not self._done:
            self._done = True
            self.dismiss(value)

    def action_cancel(self) -> None:
        self._finish(None)


QUALITIES = ["best", "1080p60", "720p60", "720p", "480p", "360p"]


class SettingsScreen(ModalScreen[Optional[dict]]):
    """Change quality, save folders and chat login without leaving the app.

    Dismisses with a dict of the new values, or None when cancelled.
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", priority=True),
        Binding("ctrl+c", "cancel", "Cancel", show=False, priority=True),
    ]

    DEFAULT_CSS = f"""
    SettingsScreen {{
        align: center middle;
        background: {theme.BG} 85%;
    }}
    SettingsScreen #settings-card {{
        width: 76;
        max-width: 96%;
        height: auto;
        max-height: 100%;
        background: {theme.BG};
        border: round {theme.BORDER_DIM};
        padding: 1 3;
    }}
    SettingsScreen #settings-title {{
        width: 100%;
        content-align: center middle;
        color: {theme.ACCENT};
        text-style: bold;
        margin-bottom: 1;
    }}
    SettingsScreen .field {{
        color: {theme.TEXT};
        text-style: bold;
        margin-top: 1;
    }}
    SettingsScreen .muted {{
        color: {theme.MUTED};
    }}
    SettingsScreen Input {{
        border: round {theme.BORDER_DIM};
        background: {theme.SURFACE};
    }}
    SettingsScreen Input:focus {{
        border: round {theme.ACCENT};
        background-tint: transparent;
    }}
    SettingsScreen Select {{
        width: 100%;
    }}
    SettingsScreen #buttons {{
        height: auto;
        margin-top: 1;
    }}
    SettingsScreen Button {{
        margin-right: 2;
        height: 3;
        min-width: 16;
    }}
    SettingsScreen Button.-style-default {{
        border: none;
        background: {theme.PANEL};
        color: {theme.TEXT};
        background-tint: transparent;
    }}
    SettingsScreen Button.-style-default:hover, SettingsScreen Button.-style-default:focus {{
        border: none;
        background: {theme.BORDER_DIM};
    }}
    SettingsScreen Button.-style-default.-primary {{
        background: {theme.ACCENT};
        color: {theme.BG};
    }}
    SettingsScreen Button.-style-default.-primary:hover, SettingsScreen Button.-style-default.-primary:focus {{
        background: {theme.ACCENT_DIM};
    }}
    SettingsScreen #settings-result {{
        height: auto;
        margin-top: 1;
    }}
    """

    def __init__(self, values: dict):
        super().__init__()
        self.values = values
        self._logout = False
        self._busy = False

    def compose(self) -> ComposeResult:
        v = self.values
        quality = v.get("streamlink_quality") or "best"
        options = QUALITIES if quality in QUALITIES else [quality] + QUALITIES
        username = v.get("twitch_username", "")
        with VerticalScroll(id="settings-card"):
            yield Label("◆  Settings", id="settings-title")
            yield Label("Video quality", classes="field")
            yield Select([(q, q) for q in options], value=quality, allow_blank=False, id="quality")
            yield Label("Recordings folder", classes="field")
            yield Input(value=v.get("recording_path", ""), id="rec-path")
            yield Label("Clips folder", classes="field")
            yield Input(value=v.get("clips_path", ""), id="clip-path")
            yield Label("Chat login", classes="field")
            if username:
                where = "stored in your system's credential store" if v.get("protected") else "stored in the config file"
                yield Label(f"Logged in as {username} ({where})", id="login-status", classes="muted")
            else:
                yield Label("Not logged in: chat is read-only. Get a token at twitchtokengenerator.com "
                            "(Bot Chat Token).", id="login-status", classes="muted")
            yield Input(value=username, placeholder="Twitch username", id="login-user")
            yield Input(placeholder="Access token (leave empty to keep what you have)", password=True,
                        id="login-token")
            yield Static("", id="settings-result")
            with Horizontal(id="buttons"):
                yield Button("Save & apply", variant="primary", id="save")
                if username:
                    yield Button("Log out", id="logout")
                yield Button("Cancel", id="cancel")
            yield Label("Saving restarts the video windows so the new settings take effect.", classes="muted")

    def on_mount(self) -> None:
        self.query_one("#quality", Select).focus()

    def _say(self, text: str, style: str = theme.MUTED) -> None:
        self.query_one("#settings-result", Static).update(Text(text, style=style))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == "cancel":
            self.dismiss(None)
        elif event.button.id == "logout":
            self._logout = True
            self.query_one("#login-user", Input).value = ""
            self.query_one("#login-token", Input).value = ""
            self._say("You will be logged out when you save.", theme.WARN)
        elif event.button.id == "save":
            self._save()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self._save()

    def action_cancel(self) -> None:
        self.dismiss(None)

    @work(exclusive=True)
    async def _save(self) -> None:
        if self._busy:
            return
        self._busy = True
        try:
            result = {"streamlink_quality": self.query_one("#quality", Select).value}
            for key, field_id, label in (("recording_path", "#rec-path", "recordings"),
                                         ("clips_path", "#clip-path", "clips")):
                value = self.query_one(field_id, Input).value.strip()
                if value:
                    try:
                        Path(value).expanduser().mkdir(parents=True, exist_ok=True)
                    except OSError as e:
                        self._say(f"Can't use that {label} folder: {e}", theme.ERR)
                        return
                result[key] = str(Path(value).expanduser()) if value else ""

            username = self.query_one("#login-user", Input).value.strip().lower().lstrip("@")
            token = self.query_one("#login-token", Input).value.strip()
            if self._logout and not token:
                result["twitch_username"], result["twitch_oauth"] = "", ""
            elif token:
                if not username:
                    self._say("Enter your Twitch username together with the token.", theme.WARN)
                    return
                if not token.startswith("oauth:"):
                    token = f"oauth:{token}"
                self._say("Checking the token with Twitch…", theme.ACCENT)
                ok, message = await verify_login(token, username)
                if not ok:
                    self._say(message, theme.ERR)
                    return
                result["twitch_username"], result["twitch_oauth"] = username, token
            self.dismiss(result)
        finally:
            self._busy = False


class MultiChannelUI(App):
    """Multi-channel Twitch chat UI."""

    TITLE = "nTwitch"
    ENABLE_COMMAND_PALETTE = False

    CSS = f"""
    Screen {{
        background: {theme.BG};
    }}
    #channels {{
        height: 1fr;
        padding: 0 1;
    }}
    #chat-input {{
        height: 3;
        margin: 0 1;
        border: round {theme.BORDER_DIM};
        background: {theme.SURFACE};
    }}
    #chat-input:focus {{
        border: round {theme.ACCENT};
        background-tint: transparent;
    }}
    Footer {{
        background: {theme.BG};
    }}
    """

    BINDINGS = [
        Binding("f1", "cycle_channel", "Next channel"),
        Binding("tab", "cycle_channel", "Next channel", show=False, priority=True),
        Binding("f2", "toggle_recording", "Record"),
        Binding("f3", "save_clip", "Clip"),
        Binding("f4", "switch_streamers", "Switch channels"),
        Binding("f5", "open_settings", "Settings"),
        Binding("ctrl+l", "clear_chat", "Clear chat"),
        Binding("ctrl+q", "quit", "Quit"),
        Binding("ctrl+c", "quit", "Quit", show=False, priority=True),
    ]

    MAX_MESSAGES_PER_PANE = 200
    MAX_VISIBLE_PANES = MAX_CHANNELS

    def __init__(self, channels: List[str], is_authenticated: bool = True,
                 recording_manager=None, clip_manager=None, username: Optional[str] = None,
                 notices: Optional[List[Tuple[str, str]]] = None,
                 favorites: Optional[List[str]] = None,
                 favorites_callback: Optional[Callable[[List[str]], None]] = None):
        super().__init__()
        self.register_theme(theme.make_theme())
        self.theme = "ntwitch"

        self.favorites = list(favorites or [])
        self.favorites_callback = favorites_callback
        self.settings_provider: Optional[Callable[[], dict]] = None
        self.settings_callback: Optional[Callable] = None
        self.channels = channels[:self.MAX_VISIBLE_PANES]
        self.is_authenticated = is_authenticated
        self.recording_manager = recording_manager
        self.clip_manager = clip_manager
        self.my_username = username.lower() if username else None
        self.notices = notices or []
        self.active_channel_index = 0

        self.send_callback: Optional[Callable] = None
        self.recording_callback: Optional[Callable] = None
        self.clip_callback: Optional[Callable] = None
        self.switch_callback: Optional[Callable] = None
        self.channel_change_callback: Optional[Callable] = None

        self.panes: Dict[str, ChannelPane] = {}
        self._panes_ready = False
        self._pending: List[ChatMessage] = []  # chat that arrived before the panes existed

    # ------------------------------------------------------------------ layout

    def compose(self) -> ComposeResult:
        mode = f"chat as {self.my_username}" if self.is_authenticated and self.my_username else "read-only chat"
        yield TitleBar(mode)
        with Horizontal(id="channels"):
            for channel in self.channels:
                pane = ChannelPane(channel, max_messages=self.MAX_MESSAGES_PER_PANE)
                self.panes[channel] = pane
                yield pane
        if self.is_authenticated:
            yield ChatInput(placeholder=self._placeholder(), id="chat-input")
        yield StatusBar()
        yield Footer()

    def on_mount(self) -> None:
        for pane in self.panes.values():
            pane.my_username = self.my_username
        if self.channels and self.channels[0] in self.panes:
            self.panes[self.channels[0]].set_active(True)
        self._panes_ready = True
        for message in self._pending:
            self.add_message(message)
        self._pending.clear()
        self._update_status()
        self.set_interval(1, self._update_status)
        for message, severity in self.notices:
            self.notify(message, severity=severity, timeout=8)

    def _placeholder(self) -> str:
        return f"Message #{self.get_active_channel()}  ·  Enter to send"

    def get_active_channel(self) -> str:
        if 0 <= self.active_channel_index < len(self.channels):
            return self.channels[self.active_channel_index]
        return self.channels[0] if self.channels else ""

    # ------------------------------------------------------------------ public API (used by the viewer)

    def add_message(self, message: ChatMessage) -> None:
        if not self._panes_ready:
            self._pending.append(message)
            return
        pane = self.panes.get(message.channel)
        if pane:
            pane.add_message(message, self.my_username)

    def set_status(self, message: str, severity: str = "information") -> None:
        """Show a short-lived notification."""
        try:
            self.notify(message, severity=severity, timeout=8)
        except Exception as e:  # app not running yet / already closing
            logger.debug("notify failed: %s", e)

    def set_callbacks(self, send_callback, recording_callback=None, clip_callback=None,
                      switch_callback=None, channel_change_callback=None,
                      settings_provider=None, settings_callback=None):
        self.settings_provider = settings_provider
        self.settings_callback = settings_callback
        self.send_callback = send_callback
        self.recording_callback = recording_callback
        self.clip_callback = clip_callback
        self.switch_callback = switch_callback
        self.channel_change_callback = channel_change_callback

    def stop(self) -> None:
        try:
            self.exit()
        except Exception as e:
            logger.debug("Error stopping UI: %s", e)

    # ------------------------------------------------------------------ status line

    def _update_status(self) -> None:
        try:
            bar = self.query_one(StatusBar)
        except Exception:
            return  # not mounted yet
        channel = self.get_active_channel()
        verb = "Chatting in" if self.is_authenticated else "Watching"
        left = f"{verb} #{channel.upper()}"

        if self.clip_manager:
            buffered = self.clip_manager.get_buffer_status(channel)
            if buffered:
                left += f"   clip buffer {buffered}"

        if self.recording_manager:
            for name in self.channels:
                is_rec = self.recording_manager.is_recording(name)
                pane = self.panes.get(name)
                if pane:
                    duration = self.recording_manager.get_recording_info(name) if is_rec else ""
                    if (pane.is_recording, pane.recording_duration) != (is_rec, duration or ""):
                        pane.set_recording(is_rec, duration or "")

        bar.left = left
        bar.right = "" if self.is_authenticated else "Read-only chat · run  nTwitch --setup  to log in and send messages"

    # ------------------------------------------------------------------ actions

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        if not self.is_authenticated or not self.send_callback:
            return
        message = event.value.strip()
        if message:
            await self.send_callback(self.get_active_channel(), message)
            event.input.value = ""

    def check_action(self, action: str, parameters) -> Optional[bool]:
        """While a dialog is open, Tab must move focus inside it instead of switching channels."""
        if action == "cycle_channel" and len(self.screen_stack) > 1:
            return False
        return True

    def _remember_favorites(self, favorites: List[str]) -> None:
        self.favorites = list(favorites)
        if self.favorites_callback:
            self.favorites_callback(list(favorites))

    def action_open_settings(self) -> None:
        if len(self.screen_stack) > 1 or not self.settings_provider:
            return
        self.push_screen(SettingsScreen(self.settings_provider()), self._on_settings_saved)

    async def _on_settings_saved(self, result: Optional[dict]) -> None:
        if result and self.settings_callback:
            await self.settings_callback(result)

    def action_cycle_channel(self) -> None:
        if len(self.screen_stack) > 1 or len(self.channels) <= 1:
            return  # a dialog is open, or nothing to cycle
        current = self.get_active_channel()
        if current in self.panes:
            self.panes[current].set_active(False)

        self.active_channel_index = (self.active_channel_index + 1) % len(self.channels)
        new_channel = self.get_active_channel()
        if new_channel in self.panes:
            self.panes[new_channel].set_active(True)

        if self.is_authenticated:
            try:
                self.query_one(ChatInput).placeholder = self._placeholder()
            except Exception:
                pass
        self._update_status()
        if self.channel_change_callback:
            asyncio.create_task(self.channel_change_callback(new_channel))

    async def action_toggle_recording(self) -> None:
        if not self.recording_callback:
            return
        ok, message = await self.recording_callback(self.get_active_channel())
        self.notify(message, severity="information" if ok else "warning", timeout=6)
        self._update_status()

    async def action_save_clip(self) -> None:
        if not self.clip_callback:
            return
        ok, message = await self.clip_callback(self.get_active_channel())
        self.notify(message, severity="information" if ok else "warning", timeout=8)

    def action_switch_streamers(self) -> None:
        if len(self.screen_stack) > 1:
            return
        self.push_screen(
            ChannelPickerScreen(initial=", ".join(self.channels),
                                prompt="Which channels do you want to switch to?",
                                favorites=self.favorites, favorites_callback=self._remember_favorites),
            self._on_switch_picked,
        )

    async def _on_switch_picked(self, result: Optional[str]) -> None:
        if result and self.switch_callback:
            await self.switch_callback(result)

    def action_clear_chat(self) -> None:
        channel = self.get_active_channel()
        if channel in self.panes:
            self.panes[channel].clear_messages()
            self.notify(f"Cleared #{channel}", timeout=3)


class ChannelSelectionApp(App[Optional[str]]):
    """Stand-alone channel picker shown at startup."""

    ENABLE_COMMAND_PALETTE = False

    def __init__(self, initial: str = "", favorites: Optional[List[str]] = None,
                 favorites_callback: Optional[Callable[[List[str]], None]] = None):
        super().__init__()
        self.register_theme(theme.make_theme())
        self.theme = "ntwitch"
        self.initial = initial
        self.favorites = favorites
        self.favorites_callback = favorites_callback

    CSS = f"""
    Screen {{
        background: {theme.BG};
    }}
    """

    def on_mount(self) -> None:
        self.push_screen(
            ChannelPickerScreen(initial=self.initial, cancel_label="quit", favorites=self.favorites,
                                favorites_callback=self.favorites_callback),
            self.exit,
        )

    @classmethod
    def get_channels(cls, initial: str = "", favorites: Optional[List[str]] = None,
                     favorites_callback: Optional[Callable[[List[str]], None]] = None) -> Optional[str]:
        """Run the picker; returns the comma-separated live channels, or None if the user quit."""
        return cls(initial, favorites, favorites_callback).run()


class SimpleUI:
    """Plain scrolling output (no panes, no input) for `--simple-ui`."""

    def __init__(self, channels: List[str], is_authenticated: bool = True):
        self.channels = channels
        self.is_authenticated = is_authenticated

    def add_message(self, message: ChatMessage) -> None:
        badge = next((g for b, g, _ in BADGES if b in message.badges), "")
        prefix = f"{badge} " if badge else ""
        print(f"[#{message.channel}] {prefix}{message.author}: {clean_text(message.content)}")

    def set_status(self, message: str, severity: str = "information") -> None:
        print(f"* {message}")

    def start(self) -> None:
        print(f"nTwitch - watching {', '.join(self.channels)}")
        if not self.is_authenticated:
            print("(read-only chat)")
        print("Press Ctrl+C to exit.\n")

    def stop(self) -> None:
        pass
