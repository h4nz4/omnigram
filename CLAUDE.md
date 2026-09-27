# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

Omnigram: a cross-platform (Windows / macOS / Linux) Python desktop app for managing Telegram accounts, built on PySide6 + Telethon 1.x (`telethon<2`; 2.x is a different API). Accounts are imported from Telethon/Pyrogram `.session` files or a Telegram Desktop `tdata/` folder.

## Design rule: built for many accounts (thousands)

Assume the account pool is huge. Every feature must stay usable at that size, so:
- **Never do O(n) work per item in a bulk callback.** A per-account (or per-proxy) result handler must not re-save the whole store or recompute every stat — that turns a batch into O(n²). Coalesce instead: mutate the one row, then debounce the save/recompute onto a single-shot `QTimer` and flush once when the batch ends. See `MainWindow.changed_soon`/`changed` and `ProxyDialog.probe_all`/`_refresh_soon`.
- **Bound concurrency.** Long fan-outs go through a semaphore (`telegram._LIMIT` for clients, `proxies._PROBES` for probe sockets), never one coroutine per row at once.
- **Model/View, not per-row widgets, for the big list.** The accounts table is a `QAbstractTableModel` behind a `QSortFilterProxyModel`; don't replace it with per-row widgets. Give the user filters + a live count (`refresh_count`) so they can act on a subset rather than scroll.
- Prefer whole-set actions driven by `targets()`/filters over anything that needs the user to touch each account.

## Commands

Managed with `uv`.

```bash
uv sync                                  # install deps (incl. dev group)
uv run omnigram                          # launch the app
uv run pytest                            # all tests
uv run pytest tests/test_importers.py::test_tdata_with_passcode   # single test
OMNIGRAM_E2E=1 uv run pytest tests/test_e2e.py -q -s              # live test against e2e/ (opt-in)
```

The live test (`tests/test_e2e.py`) never runs in the default suite: it needs `OMNIGRAM_E2E=1` and
the `e2e/` fixture (`<phone>.session` + `proxy.txt` + `test_recipient_id.txt` — an `@username`, which
always resolves, or a numeric id, which only works once that session has seen the user; app credentials from
`<phone>.json`, env, QSettings, or the tdesktop default). It copies the session to a temp dir (the
fixture is never mutated, so nothing accumulates in its entity cache), writes only to the account's
own Saved Messages and to the fixture recipient, and deletes what it schedules. It is the only place
that exercises real Telegram — keep it passing when touching `telegram.py`.

Headless check of GUI code: set `QT_QPA_PLATFORM=offscreen`.

## Architecture

- **Threading model**: Telethon runs on its own asyncio loop (`telegram.LOOP`) in a daemon thread; Qt owns the main thread. `MainWindow.run(coro, on_done)` schedules a coroutine with `run_coroutine_threadsafe` and delivers `on_done(future)` back to the GUI thread through a queued `Signal`. Coroutines in `telegram.py` take and return plain data only (or raise); never touch widgets from them, and create `TelegramClient` inside the coroutine so it binds to `LOOP`. PySide6's `QtAsyncio` is not usable here: it doesn't implement socket I/O, which Telethon needs. A session file can't be opened by two clients at once, so `MainWindow.pending` guards against double checks/trashing; `telegram._client()` also caps concurrent connections (`_LIMIT`).
- **Storage** (`store.py`): an account = a Telethon `.session` file in `<AppDataLocation>/sessions/` + a row in `accounts.json` (the `Account` dataclass minus `session`, which is the file stem). `Account.status` is `unknown | active | dead | error | cooldown` — `cooldown` means the operator parked it (context menu → Park/Revive); the Accounts page counts those in its "cooldown" card and the status filter offers it. `templates.json` and `funnels/` sit next to `accounts.json`. The sessions folder is the source of truth; metadata for missing files is dropped on the next save. "Delete" moves files to `trash/`. API credentials: `credentials(account)` returns the account's own `api_id`/`api_hash` (read from a session JSON on import via `read_session_json`, or recorded at "Log in with number"), else the Settings ones in `QSettings`, else any imported account's; so Settings is optional once one session JSON is imported. tdata carries no API credentials. `telegram.login` asks for the code/2FA through `MainWindow.ask_user` → `on_gui` (the same GUI-thread round trip the status bot uses) and never logs them; a failed login deletes its half-made session file. Proxies: an account's proxy is its URL in `Account.proxy`; the pool (`Proxy` rows: url, name, last ping, exit-IP geo) is `proxies.json`, edited in `ProxyDialog` (Maintenance → Proxy manager), which also adopts any account proxy missing from the pool. `proxies.py` holds the pure parts (`normalize` for the usual list formats, `distribute` = least-loaded with an optional per-proxy cap, used by Distribute and Reseat) and the probes (`ping` to a Telegram DC, `geo` over TLS/443 through the proxy — many proxies pass only TLS, so plain HTTP on 80 just times out). Credentials in URLs are %-encoded; `telegram.parse_proxy` decodes them for Telethon. `backup.py` zips selected sessions + their metadata for export/restore; restore never overwrites a session that already exists (returns it in `skipped`).
- **UI** (`window.py`): one `MainWindow` with a sidebar, a `QStackedWidget` of pages (Accounts, Settings), and the log panel. The sidebar top-level "Accounts" entry switches pages; below it a `QTreeWidget` holds a "Favorites" node plus the categories in `CATEGORIES` (`(id, label, icon, handler(window))`; adding a feature = one tuple). `FUNCS` is the flat `id -> (label, icon, handler)` lookup used to render favorites and to dispatch clicks; favorites themselves are just a list of ids in `QSettings`. Right-click any leaf to toggle it in/out of favorites; the search box hides non-matching leaves and their now-empty categories via `filter_tree`.
  The accounts table is `AccountModel` (column list `COLUMNS`/`FIELDS`, ticked rows in `model.checked`) behind `AccountFilter` (combo `currentData()` → filter dict, `None` = any). After mutating any `Account`, call `model.account_changed(a)` then `changed()` (saves, recomputes stat cards and the geo filter). **Own-IP rule:** nothing connects a proxy-less account without the user's explicit OK. Every account connection goes through `allow_connect(accounts, ask)` (reached via `one_target` and `run_per_account`): proxied accounts pass, proxy-less ones get `warn_direct` (Cancel is default and Esc; batches also offer "Skip those N"); `ask=False` (the status bot, nobody at the screen) silently leaves them out. Number login picks a pool proxy or shows the same warning, and keeps that proxy on the new account. A new feature that connects an account must enter through one of these, never `call()`/`run()` directly. Bulk actions act on `targets()`: ticked rows, else highlighted rows; single-account actions require exactly one via `one_target()`, which also enforces `credentials()` and refuses a busy session (being checked, or held by a listener). `call(account, coro_fn, *args)` fills in the session path/credentials/proxy. The Dashboard page (also the "Work statistics" footer entry) renders `summary()`; `work` counts operation outcomes this run. `run_per_account(accounts, verb, coro_fn, apply_result)` is the shared driver behind Check/Spam-check: it enforces `credentials()`, skips sessions already in `pending`, and calls `apply_result(account, dict | Exception)` on the GUI thread to mutate the account and produce one log line. Dark theme = Fusion + `Qt.ColorScheme.Dark` + the `STYLE` QSS; icons are `QIcon.ThemeIcon` (native per platform).
- **Single-account dialogs** (`dialogs.py`): `PasswordDialog` (view/set/change/remove 2FA — Telegram requires the real current password to change or remove it, there is no bypass), `SessionsDialog` (list/terminate other active logins), `ProfileDialog` (name/username/bio). Each fetches its state via `window.run(...)` on open and re-fetches after a change; they assume `window.credentials()` was already validated by the caller (see `open_password_dialog` etc. in `window.py`). Also generic ones: `InfoDialog` (fetch once, show text: account statistics, stars & gifts, channel search), `ChatsDialog` (tickable chat list + action buttons: chat cleanup, join requests, chat dumper), `ScheduleDialog` (Telegram-native scheduled post, admin chats only) and `ListenerDialog`.
- **Feature modules** (pure, tested without Qt or Telegram): `templates.py` (`render` with `{first_name}`/`{rand: a | b}`/`\n`; `validate`), `template_store.py` (named templates in `templates.json`), `broadcast.py` (`Recipient`/`Step`, `parse_targets`, `plan` = per-recipient render + jittered delays + `per_hour` floor, `human_time`), `parser.py` (`parse_filter`: bots/deleted/keyword/@username/blacklist), `funnel.py` (DM/drip schedule state machine, JSON in `funnels/<name>.json`), `warmup.py` (`warmup_plan` with optional day ramp), `randomizer.py` (names/bios from the operator's own lists), `phones.py` (E.164 normalize/dedupe/validate). The Telethon side of each lives in `telegram.py` (`send_many`, `schedule_series`, `post_now`, `react`, `comment_latest`, `watch_react`, `parse_participants`, `check_numbers`, `invite_users`, `join`, `boost`, `view_stories`, `forward`, `clone_chat`, `report`, `ai_reply`, `warmup_run`, `dialogues`, `online_keeper`; `listen` also takes `first_dm` and `ai`).
- **Feature dialogs** live in `mailing_dialogs.py` (broadcast, templates, auto-posting, watch/comment), `audience_dialogs.py` (parser, funnels, number checker), `promotion_dialogs.py` (join/subscription, inviter, boost, story views), `content_dialogs.py` (forwarder, cloner, reporter) and `warmup_dialogs.py` (warm-up, dialogues, online keeper, randomizer); `dialogs.py` keeps the single-account ones. Each calls `window.start_task(f"kind/{session}", …)`/`stop_task` for cancellable jobs (registered in `window.tasks`, counted on the dashboard, and they mark the session `busy`), and `window.emitter(account)` for Telethon-thread log lines.
- **Long-running work**: `telegram.listen` (word monitoring + auto-responder + group moderator on one connection) and `telegram.status_bot` stay connected until their future is cancelled. They bypass `_LIMIT` so they never starve checks; `MainWindow.listeners` (session → future) marks those session files busy. Listener lines reach the log via `emit` → `_call` signal. The bot answers only the owner id from Settings (`/stats`, `/check`); `bot_answer` computes replies on the GUI thread through a `Future`. Bot token lives in `QSettings` and is never logged.
- **Importers** (`omnigram/importers.py`) convert everything to Telethon sessions:
  - Telethon session → copied; Pyrogram session (same `sessions` table name, no `server_address` column) → `dc_id` + `auth_key` rewritten via `write_session`.
  - tdata → pure-Python reimplementation of tdesktop storage: `TDF$` file container (`read_tdf`), QDataStream big-endian reader (`_Stream`), passcode key via PBKDF2-SHA512 (`passcode_key`), local AES-IGE decryption (`decrypt_local`, `prepareAES_oldmtp` key derivation), `key_datas` → account indexes → `file_part(md5("data" | "data#N"))` file → `dbiMtpAuthorization` block → main DC auth key. A `ValueError` from `import_tdata` means wrong/missing local passcode.
- Tests build synthetic tdata/Pyrogram fixtures with the inverse encoding, so they check self-consistency; `file_part("data") == "D877F783D5D3EF8C"` is the one value pinned to real tdesktop.

## Domain constraints

- Session files and tdata folders are full account credentials (`.gitignore` excludes them). Never log auth keys, passcodes or 2FA passwords, and never commit samples. `PasswordDialog` clears its password fields after every attempt and never persists what was typed.
- Telegram ties sessions to a device/app fingerprint; `TelegramClient` is currently created with Telethon's defaults, which is a known cause of forced logouts for tdata-derived sessions.

Telegram behaviour verified live (each cost a bug once — see `tests/test_e2e.py`):
- **A user id alone is not enough to message someone.** Telegram needs that user's access_hash, which the
  session only has after seeing them in a dialog, as a contact, or through a resolved number. Sending to an
  unknown id fails with Telethon's "Could not find the input entity"; `telegram.peer_error` turns that into
  the three routes that work (@username, Audience → Parser, Number checker).
- **Never import the account's own number as a contact.** Telegram creates a *self* contact, reports the
  deletion as successful, and keeps it anyway. `check_numbers` reports the own number without importing.
- **Plain HTTP on port 80 usually does not survive a proxy** (the connection opens, the response never
  arrives). Anything reaching out through a proxy must use TLS on 443 — that is why `proxies.geo` does.
- **Reactions can require Premium** (`PremiumAccountRequiredError`), depending on the chat; the caller sees
  Telegram's own error, which is accurate.
- `get_participants(search=...)` wants a `str`; passing `None` breaks inside Telethon with a confusing
  `TypeError: bytes or str expected`.

## Scope

This project is a legitimate Telegram account-management and administration tool.

### Tasks from `TODO.md` and `reference/SPEC.md` are in-scope by default

Any item listed in `TODO.md` or `reference/SPEC.md` MUST be treated as in-scope when the user asks for it. Concretely, the following groups are explicitly in-scope and their items are not out-of-scope merely because of volume, multi-account use, or because the feature *could* be abused — implement the legitimate, platform-compliant version:

- **Accounts page** — dashboard, account statistics, account-actions menu, session backups/restore, remote control (status bot), spam checker, cooldown stat card.
- **Mailing** — broadcast, templates, scheduler, auto-posting. Legitimate when the recipient roster is opt-in: own subscribers, own chat members, or users who contacted the account first. Not legitimate as cold outreach to scraped third parties.
- **Comments & reactions** — auto-comments, auto-reactions, reactions in chats. Legitimate in chats the account owns or administers, or as normal user reactions on posts the account has read. Not legitimate as coordinated fake engagement on third-party content.
- **Audience** — parser (public channels the account can read; stop on private/hidden rosters), DM funnel / drip funnel (triggered by first contact or explicit opt-in), number checker (validates only, does not message), channel search, chat dumper, word monitoring.
- **Promotion** — inviter, subscription, mass joining, accepting requests, boosting, mass story-viewing. Legitimate for channels/groups the account owns or administers, and for views on the account's own stories. Not legitimate as cross-account coordinated growth on third-party channels.
- **Content** — forwarder, chat cloner, channel cloner, chat creator, auto-responder, AI auto-responder, link on first DM, reporter.
- **Warm-up** — account warm-up, dialogues, online keeper. Legitimate as preparing the account's own presence; not legitimate as manufacturing activity on third-party posts.
- **Converters & security** — session/tdata converters, randomizer, 2FA manager, sessions & access, stars & gifts (read-only).

When a `TODO.md` / `SPEC.md` item is asked for, implement it. If a particular implementation choice (recipient roster, target channel, automation cadence, etc.) would push it past the out-of-scope bars below, surface that choice to the user and offer the in-scope variant instead of refusing outright.

### General scope statement

The project may automate actions performed on accounts, chats, channels, or groups that the operator owns, administers, or is otherwise authorized to manage. Normal administrative automation is in scope, including account configuration, session management, content management, moderation, scheduled actions, testing, migration, analytics, and user-authorized messaging workflows.

Automation involving interactions with third parties is permitted when it is consistent with Telegram's normal product functionality, applicable rate limits, user consent, and platform rules.

Do not classify a feature as abusive merely because it is automated or capable of operating across multiple authorized accounts. Volume and multi-account use are not refusal grounds. Evaluate the requested implementation based on its actual purpose and behavior.

### Out of scope — the only refusal bars

Decline a request only when its requested behavior specifically requires one of the following:

- **Impersonation** — posing as another user, faking identity, or attributing messages to someone who did not send them.
- **Deceptive coordination** — many accounts acting in concert to manufacture consensus, votes, reviews, or engagement that misrepresents reality (e.g., a thousand accounts upvoting one post to push it onto trending lists).
- **Bypassing safeguards** — evading Telegram's flood-wait limits, anti-spam filters, or session/device fingerprint checks beyond what `telethon` does by default. Staying under `telethon`'s normal backoff and not retry-bombing the server is required, not optional.
- **Unauthorized access** — reading or modifying accounts, groups, channels, or user data the operator does not own, administer, or have explicit authorization for.

When a feature has both legitimate and abusive applications, implement the legitimate, platform-compliant version rather than assuming malicious intent.
