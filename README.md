# nTwitch

Watch Twitch in VLC with live chat in your terminal. Up to 4 channels at once, ad-free via Streamlink.

## Start

**Windows:** double-click `nTwitch.bat`  
**macOS / Linux:** run `sh nTwitch.sh`

That's it. On the first run nTwitch sets itself up: it creates a private Python environment, installs its Python packages, and installs anything missing (VLC, Streamlink, ffmpeg) for you. It then asks one optional question (whether to log in to chat) and shows the channel picker. Later launches start in about a second.

Prefer a normal install? With Python 3.9+:

```bash
pip install .          # adds an `nTwitch` command to your PATH
nTwitch
```

or run it in place with `python nTwitch.py` / `python -m ntwitch`.

> Installing VLC or ffmpeg through winget may show a Windows permission (UAC) prompt. Accept it once.
> On Linux the installer uses `sudo` with your package manager and will ask for your password.

## Using it

Type one or more channels (commas or spaces, or paste a `twitch.tv` link). nTwitch checks which are live, opens a VLC window for each, and shows their chats side by side. The channels you watched last are pre-filled next time.

**Favourites:** press `F2` in the picker to star the channels you typed. Starred channels are listed under the box with a green `● live` mark for those streaming right now. Press `Tab` to move to the list, `Space` to tick a channel, `Enter` to watch.

| Key | Action |
|---|---|
| `F1` / `Tab` | Switch the active channel (the one with the lavender border) |
| `F2` | Start / stop recording the active channel |
| `F3` | Save a clip of the active channel (the first press starts its buffer) |
| `F4` | Switch to different channels |
| `F5` | Settings: video quality, folders, chat login |
| `Ctrl+L` | Clear the active chat |
| `Ctrl+Q` or `Ctrl+C` | Quit and close everything |

Chat markers: `★` streamer, `◆` moderator, `♦` VIP. Messages that mention you are highlighted.

## Sending messages

Chat is read-only until you log in. Press `F5`, run `nTwitch --setup`, or answer yes during first-run setup. nTwitch opens the token page, checks the token with Twitch, and stores it in your **system credential store** (Windows Credential Manager, macOS Keychain, Secret Service on Linux). If your system has none, the token falls back to your config file in plain text and nTwitch tells you so. Tokens from older versions are moved to the credential store automatically.

## Options

```bash
nTwitch xqc shroud                  # skip the picker
nTwitch --channels xqc --quality 720p60
nTwitch xqc --no-chat               # video only
nTwitch xqc --anon                  # read-only chat even if logged in
nTwitch --simple-ui xqc             # plain scrolling chat, no panes
nTwitch --setup                     # chat login, save folders
nTwitch --debug                     # write diagnostics to ~/.twitch-terminal.log
```

(`nTwitch` means `nTwitch.bat` / `sh nTwitch.sh` / `python nTwitch.py`, or the installed command.)

Quality: `best` (default), `1080p60`, `720p60` (good for 3+ streams), `720p`, `480p`, `360p`. Each stream at `best` uses about 6-8 Mbps. Change it any time in `F5` settings; saving restarts the video windows so it takes effect.

## Recordings and clips

- **Recordings** go to `~/Videos/TwitchRecordings/<channel>/`. They are written as `.ts` while recording (safe if the app is killed) and converted to `.mp4` when you stop.
- **Clips** go to `~/Videos/TwitchClips/<channel>/`. The first `F3` press starts a rolling buffer of the active channel; every later press saves everything buffered, up to the last 10 minutes (at least 10 seconds are needed). The buffer lives in short segment files that are deleted as they age out, so disk use stays bounded. Saved as `.mp4` (ffmpeg is installed for you; without it you get `.ts`).

Folders can be changed in `F5` settings or with `nTwitch --setup`.

## Troubleshooting

| Problem | What to do |
|---|---|
| "Offline, or the channel doesn't exist" | Twitch gives the same answer for both. Check the spelling, or try later. |
| A tool could not be installed | nTwitch prints the one-line command to install it yourself, then start it again. |
| Chat says login failed | Your token expired. Press `F5` (or run `nTwitch --setup`) and log in again. |
| A video window closed | nTwitch doesn't reopen windows you close. Restart it (or press `F4`) to bring it back. |
| Anything else | Run with `--debug` and check `~/.twitch-terminal.log`. |

Use a modern terminal for the best look (Windows Terminal, iTerm2, any recent Linux terminal). A window at least 120 columns wide fits 4 channels comfortably. Set `NO_COLOR=1` for a monochrome UI.

## Configuration

`~/.twitch-terminal-config.json`, normally managed from `F5` settings or `nTwitch --setup`:

```json
{
  "vlc_path": "C:/Program Files/VideoLAN/VLC/vlc.exe",
  "twitch_username": "",
  "twitch_oauth": "",
  "streamlink_quality": "best",
  "recording_path": "C:/Users/You/Videos/TwitchRecordings",
  "clips_path": "C:/Users/You/Videos/TwitchClips",
  "last_channels": "xqc, shroud",
  "favorites": ["xqc", "shroud"]
}
```

`twitch_oauth` stays empty when the token is in the credential store.

## Development

```bash
pip install -e ".[dev]"
python -m pytest                    # no network needed
python build_scripts/build.py       # standalone dist/nTwitch.exe via PyInstaller
```

Code lives in the `ntwitch/` package (`cli.py` is the entry point). Tests run on Linux and Windows in GitHub Actions.

## Known limitations

- Text-only chat (no emote images); live streams only (no VODs)
- Max 4 channels (panes); Twitch limits chat to 20 messages per 30 seconds
- Chat login uses a token from a third-party generator, since Twitch requires every app to register its own client ID
- Twitch's own stream/chat endpoints can change; update Streamlink if streams stop opening (`pip install -U streamlink` inside `.venv`)

## Credits

[Streamlink](https://streamlink.github.io/) · [Textual](https://textual.textualize.io/) · [Rich](https://github.com/Textualize/rich) · [VLC](https://www.videolan.org/)

MIT License. Not affiliated with Twitch Interactive, Inc.; respect Twitch's Terms of Service.
