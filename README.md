<p align="center"><img src="assets/logo.png" alt="Omnigram logo" width="160"></p>

# Omnigram

A cross-platform (Windows / macOS / Linux) desktop app for managing your own Telegram accounts, built with PySide6 and Telethon.

> [!IMPORTANT]
> **Omnigram is an educational project.** It exists to demonstrate the Telegram MTProto API, Telethon, session formats and desktop GUI design in Python. It is intended **for personal use only, on Telegram accounts that you personally and legally own.** Do not use it on accounts, chats, channels or data you are not authorized to manage.

## Features

- **Import** accounts from Telethon or Pyrogram `.session` files, or from a Telegram Desktop `tdata/` folder (including passcode-protected ones), or log in with a phone number.
- **Accounts** — status checks, spam check, statistics, dashboard, session backup and restore, 2FA manager, active sessions, profile editor.
- **Proxies** — per-account proxies, a proxy pool with ping and exit-IP geo lookup, even distribution across accounts.
- **Content & chats** — templates, scheduled posts, forwarding, chat cleanup, auto-responder, word monitoring.
- **Converters** — tdata → Telethon session, Pyrogram → Telethon session.

Accounts and their metadata are stored locally in the platform's app-data folder; nothing is sent anywhere except to Telegram (optionally through your proxy).

## Requirements

- Python 3.11+
- [uv](https://docs.astral.sh/uv/)
- Your own Telegram API credentials (`api_id` / `api_hash` from <https://my.telegram.org>), unless an imported session JSON already carries them.

## Usage

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
