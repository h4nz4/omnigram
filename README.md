<p align="center"><img src="assets/logo.png" alt="Omnigram logo" width="160"></p>

<p align="center">
  <a href="LICENSE"><img src="https://img.shields.io/github/license/h4nz4/omnigram?color=blue" alt="License: MIT"></a>
  <a href="pyproject.toml"><img src="https://img.shields.io/badge/python-3.11%2B-3776AB?logo=python&logoColor=white" alt="Python 3.11+"></a>
  <a href="#download"><img src="https://img.shields.io/badge/platform-Windows%20%7C%20macOS%20%7C%20Linux-555" alt="Windows | macOS | Linux"></a>
  <a href="https://doc.qt.io/qtforpython-6/"><img src="https://img.shields.io/badge/GUI-PySide6%20(Qt%206)-41CD52?logo=qt&logoColor=white" alt="PySide6 (Qt 6)"></a>
  <a href="https://docs.telethon.dev/"><img src="https://img.shields.io/badge/Telethon-MTProto-26A5E4?logo=telegram&logoColor=white" alt="Telethon"></a>
  <a href="#features"><img src="https://img.shields.io/badge/AI-replies%20%2B%20Jev%20decisions-8A2BE2" alt="AI replies with Jev decisions"></a>
  <a href="https://github.com/astral-sh/uv"><img src="https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/uv/main/assets/badge/v0.json" alt="uv"></a>
  <a href="https://github.com/h4nz4/omnigram/commits/main"><img src="https://img.shields.io/github/last-commit/h4nz4/omnigram" alt="Last commit"></a>
</p>

# Omnigram

A cross-platform (Windows / macOS / Linux) desktop app for managing your own Telegram accounts, built with PySide6 and Telethon.

> [!IMPORTANT]
> **Omnigram is an educational project.** It exists to demonstrate the Telegram MTProto API, Telethon, session formats and desktop GUI design in Python. It is intended **for personal use only, on Telegram accounts that you personally and legally own.** Do not use it on accounts, chats, channels or data you are not authorized to manage.

## Features

- **Import** accounts from Telethon or Pyrogram `.session` files, or from a Telegram Desktop `tdata/` folder (including passcode-protected ones), or log in with a phone number.
- **Accounts** — status checks, spam check, statistics, dashboard, session backup and restore, 2FA manager, active sessions, profile editor.
- **Proxies** — per-account proxies, a proxy pool with ping and exit-IP geo lookup, even distribution across accounts.
- **AI replies** — per chat: Draft (the AI suggests, you send) or Auto (it answers with human pacing), writing as you in your languages and style. A chat model (OpenRouter or any OpenAI-compatible endpoint) writes; [Jev](https://typesafe.ai) decides — the contact's language and mood, whether to answer, when to hand the chat back to you, and whether a draft is safe to send. A background autopilot keeps Auto chats answered with the chat window closed; in groups it speaks only when addressed.
- **Server mode** — keep accounts working while the app, or your computer, is off: the app installs Omnigram on a Linux server you control over SSH, and warm-ups, the AI autopilot, funnels, listeners, the online keeper and the status bot run there. See [Server mode](#server-mode).
- **Warm-up** — ramped, multi-day own-presence activity inside each account's local active hours.
- **Content & chats** — templates, scheduled posts, forwarding, chat cleanup, auto-responder, word monitoring.
- **Converters** — tdata → Telethon session, Pyrogram → Telethon session.

Accounts and their metadata are stored locally in the platform's app-data folder; nothing is sent anywhere except to Telegram (optionally through your proxy).

## Download

Ready-to-run builds for each platform are on the [Releases](https://github.com/h4nz4/omnigram/releases) page — no Python needed:

| Platform | File |
|---|---|
| Windows x64 | `Omnigram-<version>-windows-x64.zip` — unzip, run `Omnigram.exe` |
| macOS Apple Silicon | `Omnigram-<version>-macos-arm64.dmg` |
| macOS Intel | `Omnigram-<version>-macos-x64.dmg` |
| Linux x64 | `Omnigram-<version>-linux-x64.AppImage` — `chmod +x` it, then run it |

The builds are **not code-signed**, so your OS will warn the first time:

- **Windows** — SmartScreen shows "Windows protected your PC": click **More info → Run anyway**.
- **macOS** — the app is blocked on first open: go to **System Settings → Privacy & Security** and click **Open Anyway** (on older macOS, right-click the app → **Open**).
- **Antivirus** — a tool that reads `tdata` and `.session` files can be flagged heuristically. If you'd rather not trust a binary, run from source (below); it's the same code.

You'll need your own Telegram API credentials (`api_id` / `api_hash` from <https://my.telegram.org>), unless an imported session JSON already carries them.

## Run from source

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
```

```bash
uv run omnigram
```

Run the tests:

```bash
uv run pytest
```

Build a portable app for your current OS into `dist/` (the same script the release pipeline runs on each platform):

```bash
uv run --isolated --python 3.13 --extra gui --group build python build.py
```

Releases: bump `version` in `pyproject.toml`, commit, and push a matching tag (`v0.2.0`). CI tests, builds all four platforms and leaves a draft release to review and publish.

## Server mode

Sidebar → **Server**: enter the server's address, SSH port, user and your SSH **private key file**, then **Install /
update** and **Connect**. Right-click accounts → **Move to server…**; they run only there from then on, and
**Move back to this computer…** brings them home with their running jobs.

- **Requirements**: a Linux server with systemd, reachable with an SSH key (no passwords). The app uses your
  system's `ssh`/`scp`, installs [uv](https://docs.astral.sh/uv/) and Omnigram (without the desktop GUI) for that
  user, and runs it as a systemd user service. To keep it running after you log out of the server, lingering must
  be on — the installer tries, and if the server refuses, an admin runs `sudo loginctl enable-linger <user>`.
- **First connection**: the app shows the server's SSH host key fingerprints; compare them with what your hosting
  provider shows before trusting them.
- **What the server gets**: moved accounts' session files (full access to those accounts), their AI settings and
  saved jobs, your AI key and the status bot's token. **Anyone with access to the server has access to these
  accounts** — use a server only you control. The app asks you to confirm this before the first move.
- **Network**: the server opens no port. Its API listens on a private socket readable only by its user, and the
  app reaches it through an SSH tunnel, so your SSH key is the only credential.
- **One place at a time**: Telegram ends a session used from two places at once, so an account runs either here or
  on the server. The session stays here as a locked backup the app won't use while the account is on the server;
  if the server is gone for good, **Move back** offers to force it back.
- **Proxies**: every account on a server needs one — without it Telegram would see the server's IP.
- **Backups**: Server → **Download backup…** saves the server's accounts, AI settings and jobs; **Restore sessions**
  reads it.

Several of your computers can connect to the same server at once. Running only the server, without the desktop
app: `omnigram serve` (see `omnigram serve --help`).

## Responsible use

You are solely responsible for how you use this software. By using it you agree to:

- use it only with accounts you own and have the legal right to control;
- follow [Telegram's Terms of Service](https://telegram.org/tos) and API Terms, and all laws that apply to you;
- not use it for spam, unsolicited messaging, impersonation, fake engagement, evading Telegram's limits, or accessing anyone else's accounts or data.

Session files and `tdata` folders are full credentials to an account. Keep them private and never share or commit them.

## Disclaimer

This software is provided "as is", without warranty of any kind. **The author waives all liability** for any misuse of this software and for any damage, account restriction, ban, data loss or legal consequence arising from its use. Use it at your own risk.

This project is not affiliated with, endorsed by, or connected to Telegram.

## License

Released under the [MIT License](LICENSE) © 2026 h4nz4.
