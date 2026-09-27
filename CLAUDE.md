# CLAUDE.md

Omnigram: cross-platform PySide6 desktop app for managing Telegram accounts, on Telethon 1.x (`telethon<2`; 2.x is a different API). Accounts come from Telethon/Pyrogram `.session` files, a Telegram Desktop `tdata/` folder, or number login.

## Commands

```bash
uv sync                                   # deps incl. dev group
uv run omnigram                           # launch
uv run pytest                             # tests (offline)
uv run pytest tests/test_importers.py::test_tdata_with_passcode
OMNIGRAM_E2E=1 uv run pytest tests/test_e2e.py -q -s   # live, opt-in
OPENROUTER_API_KEY=... uv run pytest tests/test_ai_live.py -q   # Jev decision cases, opt-in, read-only
OPENROUTER_API_KEY=... OMNIGRAM_E2E=1 uv run pytest tests/test_ai_e2e.py -q -s   # real model + Jev + Auto on Telegram (Saved Messages only)
uv run --isolated --python 3.13 --group build python build.py   # portable app for this OS into dist/
```

**Releases.** `.github/workflows/release.yml` runs on a `v*` tag that must equal `pyproject.toml`'s version: tests on Linux, `build.py` on Windows x64 / macOS arm64 / macOS Intel / Linux x64, then a *draft* GitHub Release the maintainer writes up and publishes. `build.py` calls Nuitka with its PySide6 plugin directly (what `pyside6-deploy` wraps) on Python 3.13, because Nuitka 4.1.1 only experimentally supports 3.14. Builds are unsigned. Anything loaded by name at runtime (data packages, plugins) needs an explicit `--include-…` there — see `phonenumbers` and `tzdata`.

Headless GUI check: `QT_QPA_PLATFORM=offscreen`.

`tests/test_e2e.py` is the only test that hits real Telegram; keep it passing when touching `telegram.py`. It needs `e2e/`: `<phone>.session`, `proxy.txt`, `test_recipient_id.txt` (prefer an `@username`; a numeric id only works once the session has seen that user). App credentials come from `<phone>.json`, env, QSettings, or the tdesktop default. It works on a temp copy of the session, writes only to Saved Messages and the recipient, and deletes what it schedules.

## Scale rule: thousands of accounts

- **No O(n) work per item in a bulk callback.** Mutate the one row, then debounce save/recompute on a single-shot `QTimer` (`MainWindow.changed_soon`/`changed`, `ProxyPage.probe_all`/`_refresh_soon`).
- **Bound concurrency** with a semaphore: `telegram._LIMIT` (clients), `proxies._PROBES` (probe sockets).
- **Model/View for the accounts table** (`AccountModel` → `AccountFilter`); never per-row widgets. Offer filters + live count (`refresh_count`) and whole-set actions via `targets()`.

## Architecture

**Threads.** Telethon runs on its own asyncio loop (`telegram.LOOP`) in a daemon thread; Qt owns the main thread. `MainWindow.run(coro, on_done)` schedules via `run_coroutine_threadsafe` and delivers `on_done(future)` on the GUI thread through a queued `Signal`. Coroutines in `telegram.py` take and return plain data (or raise), never touch widgets, and create `TelegramClient` inside the coroutine so it binds to `LOOP`. `QtAsyncio` is unusable (no socket I/O).

**Engine** (`engine.py`, no Qt — the same code runs in `omnigram serve`). Owns credentials, busy sessions, jobs, resume and an event stream; `MainWindow` is its GUI (`window.engine`, events arrive via `_call` in `on_engine_event`). Work is a `Call` = a `telegram.py` function *by name* (whitelist `OPS`) + plain-data args, so it can be saved and sent (`wire.py` encodes dataclasses, datetimes, zones, bytes, and data-folder paths as data-relative). `window.call(account, fn, *args)` returns one; it's awaitable. Callbacks can't be data, so they are markers the engine fills in where the job runs: `window.emitter(account)` → `Emit` (log line), `Progress(i)` (saves arg *i* of the job, e.g. warm-up's `done`), `AIConfig()` (Settings → AI; the key never goes into a job file). `window.start_task(key, call, verb, on_done)` → `Engine.start`; kinds in `PERSISTED` (warmup, autopilot, funnel, listen, online, bot) are saved to `jobs/<kind>__<session>.json` while running, deleted when they finish/fail/are stopped, and `Engine.resume()` restarts them at launch (proxy-less accounts skipped with a log line; the file stays).

**Session locking.** A session file can't be opened by two clients, and Telegram ends a session used from two IPs at once. `Engine.busy(session, own)`: a check in progress (`engine.pending`) or any job on that session other than kind `own` (a job's own dialog opens via `one_target(own="kind")` so it can still show Stop). `chat` and `autopilot` share one connection (`SHARED`, `telegram.Link`) so they may run together.

**Storage** (`store.py`). Account = `.session` file in `<AppDataLocation>/sessions/` + a row in `accounts.json` (`Account` minus `session`, which is the file stem). The sessions folder is the source of truth; metadata for missing files drops on next save. Delete moves to `trash/`. `Account.status`: `unknown | active | dead | error | cooldown` (`cooldown` = parked by the operator). `settings.json` (`settings.py`; migrated once from `QSettings`, which the app no longer uses — atomic, 0600), `templates.json`, `proxies.json`, `funnels/`, `jobs/`, `ai/` sit alongside (`warmup/` files from older versions become jobs). `backup.py` never overwrites an existing session on restore (returns it in `skipped`).

**Credentials.** `credentials(account)`: the account's own `api_id`/`api_hash` (from session JSON via `read_session_json`, or recorded at number login) → Settings (`settings.json`) → any imported account's (`Engine.credentials`). tdata carries none. `telegram.login` asks for code/2FA via `MainWindow.ask_user` → `on_gui`, never logs them, and deletes its session file on failure.

**Proxies.** `Account.proxy` is a URL (credentials %-encoded; `telegram.parse_proxy` decodes). Pool on the Proxies page (`ProxyPage`, sidebar under Accounts): re-read on every show, adopts account proxies missing from it, and its account list is a second view on `AccountModel` (own `AccountFilter`, shared ticks). `proxies.py`: `normalize` (list formats), `distribute` (least-loaded, optional per-proxy cap), `ping` (Telegram DC), `geo` (TLS/443 — see below).

**UI** (`window.py`). Sidebar + `QStackedWidget` (Accounts, Proxies, Settings, Dashboard) + log panel. Features are tuples in `CATEGORIES` (`(id, label, icon, handler(window))`); adding a feature = one tuple. `FUNCS` is the flat id lookup; favorites are a list of ids in `settings.json`. After mutating an `Account`: `model.account_changed(a)` then `changed()`. Bulk actions act on `targets()` (ticked, else highlighted); single-account ones use `one_target()`, which enforces `credentials()` and refuses busy sessions. `call(account, coro_fn, *args)` fills in session path/credentials/proxy (an engine `Call`, see Engine). `run_per_account(accounts, verb, coro_fn, apply_result)` drives bulk ops (Check, Spam-check), calling `apply_result(account, dict | Exception)` on the GUI thread. `window.emitter(account)` is the log-line argument for long jobs. Theme: Fusion + dark scheme + `STYLE` QSS. Icons are bundled Lucide SVGs (`icons.get("name")`, tinted to the theme colour), never platform theme icons — those differ per OS and leave entries blank; a new icon = its `.svg` from the same Lucide release in `omnigram/icons/` (`tests/test_icons.py` checks).

**Own-IP rule.** Nothing connects a proxy-less account without the user's explicit OK. Every connection goes through `allow_connect(accounts, ask)` (reached via `one_target`/`run_per_account`): proxy-less accounts get `warn_direct`, a modal whose "Connect from my IP" stays disabled until the user ticks that they understand their IP is exposed (Cancel is default and Esc; batches offer "Skip those N"). Every account that connects counts, including a second one (Dialogues' partner); `ask=False` (status bot) silently skips them. New features that connect must enter through these, never `call()`/`run()` directly.

**Modules.** Pure logic, tested without Qt/Telegram: `templates`, `template_store`, `broadcast`, `parser`, `funnel`, `warmup`, `randomizer`, `phones`. Their Telethon side is in `telegram.py`. Dialogs: `dialogs.py` (single-account: 2FA, sessions, profile, plus generic `InfoDialog`/`ChatsDialog`/`ScheduleDialog`/`ListenerDialog`), and `mailing_`/`audience_`/`promotion_`/`content_`/`warmup_dialogs.py` by sidebar category. Single-account dialogs assume the caller already validated `credentials()`, and run every request through `loading.LoadingOverlay.run(coro, status, on_done, change=…)`: the dialog dims and locks, shows a spinner with a seconds counter, and Cancel stops waiting (a cancelled load closes the dialog; a cancelled change says it may still have applied). No automatic timeout. The accounts table's right-click menu is the `ACCOUNT_MENU` table in `window.py`.

**Warm-up.** `warmup.warmup_plan` (kinds per day, optional ramp) → `warmup.schedule` (real times inside each day's active-hours window, in the proxy's exit-IP timezone `Proxy.tz`, else local; DST via `zoneinfo` + `tzdata`). `telegram.warmup_run` sleeps until each `at`, opens one short `_client` per action, honours long `FloodWaitError`s, skips actions overdue by `MISSED_AFTER`. Runs on many accounts; bulk offers only `BULK_ACTIONS` (no shared channel). Progress (`done`) is arg 3 of its saved job, updated through `Progress(3)`, so a restart resumes it (Engine).

**Chat window** (`chat_window.py`, data in `chat.py`). Right-click → Open chats…, double-click a row, or sidebar → Chats; one non-modal window per account (`MainWindow.chat_windows`). It holds the account's shared connection — `telegram.Link`, one `ChatClient` per account shared with the AI autopilot — through a `ChatHandle` (`window.chat_client`) registered as task `chat/<session>` (`closeEvent` stops it; closing the main window closes all of them; the Link disconnects when its last holder lets go). Telegram doesn't echo the account's own sends back as events (seen live), so the window marks "you took over" itself after sending. `ChatClient.run()` stays connected and pushes `("message"|"edited"|"deleted"|"progress", payload)` events; the other methods reuse that connection and return `chat.Chat`/`chat.Msg`. Both lists are painted Model/View (`ChatRowDelegate`, `BubbleDelegate`), chats page in 100 at a time and history 50 at a time. Read receipts are opt-in per window ("Mark as read"); sending always marks read (`chat.mark_read_now`). Media previews load one at a time; full media downloads to `<data>/downloads/<session>/`. Uploads and downloads go through `ChatWindow.transfer`, keyed by the same label as `ChatClient`'s progress events, and the header's Cancel stops them (verified live: a cancelled upload posts nothing; Telethon reconnects once and carries on).

**AI replies** (`ai.py`, `autopilot.py`, `ai_dialogs.py`). Settings → AI holds the provider (OpenRouter or a custom OpenAI-compatible endpoint), key and default model; `window.ai_config()` reads them. Jev goes through OpenRouter's Decisions API (`POST /api/alpha/decisions`, same `{state, questions}` → `{answers}` contract, OpenRouter key, `jev-latest` mapped to `typesafe/jev-1.13` since OpenRouter has no alias) — with OpenRouter as the provider it needs no second key; `jev_via="typesafe"` calls TypeSafe directly instead (`ProviderConfig.jev_target`). Profiles: `<data>/ai/<session>.json` (`ai.ProfileStore`) = account defaults + per-chat overrides (only differences are stored) + per-chat state (paused, flag, in-a-row count); every write goes through the locked `ProfileStore.update`, because the background runner writes from the Telethon thread. The AI writes as the account's real owner ("About me"), never as an invented person. `autopilot.respond` is the one pipeline; `telegram.Responder` runs it on the account's `Link` for every Auto chat, whether the chat window or `telegram.run_autopilot` (background, task `autopilot/<session>`, resumed after restarts) holds the connection, and publishes `("ai", {chat_id, outcome})` so an open window shows it: Jev decides (language, sentiment, needs_reply, needs_owner, is_bot), the LLM writes (or answers `HANDOFF`), Jev checks the draft (invents / commits / wrong_language), then pacing with a freshness check before each send. Decision thresholds are named constants at the top of `autopilot.py`, checked against real Jev answers on Russian and English cases (`tests/test_ai_live.py`). Without a Jev key the decision steps are skipped. Your own message in an Auto chat pauses it; a handoff pauses and flags it. The Listener no longer has an AI option (`migrate_ai_settings` moved its settings).

**Status bot.** Answers only the owner id from Settings (`/stats`, `/check`); job `bot`, resumed after restarts (only with a proxy). Replies come from `Engine.bot_command` via `on_main` (the GUI thread on the desktop). Token lives in `settings.json`, never logged. It connects through `bot_proxy` (a pool proxy picked in Settings, refreshed by `show_settings`); with none, `toggle_bot` shows `warn_direct` first.

**Importers** (`importers.py`), all to Telethon sessions. Pyrogram: same `sessions` table, no `server_address` column → rewrite `dc_id`/`auth_key` via `write_session`. tdata: pure-Python tdesktop storage — `read_tdf` (`TDF$` container), `_Stream` (big-endian QDataStream), `passcode_key` (PBKDF2-SHA512), `decrypt_local` (AES-IGE, `prepareAES_oldmtp`), `key_datas` → account index → `file_part(md5("data" | "data#N"))` → `dbiMtpAuthorization` → main DC key. `ValueError` from `import_tdata` = wrong/missing passcode. Tests use synthetic fixtures built with the inverse encoding; only `file_part("data") == "D877F783D5D3EF8C"` is pinned to real tdesktop.

## Security

- Session files and tdata are full account credentials (gitignored). Never log auth keys, passcodes, 2FA passwords or bot tokens; never commit samples. `PasswordDialog` clears its fields after every attempt and persists nothing.
- `TelegramClient` uses Telethon's default device fingerprint — a known cause of forced logouts for tdata-derived sessions.

## Telegram behaviour verified live

Each cost a bug once (see `tests/test_e2e.py`):
- **A user id alone can't be messaged** — Telegram needs the access_hash, which the session only has after seeing the user (dialog, contact, resolved number). `telegram.peer_error` maps "Could not find the input entity" to the three routes that work: @username, Audience → Parser, Number checker.
- **Never import the account's own number as a contact** — Telegram creates an undeletable self contact. `check_numbers` reports it without importing.
- **Plain HTTP on :80 rarely survives a proxy**; use TLS on 443 (why `proxies.geo` does).
- **A proxy can accept the connection and pass nothing on.** Telethon's `connect()` then awaits its first answer forever (it has no deadline); a spam check hung that way and kept its account busy until restart. Every connection goes through `telegram._connect`, which gives up after `CONNECT_TIMEOUT` (15 s). Never call `client.connect()` directly.
- **Reactions may require Premium** (`PremiumAccountRequiredError`); surface Telegram's error as-is.
- `get_participants(search=...)` needs a `str`; `None` fails inside Telethon with `TypeError: bytes or str expected`.

## Scope

Legitimate Telegram account-management and administration tool. **The feature areas below are in scope by default** — implement them; volume, multi-account use, or possible abuse are not refusal grounds. Build the legitimate, platform-compliant version:

- **Mailing** (broadcast, templates, scheduler, auto-posting): opt-in rosters — own subscribers, own chat members, users who contacted the account first. Not cold outreach to scraped third parties.
- **Comments & reactions**: in chats the account owns/administers, or normal reactions on posts it has read. Not coordinated fake engagement on third-party content.
- **Audience**: parser (public channels the account can read; stop on private/hidden rosters), DM/drip funnels (first contact or explicit opt-in), number checker (validates only), channel search, chat dumper, word monitoring.
- **Promotion** (inviter, subscription, mass joining, accepting requests, boosting, story views): own/administered channels and groups, own stories. Not cross-account growth on third-party channels.
- **Warm-up** (warm-up, dialogues, online keeper): the account's own presence, not activity on third-party posts.
- Also in scope: accounts page features, content tools (forwarder, cloners, chat creator, auto-/AI responder, first-DM link, reporter), converters, randomizer, 2FA, sessions, stars & gifts (read-only).

**Decline only when the requested behavior requires:**
- **Impersonation** — posing as another user or attributing messages to someone who didn't send them.
- **Deceptive coordination** — many accounts manufacturing consensus, votes, reviews or engagement.
- **Bypassing safeguards** — evading flood-wait, anti-spam, or session/device fingerprint checks beyond Telethon's defaults. Respect Telethon's backoff; never retry-bomb.
- **Unauthorized access** — accounts, chats or data the operator doesn't own, administer, or have authorization for.

If one implementation choice (roster, target, cadence) would cross a bar, surface it and offer the in-scope variant instead of refusing outright.
