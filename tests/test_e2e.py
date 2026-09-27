"""Live end-to-end test against the real account in `e2e/` — opt-in, never part of the default suite.

Run it with credentials present:

    OMNIGRAM_E2E=1 uv run pytest tests/test_e2e.py -q -s

Fixture (all optional except the session; the folder is gitignored for credentials):
    e2e/<phone>.session        the account (copied to a temp dir before use — the file is never modified)
    e2e/proxy.txt              scheme/format accepted by proxies.normalize()
    e2e/test_recipient_id.txt  numeric user id that receives the test messages
    e2e/<phone>.json           {"app_id": …, "app_hash": …} — same shape read_session_json() reads

api_id/api_hash are required by Telegram for any client and are resolved from, in order:
    1. e2e/<phone>.json
    2. env TELEGRAM_API_ID / TELEGRAM_API_HASH
    3. QSettings (Omnigram/Omnigram) — the app's Settings page
    4. DEFAULT_API_ID / DEFAULT_API_HASH below — Telegram Desktop's published app credentials, per
       the repo owner's instruction. They identify the *app*, not the account; the session still
       carries the account's own authorization.

Secrets are never printed: the proxy appears as scheme://host:port, the api_hash never at all.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import struct
import time
import zlib
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from omnigram import broadcast, proxies
from omnigram import funnel as funnels
from omnigram import parser as audience
from omnigram import telegram as tg
from omnigram.importers import read_session_json
from omnigram.warmup import warmup_plan

ROOT = Path(__file__).resolve().parent.parent
E2E = ROOT / "e2e"
ENABLED = os.environ.get("OMNIGRAM_E2E") == "1"

pytestmark = pytest.mark.skipif(not ENABLED, reason="live test: set OMNIGRAM_E2E=1 to run")

SKIPPED_STAGES: list[str] = []


def _fixtures() -> tuple[Path, str, str]:
    sessions = sorted(E2E.glob("*.session"))
    if not E2E.is_dir() or not sessions:
        pytest.skip(f"no session in {E2E}")
    proxy_file, recipient_file = E2E / "proxy.txt", E2E / "test_recipient_id.txt"
    if not proxy_file.exists() or not recipient_file.exists():
        pytest.skip(f"{E2E} needs proxy.txt and test_recipient_id.txt")
    proxy = proxies.normalize(proxy_file.read_text("utf-8").strip())
    target = recipient_file.read_text("utf-8").strip()
    if not target:
        pytest.skip(f"{recipient_file} is empty")
    return sessions[0], proxy, target


# Telegram Desktop's published api_id/api_hash (the common fallback for accounts exported from it).
DEFAULT_API_ID = 2040
DEFAULT_API_HASH = "b18441a1ff607e10a989891a5462e627"


def _credentials(session: Path) -> tuple[int, str]:
    """(api_id, api_hash) from the session JSON, env, the app's settings, else the tdesktop default."""
    sidecar = session.with_suffix(".json")
    if sidecar.exists():
        fields = read_session_json(sidecar)
        if fields.get("api_id") and fields.get("api_hash"):
            return fields["api_id"], fields["api_hash"]
    env_id, env_hash = os.environ.get("TELEGRAM_API_ID"), os.environ.get("TELEGRAM_API_HASH")
    if env_id and env_hash:
        return int(env_id), env_hash
    from PySide6.QtCore import QSettings
    settings = QSettings("Omnigram", "Omnigram")
    if settings.value("api_id") and settings.value("api_hash"):
        return int(settings.value("api_id")), str(settings.value("api_hash"))
    return DEFAULT_API_ID, DEFAULT_API_HASH


class Report:
    """Collects one line per stage; a failed required stage fails the test at the end."""

    def __init__(self, log):
        self.log, self.failures = log, []

    def __call__(self, message: str):
        self.log(message)

    def failed(self, stage: str, error: Exception):
        self.failures.append(f"{stage}: {type(error).__name__}: {error}")

    def skip(self, stage: str, why: str):
        SKIPPED_STAGES.append(stage)
        self.log(f"  – {stage}: skipped ({why})")


async def _stage(report: Report, name: str, coro, required: bool = True):
    """Run one stage, log its result, and (for required stages) record a failure."""
    report(f"→ {name}")
    try:
        result = await coro
    except Exception as e:  # noqa: BLE001 - one stage failing must not hide the others' results
        if required:
            report.failed(name, e)
        else:
            report.skip(name, f"{type(e).__name__}: {e}")
        report(f"  {'✗' if required else '–'} {name}: {type(e).__name__}: {e}")
        return None
    report(f"  ✓ {name}: {str(result)[:160]}")
    return result


async def _flow(session: Path, api_id: int, api_hash: str, proxy: str, recipient: str, report: Report):
    """Every stage below runs against real Telegram through the real session and proxy."""
    creds = (session, api_id, api_hash, proxy)

    # --- read-only ---------------------------------------------------------------------------------
    status = await _stage(report, "check (authorize + fetch me)", tg.check(*creds))
    assert not status or status.get("status") == "active", f"account is {status}"

    stats = await _stage(report, "account stats (read)", tg.account_stats(*creds))
    self_id = (stats or {}).get("User id", 0)
    report(f"      own user id: {self_id or 'unknown'}")
    await _stage(report, "stars & gifts (read)", tg.stars_and_gifts(*creds))
    profile = await _stage(report, "profile incl. bio (read)", tg.get_profile(*creds))
    assert profile is None or set(profile) == {"first_name", "last_name", "username", "about"}, profile

    await _stage(report, "proxy ping to a Telegram DC", proxies.ping(proxy))
    # geo (country + timezone) goes to ipwho.is over TLS through the proxy; a proxy that blocks it is not a code fault
    await _stage(report, "proxy exit-IP geo lookup", proxies.geo(proxy), required=False)

    chats = await _stage(report, "read dialog list", tg.dialogs(*creds)) or []
    report(f"      {len(chats)} chat(s) visible")

    # --- parser-------------------------------------------------------------------------------------
    if chats:
        chat_id, label = chats[0]
        # non-required: Telegram hides member lists of chats the account does not administer, and
        # surfacing that error is the correct behaviour — not a code fault
        users = await _stage(report, f"parse participants of {label[:40]}",
                             tg.parse_participants(*creds, chat_id, 5), required=False)
        if users:
            kept = audience.parse_filter([_as_recipient(u) for u in users])
            report(f"      {len(users)} fetched, {len(kept)} kept after parse_filter")
    else:
        report.skip("parse participants", "account sees no chats")

    # --- mailing: the real send path ----------------------------------------------------------------
    # Required proof: send to the account's own Saved Messages ('me' always resolves, touches nobody).
    text = "{rand: e2e test | omnigram check}"
    own = [broadcast.Recipient(id=0, username="me")]
    steps = broadcast.plan(own, text, min_delay=0, max_delay=0)
    lines: list[str] = []
    sent = await _stage(report, "send_many → own Saved Messages (1 step)",
                        tg.send_many(*creds, steps, lines.append))
    assert not sent or (sent["sent"] == 1 and sent["failed"] == 0), f"send_many reported {sent}; emit: {lines}"

    # The fixture recipient. A bare numeric id is only reachable once this account has seen that user
    # (shared chat / contact / resolved number) — Telegram withholds the access_hash otherwise, so
    # that case is reported rather than failed; an @username always resolves.
    fixture_steps = broadcast.plan(broadcast.parse_targets(recipient), text, min_delay=0, max_delay=0)
    fixture_lines: list[str] = []
    resolvable = not recipient.lstrip("-").isdigit()
    await _stage(report, f"send_many → {recipient} (fixture recipient)",
                 tg.send_many(*creds, fixture_steps, fixture_lines.append), required=resolvable)
    report(f"      {fixture_lines}")

    # --- scheduling, cleaned up afterwards ----------------------------------------------------------
    when = datetime.now().astimezone() + timedelta(minutes=5)
    # non-required: schedule_series only queues into chats present in the dialog list, and Saved
    # Messages may not be one for a fresh account
    scheduled = await _stage(report, "schedule_series → Saved Messages (1 message, +5 min)",
                             tg.schedule_series(*creds, self_id, ["e2e scheduled test"], when, 1),
                             required=False) if self_id else None
    if scheduled:
        removed = await _stage(report, "  …delete the scheduled message again",
                              _drop_scheduled(session, api_id, api_hash, proxy, self_id), required=False)
        report(f"      cleaned up: {removed}")
    else:
        report.skip("schedule_series cleanup", "nothing was scheduled (Saved Messages not in dialogs?)")

    # --- number checker: imports the numbers as contacts, reports, deletes them again ----------------
    # non-required: Telegram rate-limits contact import, and the numbers are the account's own plus
    # an unassigned test-range one
    own_phone = (status or {}).get("phone") or ""
    numbers = ([f"+{own_phone}"] if own_phone else []) + ["+15555550100"]
    contacts_before = (stats or {}).get("Contacts", 0)
    rows = await _stage(report, f"check_numbers ({len(numbers)} numbers)", tg.check_numbers(*creds, numbers),
                        required=False)
    if rows:
        report(f"      {[(r['phone'][:6] + '…', r['registered']) for r in rows]}")
        after = await tg.account_stats(*creds)
        assert after.get("Contacts", 0) == contacts_before, \
            f"check_numbers left contacts behind: {contacts_before} → {after.get('Contacts')}"
        report(f"      contacts back to {contacts_before} — imported contacts were cleaned up")

    # --- reaction on the account's own message -------------------------------------------------------
    await _stage(report, "react 👍 to own last Saved-Messages message", _react_to_last(*creds), required=False)

    # --- warm-up plan (own presence only) -----------------------------------------------------------
    plan = warmup_plan(days=1, per_day=2, kinds=["read", "pause"])
    for action in plan:
        action.at = time.time()  # due now; the real schedule spreads them over active hours
    warm_lines: list[str] = []
    await _stage(report, "warmup_run (read + pause)", tg.warmup_run(*creds, plan, [], warm_lines.append))
    report(f"      warm-up emitted: {warm_lines}")

    # --- online keeper, cancelled after a few seconds ------------------------------------------------
    keeper_lines: list[str] = []
    keeper = asyncio.create_task(tg.online_keeper(*creds, 5, keeper_lines.append))
    await asyncio.sleep(4)
    keeper.cancel()
    try:
        await keeper
    except asyncio.CancelledError:
        pass
    assert any("online" in line for line in keeper_lines), f"keeper emitted {keeper_lines}"
    report(f"✓ online_keeper: {keeper_lines}")

    # --- funnel: enroll + send the due step, then stop the watcher ------------------------------------
    funnel_path = Path(session).with_name("e2e-funnel.json")
    funnel = funnels.Funnel(name="e2e", kind="dm", steps=[funnels.Step(0, "funnel step one"), funnels.Step(24, "step two")])
    entries: dict = {}
    funnels.enroll(entries, funnel, "me", datetime.now())  # string keys are legal: usernames and 'me'
    funnels.save(funnel_path, funnel, entries)
    watch_lines: list[str] = []
    watcher = asyncio.create_task(_run_watcher(session, api_id, api_hash, proxy, funnel_path, watch_lines))
    await asyncio.sleep(8)  # the watcher ticks every 3 s
    watcher.cancel()
    try:
        await watcher
    except asyncio.CancelledError:
        pass
    _funnel_after, entries_after = funnels.load(funnel_path)
    sent_step = entries_after.get("me", {}).get("step", 0)
    report(f"✓ funnel_watch: step now {sent_step}, emitted {watch_lines}")
    assert sent_step >= 1, f"the watcher did not send the due step: {watch_lines}"
    funnel_path.unlink(missing_ok=True)

    # --- chat window connection: Saved Messages only, everything it creates is deleted again ---------
    await _stage(report, "chat window (ChatClient) in Saved Messages", _chat_window_flow(*creds, report))


def _png(width: int, height: int) -> bytes:
    """A plain blue PNG, built by hand so the live test needs no image library."""
    row = b"\x00" + bytes((50, 90, 140)) * width

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(row * height))
            + chunk(b"IEND", b""))


async def _chat_window_flow(session, api_id, api_hash, proxy, report) -> str:
    """What the chat window does, against the real API: list chats, read history, send, reply, edit, send a
    file, preview/download it, see live events, mark read, and delete what it sent (for everyone)."""
    events_seen: list[tuple] = []
    client = tg.ChatClient(session, api_id, api_hash, proxy, lambda kind, payload: events_seen.append((kind, payload)))
    runner = asyncio.create_task(client.run())
    created: list[int] = []
    try:
        await asyncio.wait_for(client.ready(), 30)
        chats, _more = await client.dialogs(limit=20)
        saved = client.self_id
        assert chats, "no chats listed"
        first = await client.send_text(saved, "e2e chat window: first")
        created.append(first.id)
        reply = await client.send_text(saved, "e2e chat window: reply", reply_to=first.id)
        created.append(reply.id)
        assert reply.reply_to == first.id, reply
        edited = await client.edit(saved, reply.id, "e2e chat window: reply, edited")
        assert edited.text.endswith("edited") and edited.edited, edited
        note = Path(session).with_name("e2e-attachment.txt")
        note.write_text("omnigram e2e attachment\n", "utf-8")
        attached = await client.send_file(saved, str(note), caption="e2e file", compress=False)
        created.append(attached.id)
        assert attached.media == "document" and attached.media_label.startswith("e2e-attachment.txt"), attached
        picture = Path(session).with_name("e2e-photo.png")
        picture.write_bytes(_png(320, 200))
        photo = await client.send_file(saved, str(picture), compress=True)
        created.append(photo.id)
        assert photo.media == "photo" and photo.has_thumb, photo
        history = await client.history(saved, limit=10)
        assert {first.id, reply.id, attached.id, photo.id} <= {m.id for m in history}, [m.id for m in history]
        preview = await client.thumbnail(saved, photo.id)
        assert preview and preview[:3] == b"\xff\xd8\xff", "the photo preview isn't a JPEG"
        path = await client.download(saved, attached.id, Path(session).with_name("e2e-downloads"))
        assert Path(path).read_text("utf-8") == "omnigram e2e attachment\n"
        await client.mark_read(saved, attached.id)
        await asyncio.sleep(2)  # give live updates a moment to arrive
        kinds = sorted({kind for kind, _ in events_seen})
        report(f"      chat events seen: {kinds}")
        return f"{len(chats)} chats; sent, replied, edited, sent a file and a photo, previewed, downloaded"
    finally:
        if created and client.client:
            await client.delete(client.self_id, created, revoke=True)
        runner.cancel()
        try:
            await runner
        except asyncio.CancelledError:
            pass


def _as_recipient(row: dict):
    from omnigram.broadcast import Recipient
    r = Recipient(id=row["id"], first_name=row["first_name"], last_name=row["last_name"],
                  username=row["username"], phone=row["phone"])
    r.bot, r.deleted = row.get("bot", False), row.get("deleted", False)
    return r


async def _react_to_last(session, api_id, api_hash, proxy) -> str:
    """React to the newest message in Saved Messages. A second client is used only to read the id,
    then closed before telegram.react opens its own — one client per session file at a time."""
    from telethon import TelegramClient
    client = TelegramClient(str(session), api_id, api_hash, proxy=tg.parse_proxy(proxy))
    await client.connect()
    try:
        ids = [m.id for m in await client.get_messages("me", limit=1)]
    finally:
        await client.disconnect()
    if not ids:
        raise ValueError("nothing in Saved Messages to react to")
    done = await tg.react(session, api_id, api_hash, proxy, "me", ids, "👍")
    return f"reacted to {done} message(s)"


async def _run_watcher(session, api_id, api_hash, proxy, path, lines):
    await tg.funnel_watch(session, api_id, api_hash, proxy, str(path), lines.append, every=3)


async def _drop_scheduled(session, api_id, api_hash, proxy, recipient) -> str:
    """Inline cleanup: read Telegram's scheduled queue for the peer and delete what we queued."""
    from telethon import TelegramClient, functions
    client = TelegramClient(str(session), api_id, api_hash, proxy=tg.parse_proxy(proxy))
    await client.connect()
    try:
        peer = await client.get_input_entity(recipient)
        scheduled = await client(functions.messages.GetScheduledHistoryRequest(peer=peer, hash=0))
        ids = [m.id for m in scheduled.messages]
        if ids:
            await client(functions.messages.DeleteScheduledMessagesRequest(peer=peer, id=ids))
        return f"deleted {len(ids)} scheduled message(s)"
    finally:
        await client.disconnect()


def test_live_e2e(tmp_path):
    session, proxy, recipient = _fixtures()
    api_id, api_hash = _credentials(session)
    local = tmp_path / session.name  # never run against the fixture in place
    shutil.copy2(session, local)
    report = Report(print)
    print(f"live e2e: session={session.name}, proxy={proxies.describe(proxy)[0]}://{proxies.describe(proxy)[1]}, "
          f"recipient={recipient}, api_id={'set'}")
    asyncio.run(_flow(local, api_id, api_hash, proxy, recipient, report))
    assert not report.failures, "failed stages: " + "; ".join(report.failures)
