"""Pure-logic tests: channel parsing, chat formatting/notices, config + credential store, tools."""
import json

import pytest

from ntwitch import bootstrap, credentials, tools
from ntwitch.chat_client import ChatMessage, TwitchIRCClient
from ntwitch.config_manager import ConfigManager
from ntwitch.stream_manager import parse_channel_input
from ntwitch.terminal_ui import format_message, readable_color


# ------------------------------------------------------------------ channel input

@pytest.mark.parametrize("text, expected", [
    ("xqc", ["xqc"]),
    ("XQC, shroud  pokimane", ["xqc", "shroud", "pokimane"]),
    ("https://www.twitch.tv/xqc/videos?x=1, @Shroud #poki twitch.tv/xqc", ["xqc", "shroud", "poki"]),
    ("bad!!, ok_name", ["ok_name"]),
    ("a" * 26, []),  # Twitch names are at most 25 characters
    ("", []),
])
def test_parse_channel_input(text, expected):
    assert parse_channel_input(text) == expected


# ------------------------------------------------------------------ chat

def test_chat_content_is_never_parsed_as_markup():
    msg = ChatMessage("x", "user", "[/bold][red]boom[/] \x1b[31mred", "#0000FF", [])
    plain = format_message(msg).plain
    assert plain.startswith("user: [/bold][red]boom[/] ")      # shown literally, not interpreted
    assert "\x1b" not in plain                                 # terminal escapes cannot get through chat


def test_action_messages_and_mentions():
    action = format_message(ChatMessage("x", "vip", "\x01ACTION waves\x01", None, ["vip"]))
    assert action.plain == "♦ vip waves"
    mention = format_message(ChatMessage("x", "a", "hey @Me look", None, []), my_username="me")
    assert any("on #BABAFF" in str(span.style) for span in mention.spans)


def test_readable_color_lightens_dark_colours_only():
    assert readable_color("#0000FF") != "#0000ff"
    assert readable_color("#ffffff") == "#ffffff"
    assert readable_color("nonsense") is None and readable_color(None) is None


def test_login_failure_notice_reaches_the_ui_and_stops_reconnects():
    seen = []
    client = TwitchIRCClient(["xqc"], lambda m: None, "oauth:x", "me",
                             status_callback=lambda text, level: seen.append((text, level)))
    client._running = True
    client._parse_notice(":tmi.twitch.tv NOTICE * :Login authentication failed")
    assert seen and seen[0][1] == "error" and "--setup" in seen[0][0]
    assert client._running is False


def test_restriction_notice_becomes_a_system_message():
    got = []
    client = TwitchIRCClient(["xqc"], got.append)
    client._parse_notice("@msg-id=msg_banned :tmi.twitch.tv NOTICE #xqc :You are permanently banned")
    assert got and got[0].author == "SYSTEM" and "banned" in got[0].content


# ------------------------------------------------------------------ config + credential store

@pytest.fixture
def fake_store(monkeypatch):
    store = {}
    monkeypatch.setattr(credentials, "available", lambda: True)
    monkeypatch.setattr(credentials, "get_token", lambda user: store.get(user.lower()))
    monkeypatch.setattr(credentials, "set_token", lambda user, tok: store.__setitem__(user.lower(), tok) or True)
    monkeypatch.setattr(credentials, "delete_token", lambda user: store.pop(user.lower(), None))
    return store


def test_token_goes_to_the_credential_store_not_the_file(tmp_path, fake_store):
    cm = ConfigManager(str(tmp_path / "c.json"))
    assert cm.save({"twitch_username": "Me", "twitch_oauth": "oauth:secret", "favorites": ["a"]})
    on_disk = json.loads((tmp_path / "c.json").read_text())
    assert on_disk["twitch_oauth"] == "" and on_disk["token_in_credential_store"] is True
    assert "secret" not in (tmp_path / "c.json").read_text()
    assert cm.load()["twitch_oauth"] == "oauth:secret"


def test_logout_removes_the_stored_token(tmp_path, fake_store):
    cm = ConfigManager(str(tmp_path / "c.json"))
    cm.save({"twitch_username": "me", "twitch_oauth": "oauth:secret"})
    cm.save({"twitch_username": "", "twitch_oauth": ""})
    assert fake_store == {}


def test_plaintext_token_is_migrated(tmp_path, fake_store):
    path = tmp_path / "c.json"
    path.write_text(json.dumps({"twitch_username": "me", "twitch_oauth": "oauth:old"}))
    cm = ConfigManager(str(path))
    assert cm.migrate_secrets() is True
    assert "old" not in path.read_text() and fake_store == {"me": "oauth:old"}


def test_without_a_credential_store_the_file_keeps_the_token(tmp_path, monkeypatch):
    monkeypatch.setattr(credentials, "available", lambda: False)
    monkeypatch.setattr(credentials, "set_token", lambda *a: False)
    cm = ConfigManager(str(tmp_path / "c.json"))
    cm.save({"twitch_username": "me", "twitch_oauth": "oauth:secret"})
    assert cm.load()["twitch_oauth"] == "oauth:secret"
    assert cm.token_is_protected() is False


def test_corrupt_config_means_first_run(tmp_path):
    path = tmp_path / "c.json"
    path.write_text("{not json")
    assert ConfigManager(str(path), use_keyring=False).load() == {}


# ------------------------------------------------------------------ tools / bootstrap

def test_find_vlc_prefers_the_configured_path(tmp_path):
    fake = tmp_path / "vlc.exe"
    fake.write_text("x")
    assert tools.find_vlc(str(fake)) == str(fake)


def test_version_parsing_and_dependencies_present():
    assert bootstrap._version_tuple("8.2.8") == (8, 2)
    assert bootstrap._version_tuple("1") == (1, 0)
    assert bootstrap.missing_packages() == []
