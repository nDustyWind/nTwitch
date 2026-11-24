"""Headless Textual tests (no terminal, no network)."""
import asyncio

import pytest

from ntwitch.chat_client import ChatMessage
from ntwitch.cli import TwitchViewer
from ntwitch.stream_manager import StreamManager, StreamStatus
from ntwitch.terminal_ui import (ChannelPane, ChannelPickerScreen, ChannelSelectionApp, MultiChannelUI,
                                 SettingsScreen)
from ntwitch.tools import ToolSet


def fake_status(live):
    def check_many(self, channels):
        return {c: ((StreamStatus.LIVE, "Live") if c in live else (StreamStatus.OFFLINE, "Offline"))
                for c in channels}
    return check_many


def run(coro):
    return asyncio.run(coro)


# ------------------------------------------------------------------ main UI

def test_main_ui_layout_cycling_and_dialogs():
    async def go():
        ui = MultiChannelUI(["xqc", "shroud", "poki"], True, username="me")
        ui.add_message(ChatMessage("xqc", "early", "sent before the UI existed", None, []))
        async with ui.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            panes = list(ui.query(ChannelPane))
            assert len(panes[0].messages) == 1, "early messages must be buffered, not lost"
            assert panes[0].has_class("active")

            await pilot.press("f1")
            assert panes[1].has_class("active") and not panes[0].has_class("active")
            await pilot.press("tab")
            assert panes[2].has_class("active")

            # no widgets overlap vertically
            regions = [ui.query_one(s).region for s in ("TitleBar", "#channels", "#chat-input", "StatusBar", "Footer")]
            for upper, lower in zip(regions, regions[1:]):
                assert upper.y + upper.height <= lower.y

            panes[1].set_recording(True, "00:05")
            assert panes[1].border_subtitle == "● REC 00:05"

            await pilot.press("f4")
            assert isinstance(ui.screen, ChannelPickerScreen)
            await pilot.press("tab")  # moves focus in the dialog; must not switch channels
            assert panes[2].has_class("active")
            await pilot.press("escape")
            await pilot.press("ctrl+c")
    run(go())


def test_anonymous_ui_has_no_input_box():
    async def go():
        ui = MultiChannelUI(["xqc"], False)
        async with ui.run_test(size=(100, 24)) as pilot:
            await pilot.pause()
            assert not ui.query("#chat-input")
    run(go())


# ------------------------------------------------------------------ picker + favourites

def test_picker_returns_live_channels_and_manages_favourites(monkeypatch):
    monkeypatch.setattr(StreamManager, "check_many", fake_status({"xqc"}))
    saved = []

    async def go():
        app = ChannelSelectionApp(initial="", favorites=["shroud", "xqc"], favorites_callback=saved.append)
        async with app.run_test(size=(100, 45)) as pilot:
            await pilot.pause(0.5)
            box = app.screen.query_one("#favorites")
            entry = app.screen.query_one("#channel-input")
            assert [o.value for o in box.options][0] == "xqc", "live favourites are listed first"

            entry.value = "shroud"
            await pilot.pause()
            assert set(box.selected) == {"shroud"}, "typing a favourite ticks it"

            await pilot.press("tab")      # focus the list (highlight is on xqc)
            await pilot.press("space")
            await pilot.pause()
            assert "xqc" in entry.value and "shroud" in entry.value, "ticking adds it to the box"

            entry.value = "poki"
            await pilot.pause()
            await pilot.press("f2")
            assert saved[-1] == ["shroud", "xqc", "poki"], "F2 stars the typed channel"
            await pilot.press("f2")
            assert saved[-1] == ["shroud", "xqc"], "F2 again un-stars it"

            entry.value = "xqc, shroud"
            entry.focus()
            await pilot.press("enter")
            await pilot.pause(2.6)        # one channel is offline, so the dialog waits before continuing
        assert app.return_value == "xqc", "only live channels are returned"
    run(go())


def test_picker_rejects_junk_and_all_offline(monkeypatch):
    monkeypatch.setattr(StreamManager, "check_many", fake_status(set()))

    async def go():
        app = ChannelSelectionApp(initial="!!!")
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            assert "doesn't look like" in str(app.screen.query_one("#results").render())
            app.screen.query_one("#channel-input").value = "nobody"
            await pilot.press("enter")
            await pilot.pause(0.5)
            assert "None of these are live" in str(app.screen.query_one("#results").render())
            await pilot.press("escape")
        assert app.return_value is None
    run(go())


# ------------------------------------------------------------------ settings

def test_settings_screen_returns_values_and_validates_folders(tmp_path):
    results = []

    async def on_saved(values):
        results.append(values)

    async def go():
        ui = MultiChannelUI(["xqc"], False)
        ui.set_callbacks(None, settings_provider=lambda: {
            "streamlink_quality": "best", "recording_path": str(tmp_path / "rec"),
            "clips_path": str(tmp_path / "clips"), "twitch_username": "", "protected": False,
        }, settings_callback=on_saved)
        async with ui.run_test(size=(110, 50)) as pilot:
            await pilot.pause()
            await pilot.press("f5")
            await pilot.pause()
            assert isinstance(ui.screen, SettingsScreen)
            ui.screen.query_one("#quality").value = "720p60"
            ui.screen.query_one("#rec-path").value = str(tmp_path / "new rec folder")
            await pilot.click("#save")
            await pilot.pause(0.5)
            assert not isinstance(ui.screen, SettingsScreen)
    run(go())
    assert results and results[0]["streamlink_quality"] == "720p60"
    assert (tmp_path / "new rec folder").is_dir()
    assert "twitch_username" not in results[0], "login fields are only sent on login/logout"


def test_applying_settings_persists_changes_and_restarts():
    from rich.console import Console
    saved = []
    viewer = TwitchViewer(Console(quiet=True), {"twitch_username": "", "recording_path": "r", "clips_path": "c"},
                          ["xqc", "shroud"], ToolSet(), quality="best", persist=saved.append)
    run(viewer._apply_settings({"streamlink_quality": "720p", "recording_path": "r", "clips_path": "c"}))
    assert saved == [{"streamlink_quality": "720p"}]
    assert viewer.settings_applied and viewer.switch_channels == ["xqc", "shroud"]

    nothing = TwitchViewer(Console(quiet=True), {"twitch_username": "", "recording_path": "r", "clips_path": "c"},
                           ["xqc"], ToolSet(), quality="best", persist=saved.append)
    run(nothing._apply_settings({"streamlink_quality": "best", "recording_path": "r", "clips_path": "c"}))
    assert not nothing.settings_applied and nothing.switch_channels is None

    login = TwitchViewer(Console(quiet=True), {"twitch_username": "", "recording_path": "r", "clips_path": "c"},
                         ["xqc"], ToolSet(), quality="best", persist=saved.append)
    run(login._apply_settings({"streamlink_quality": "best", "twitch_username": "me", "twitch_oauth": "oauth:t"}))
    assert saved[-1] == {"twitch_username": "me", "twitch_oauth": "oauth:t"}
