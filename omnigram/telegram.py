"""Telethon side. It runs on its own asyncio loop in a daemon thread so the Qt event loop never blocks.

Every public coroutine here opens its own short-lived TelegramClient and takes/returns plain data
(dict/list/str) so window.py never needs to touch a Telethon object off its own thread.
"""
import asyncio
import json
import threading
import time
from collections import Counter
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import unquote, urlsplit

from telethon import TelegramClient, errors, events, functions, types
from telethon.sessions import StringSession
from telethon.tl.types.account import Password

from omnigram import ai, autopilot, chat
from omnigram.templates import render

LOOP = asyncio.new_event_loop()
PROXY_SCHEMES = ("socks5", "socks4", "http")
_LIMIT = asyncio.Semaphore(10)  # ponytail: fixed concurrency, make it a setting if 10 is wrong for someone
SPAMBOT_TIMEOUT = 8  # seconds to wait for @SpamBot's reply
# TelegramClient.connect() sends its first request (GetConfig) and awaits the answer with no deadline, so a
# proxy that accepts the connection but never passes traffic hangs it forever — seen live: a spam check never
# returned and its account stayed "busy" until restart. A healthy connection answers in 1-5 s, even proxied.
CONNECT_TIMEOUT = 15


async def _connect(client: TelegramClient):
    """client.connect(), but give up after CONNECT_TIMEOUT with an error that names the likely cause. Every
    connection goes through here; long jobs are not limited once connected."""
    try:
        async with asyncio.timeout(CONNECT_TIMEOUT):
            await client.connect()
    except TimeoutError:
        await client.disconnect()
        raise TimeoutError(f"Telegram didn't answer within {CONNECT_TIMEOUT} s — is the proxy working?") from None


_STARTED = threading.Event()


def start():
    """Run LOOP in its daemon thread (once; later calls do nothing)."""
    if not _STARTED.is_set():
        _STARTED.set()
        threading.Thread(target=LOOP.run_forever, daemon=True, name="telethon").start()


def parse_proxy(url: str) -> dict | None:
    """'socks5://user:pass@host:port' -> Telethon proxy dict; '' -> None; ValueError if malformed."""
    if not url:
        return None
    u = urlsplit(url)
    if u.scheme not in PROXY_SCHEMES or not u.hostname or not u.port:
        raise ValueError(f"expected {'|'.join(PROXY_SCHEMES)}://[user:pass@]host:port, got {url!r}")
    return {"proxy_type": u.scheme, "addr": u.hostname, "port": u.port,  # credentials may be %-encoded
            "username": u.username and unquote(u.username), "password": u.password and unquote(u.password),
            "rdns": True}


@asynccontextmanager
async def _client(session: Path, api_id: int, api_hash: str, proxy: str = ""):
    """Connect (not necessarily authorized) and always disconnect; bounded so a big batch doesn't flood."""
    async with _LIMIT:
        client = TelegramClient(str(session), api_id, api_hash, proxy=parse_proxy(proxy))
        await _connect(client)
        try:
            yield client
        finally:
            await client.disconnect()


class NotAuthorized(Exception):
    """Raised by the functions below when the session is no longer logged in."""


def peer_error(peer, error: Exception) -> str:
    """Describe a send failure in terms a user can act on.

    Telethon's "Could not find the input entity" means the account has no access_hash for that user
    (it has never seen them in a dialog, as a contact, or via a resolved number) — the raw message
    sends people to Telethon's docs, so name the three routes that actually work instead.
    """
    if isinstance(error, ValueError) and "Could not find the input entity" in str(error):
        return (f"the account cannot reach {peer} — it has never seen that user. Use their @username, "
                f"collect them from a chat (Audience → Parser), or resolve their number with the "
                f"Number checker first")
    return f"{type(error).__name__}: {error}"


async def _authorized(client: TelegramClient):
    if not await client.is_user_authorized():
        raise NotAuthorized("session is not logged in")


async def check(session: Path, api_id: int, api_hash: str, proxy: str) -> dict:
    """Log in with the session and return the Account fields to update."""
    async with _client(session, api_id, api_hash, proxy) as client:
        try:
            me = await client.get_me() if await client.is_user_authorized() else None
        except errors.UnauthorizedError:  # revoked, deactivated, banned
            me = None
    if me is None:
        return {"status": "dead"}
    return {"status": "active", "name": " ".join(filter(None, [me.first_name, me.last_name])),
            "username": me.username or "", "phone": me.phone or "", "user_id": me.id}


async def login(session: Path, api_id: int, api_hash: str, proxy: str, phone: str, ask) -> dict:
    """Sign in to an existing account by phone number, creating `session`.

    ask(prompt, secret) -> awaitable str | None, answered on the GUI thread; None means the user cancelled.
    Doesn't hold a _LIMIT slot: most of the time goes to waiting for the user to type.
    """
    async def answer(prompt: str, secret: bool = False) -> str:
        reply = await ask(prompt, secret)
        if not reply:
            raise ValueError("cancelled")
        return reply.strip()

    client = TelegramClient(str(session), api_id, api_hash, proxy=parse_proxy(proxy))
    await _connect(client)
    try:
        sent = await client.send_code_request(phone)
        prompt = f"Login code sent to {phone} (Telegram app or SMS):"
        while True:
            try:
                await client.sign_in(phone, await answer(prompt), phone_code_hash=sent.phone_code_hash)
                break
            except errors.PhoneCodeInvalidError:
                prompt = "Wrong code, try again:"
            except errors.SessionPasswordNeededError:
                prompt = "This account has a 2FA password:"
                while True:
                    try:
                        await client.sign_in(password=await answer(prompt, secret=True))
                        break
                    except errors.PasswordHashInvalidError:
                        prompt = "Wrong password, try again:"
                break
        me = await client.get_me()
    finally:
        await client.disconnect()
    return {"status": "active", "name": " ".join(filter(None, [me.first_name, me.last_name])),
            "username": me.username or "", "phone": me.phone or phone.lstrip("+"), "user_id": me.id}


async def password_state(session: Path, api_id: int, api_hash: str, proxy: str) -> dict:
    async with _client(session, api_id, api_hash, proxy) as client:
        await _authorized(client)
        info: Password = await client(functions.account.GetPasswordRequest())
        return {"has_password": bool(info.has_password), "hint": info.hint or ""}


async def set_password(session: Path, api_id: int, api_hash: str, proxy: str, current: str, new: str,
                       hint: str = "") -> dict:
    """`current` is required (and must be correct) whenever 2FA is already on; `new=''` removes it."""
    async with _client(session, api_id, api_hash, proxy) as client:
        await _authorized(client)
        if not await client.edit_2fa(current_password=current or None, new_password=new or None, hint=hint):
            raise ValueError("wrong current password, or nothing to change")
    return await password_state(session, api_id, api_hash, proxy)


async def authorizations(session: Path, api_id: int, api_hash: str, proxy: str) -> list[dict]:
    async with _client(session, api_id, api_hash, proxy) as client:
        await _authorized(client)
        result = await client(functions.account.GetAuthorizationsRequest())
        return [{
            "hash": a.hash, "current": bool(a.current),
            "device": f"{a.device_model} · {a.platform} {a.system_version}",
            "app": f"{a.app_name} {a.app_version}",
            "ip": a.ip, "location": ", ".join(filter(None, [a.country, a.region])),
            "active": a.date_active.strftime("%Y-%m-%d %H:%M") if a.date_active else "",
        } for a in result.authorizations]


async def terminate_authorization(session: Path, api_id: int, api_hash: str, proxy: str, hash_: int):
    async with _client(session, api_id, api_hash, proxy) as client:
        await _authorized(client)
        await client(functions.account.ResetAuthorizationRequest(hash=hash_))


async def terminate_other_authorizations(session: Path, api_id: int, api_hash: str, proxy: str):
    async with _client(session, api_id, api_hash, proxy) as client:
        await _authorized(client)
        await client(functions.auth.ResetAuthorizationsRequest())


async def get_profile(session: Path, api_id: int, api_hash: str, proxy: str) -> dict:
    """The profile as Telegram has it now: first_name, last_name, username, about (bio) — "" when unset."""
    async with _client(session, api_id, api_hash, proxy) as client:
        await _authorized(client)
        me = await client.get_me()
        full = await client(functions.users.GetFullUserRequest(types.InputUserSelf()))
    return {"first_name": me.first_name or "", "last_name": me.last_name or "", "username": me.username or "",
            "about": full.full_user.about or ""}


async def update_profile(session: Path, api_id: int, api_hash: str, proxy: str, **fields) -> dict:
    """fields: any of first_name, last_name, about, username (empty username clears it). Send only what changed:
    Telegram rejects an unchanged username (USERNAME_NOT_MODIFIED) and an omitted field is left as it is."""
    async with _client(session, api_id, api_hash, proxy) as client:
        await _authorized(client)
        username = fields.pop("username", None)
        if fields:
            await client(functions.account.UpdateProfileRequest(**fields))
        if username is not None:
            await client(functions.account.UpdateUsernameRequest(username=username))
        me = await client.get_me()
    return {"name": " ".join(filter(None, [me.first_name, me.last_name])), "username": me.username or ""}


async def check_spam(session: Path, api_id: int, api_hash: str, proxy: str) -> dict:
    """Message @SpamBot and classify its reply. Returns spam='clean'|'limited'|'unknown' plus the raw reply."""
    async with _client(session, api_id, api_hash, proxy) as client:
        await _authorized(client)
        await client.send_message("SpamBot", "/start")
        reply = ""
        for _ in range(SPAMBOT_TIMEOUT):
            await asyncio.sleep(1)
            messages = await client.get_messages("SpamBot", limit=1)
            if messages and messages[0].out is False and messages[0].text:
                reply = messages[0].text
                break
    if not reply:
        return {"spam": "unknown", "spam_detail": "no reply from @SpamBot"}
    good = "good news" in reply.lower() or "no limits" in reply.lower()
    return {"spam": "clean" if good else "limited", "spam_detail": reply.splitlines()[0][:200]}


# ---- chats the account is in ----------------------------------------------------------------------

def _kind(entity) -> str:
    if isinstance(entity, types.User):
        return "bot" if entity.bot else "user"
    return "channel" if getattr(entity, "broadcast", False) else "group"


def _can(entity, right: str) -> bool:
    """Creator, or an admin holding `right` (a ChatAdminRights field such as 'post_messages')."""
    return bool(getattr(entity, "creator", False) or getattr(getattr(entity, "admin_rights", None), right, False))


def _label(dialog) -> str:
    unread = f"  ·  {dialog.unread_count} unread" if dialog.unread_count else ""
    return f"[{_kind(dialog.entity)}] {dialog.name or dialog.id}{unread}"


async def _pick(client: TelegramClient, ids) -> list:
    """The account's dialogs whose (marked) id is in `ids`; iterating also re-caches their entities."""
    return [d async for d in client.iter_dialogs() if d.id in ids]


async def dialogs(session: Path, api_id: int, api_hash: str, proxy: str, right: str = "") -> list[tuple[int, str]]:
    """(id, label) for every chat, or only those where the account is creator/admin with `right`."""
    async with _client(session, api_id, api_hash, proxy) as client:
        await _authorized(client)
        return [(d.id, _label(d)) async for d in client.iter_dialogs() if not right or _can(d.entity, right)]


async def leave(session: Path, api_id: int, api_hash: str, proxy: str, ids: list[int]) -> int:
    """Leave groups/channels and delete private chats (for this account only)."""
    async with _client(session, api_id, api_hash, proxy) as client:
        await _authorized(client)
        chosen = await _pick(client, ids)
        for d in chosen:
            await d.delete()
        return len(chosen)


async def join_requests(session: Path, api_id: int, api_hash: str, proxy: str) -> list[tuple[int, str]]:
    """Chats where the account may approve join requests and some are pending."""
    async with _client(session, api_id, api_hash, proxy) as client:
        await _authorized(client)
        found = []
        async for d in client.iter_dialogs():
            if not isinstance(d.entity, (types.Channel, types.Chat)) or not _can(d.entity, "invite_users"):
                continue
            try:
                full = await client(functions.channels.GetFullChannelRequest(d.entity)
                                    if isinstance(d.entity, types.Channel)
                                    else functions.messages.GetFullChatRequest(d.entity.id))
            except errors.RPCError:
                continue
            if pending := full.full_chat.requests_pending:
                found.append((d.id, f"[{_kind(d.entity)}] {d.name}  ·  {pending} pending"))
        return found


async def resolve_join_requests(session: Path, api_id: int, api_hash: str, proxy: str, ids: list[int],
                                approve: bool) -> int:
    async with _client(session, api_id, api_hash, proxy) as client:
        await _authorized(client)
        chosen = await _pick(client, ids)
        for d in chosen:
            await client(functions.messages.HideAllChatJoinRequestsRequest(peer=d.input_entity, approved=approve))
        return len(chosen)


async def dump(session: Path, api_id: int, api_hash: str, proxy: str, ids: list[int], folder: str) -> int:
    """Write each chat's full message history to <folder>/<chat id>.json; returns the message count."""
    async with _client(session, api_id, api_hash, proxy) as client:
        await _authorized(client)
        total = 0
        for d in await _pick(client, ids):
            messages = [{
                "id": m.id, "date": m.date.isoformat(), "sender_id": m.sender_id, "reply_to": m.reply_to_msg_id,
                "text": m.message or "", "media": type(m.media).__name__ if m.media else None,
            } async for m in client.iter_messages(d.entity)]
            data = {"chat_id": d.id, "title": d.name, "kind": _kind(d.entity), "messages": messages}
            (Path(folder) / f"{d.id}.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), "utf-8")
            total += len(messages)
        return total


async def schedule_post(session: Path, api_id: int, api_hash: str, proxy: str, chat_id: int, text: str,
                        when: datetime):
    """Hand the post to Telegram's own scheduled-messages queue; it is sent even if Omnigram is closed."""
    async with _client(session, api_id, api_hash, proxy) as client:
        await _authorized(client)
        chosen = await _pick(client, [chat_id])
        if not chosen:
            raise ValueError("chat not found")
        await client.send_message(chosen[0].entity, text, schedule=when)


async def create_chat(session: Path, api_id: int, api_hash: str, proxy: str, title: str, about: str,
                      channel: bool) -> str:
    async with _client(session, api_id, api_hash, proxy) as client:
        await _authorized(client)
        await client(functions.channels.CreateChannelRequest(title=title, about=about, broadcast=channel,
                                                             megagroup=not channel))
        return title


async def search_public(session: Path, api_id: int, api_hash: str, proxy: str, query: str) -> list[str]:
    """Telegram's global search for public groups/channels, as display lines."""
    async with _client(session, api_id, api_hash, proxy) as client:
        await _authorized(client)
        found = await client(functions.contacts.SearchRequest(q=query, limit=50))
        return [f"[{_kind(c)}] {c.title}  ·  @{c.username or '—'}  ·  {c.participants_count or '?'} members"
                for c in found.chats if isinstance(c, types.Channel)]


# ---- read-only account info ----------------------------------------------------------------------

async def _stars(client: TelegramClient) -> int:
    status = await client(functions.payments.GetStarsStatusRequest(peer=types.InputPeerSelf()))
    return status.balance.amount


async def account_stats(session: Path, api_id: int, api_hash: str, proxy: str) -> dict:
    async with _client(session, api_id, api_hash, proxy) as client:
        await _authorized(client)
        me = await client.get_me()
        kinds, unread = Counter(), 0
        async for d in client.iter_dialogs():
            kinds[_kind(d.entity)] += 1
            unread += d.unread_count
        contacts = await client(functions.contacts.GetContactsRequest(hash=0))
        return {"User id": me.id, "Premium": "yes" if me.premium else "no", "Private chats": kinds["user"],
                "Bots": kinds["bot"], "Groups": kinds["group"], "Channels": kinds["channel"],
                "Unread messages": unread, "Contacts": len(contacts.users), "Stars": await _stars(client)}


async def stars_and_gifts(session: Path, api_id: int, api_hash: str, proxy: str) -> dict:
    """Read-only: star balance and the gifts on the profile. Nothing here spends, converts or transfers."""
    async with _client(session, api_id, api_hash, proxy) as client:
        await _authorized(client)
        saved = await client(functions.payments.GetSavedStarGiftsRequest(peer=types.InputPeerSelf(), offset="",
                                                                          limit=100))
        gifts = []
        for g in saved.gifts:
            gift = g.gift
            name = (f"{gift.title} #{gift.num}" if isinstance(gift, types.StarGiftUnique)
                    else gift.title or f"gift {gift.id}")
            worth = f"  ·  {gift.stars}★" if getattr(gift, "stars", None) else ""
            gifts.append(f"{g.date:%Y-%m-%d}  {name}{worth}")
        return {"stars": await _stars(client), "gifts": gifts}


# ---- long-running listeners -----------------------------------------------------------------------

def words(text: str) -> list[str]:
    """'Foo, bar ,, baz' -> ['foo', 'bar', 'baz']"""
    return [w.strip().lower() for w in text.split(",") if w.strip()]


def first_hit(needles: list[str], text: str) -> str | None:
    text = text.lower()
    return next((n for n in needles if n in text), None)


TELEGRAM_SERVICE = 777000  # login codes and notices; never auto-reply to it


async def listen(session: Path, api_id: int, api_hash: str, proxy: str, keywords: list[str], away: str,
                 banned: list[str], emit, first_dm: str = ""):
    """Stay connected until cancelled. emit(str) is called on this loop's thread for each log line.

    keywords: report incoming messages containing any of them. away: reply once per person to private
    messages. banned: delete group messages containing any of them where the account may delete.
    first_dm: template sent instead of `away` to each person's *first* private message (the "link on
    first DM"). AI replies are the AI autopilot's job (run_autopilot), not the listener's.
    Deliberately bypasses _LIMIT: a listener holding a slot forever would starve checks.
    """
    client = TelegramClient(str(session), api_id, api_hash, proxy=parse_proxy(proxy))
    await _connect(client)
    try:
        await _authorized(client)
        replied: set[int] = set()

        async def on_message(event):
            text = event.raw_text or ""
            if hit := first_hit(keywords, text):
                chat = await event.get_chat()
                emit(f"keyword '{hit}' in {getattr(chat, 'title', None) or 'private chat'}: {text[:120]}")
            if (away or first_dm) and event.is_private and event.sender_id not in replied:
                sender = await event.get_sender()
                if not getattr(sender, "bot", False) and event.sender_id != TELEGRAM_SERVICE:
                    replied.add(event.sender_id)
                    context = {"first_name": getattr(sender, "first_name", "") or "",
                               "last_name": getattr(sender, "last_name", "") or "",
                               "name": " ".join(filter(None, [getattr(sender, "first_name", None),
                                                              getattr(sender, "last_name", None)])),
                               "username": getattr(sender, "username", "") or "", "text": text}
                    try:
                        if first_dm:
                            answer = render(first_dm, context)
                        else:
                            answer = render(away, context)
                        await event.reply(answer)
                        emit(f"auto-replied to {context['name'] or event.sender_id}")
                    except Exception as e:
                        emit(f"✗ auto-reply to {context['name'] or event.sender_id}: {type(e).__name__}: {e}")
            if event.is_group and (hit := first_hit(banned, text)):
                chat = await event.get_chat()
                if _can(chat, "delete_messages"):
                    await event.delete()
                    emit(f"deleted a message with '{hit}' in {chat.title}")

        client.add_event_handler(on_message, events.NewMessage(incoming=True))
        emit("listening")
        await client.run_until_disconnected()
    finally:
        await client.disconnect()


async def status_bot(api_id: int, api_hash: str, token: str, owner: int, answer, proxy: str = ""):
    """Bot that answers /start, /stats, /check from `owner` only; everyone else is ignored.

    answer(command) -> awaitable str, computed on the GUI thread. The bot session lives in memory only.
    `proxy` "" connects from the user's own IP; the caller must have had the user confirm that.
    """
    client = TelegramClient(StringSession(), api_id, api_hash, proxy=parse_proxy(proxy))
    await _connect(client)
    try:
        await client.sign_in(bot_token=token)

        async def on_command(event):
            await event.reply(await answer(event.pattern_match.group(1)))

        only_owner = events.NewMessage(from_users=types.PeerUser(owner), pattern=r"^/(start|stats|check)\b")
        client.add_event_handler(on_command, only_owner)
        await client.run_until_disconnected()
    finally:
        await client.disconnect()


# ---- mailing: broadcast, scheduling, auto-posting --------------------------------------------------

async def send_many(session: Path, api_id: int, api_hash: str, proxy: str, steps, emit) -> dict:
    """Send one message per broadcast.Step, honoring each step's delay. Cancellable from the GUI.

    steps: list[broadcast.Step] — (recipient, rendered text, seconds to wait before that send).
    emit(str) is called on this loop's thread for each result line. One bad recipient never ends
    the run; it is counted and reported.
    """
    sent, failed = 0, 0
    client = TelegramClient(str(session), api_id, api_hash, proxy=parse_proxy(proxy))
    await _connect(client)
    try:
        await _authorized(client)
        for step in steps:
            await asyncio.sleep(step.delay)  # 0 for the first step
            label = f"@{step.recipient.username}" if step.recipient.username else str(step.recipient.id)
            try:
                await client.send_message(step.recipient.username or step.recipient.id, step.text)
                sent += 1
                emit(f"sent to {label}")
            except Exception as e:
                failed += 1
                emit(f"failed for {label}: {peer_error(label, e)}")
    finally:
        await client.disconnect()
    return {"sent": sent, "failed": failed}


async def schedule_series(session: Path, api_id: int, api_hash: str, proxy: str, chat_id: int, texts: list[str],
                          first: datetime, interval_hours: float) -> int:
    """Queue a series of posts into Telegram's own scheduled-message queue (auto-posting).

    Telegram delivers them even if Omnigram is closed; nothing keeps running here.
    """
    async with _client(session, api_id, api_hash, proxy) as client:
        await _authorized(client)
        chosen = await _pick(client, [chat_id])
        if not chosen:
            raise ValueError("chat not found")
        for i, text in enumerate(texts):
            await client.send_message(chosen[0].entity, render(text), schedule=first + timedelta(hours=interval_hours * i))
    return len(texts)


async def post_now(session: Path, api_id: int, api_hash: str, proxy: str, chat_id: int, text: str) -> str:
    """Send one post to a chat right now (mailing → send/publish)."""
    async with _client(session, api_id, api_hash, proxy) as client:
        await _authorized(client)
        chosen = await _pick(client, [chat_id])
        if not chosen:
            raise ValueError("chat not found")
        await client.send_message(chosen[0].entity, render(text))
    return text


# ---- comments & reactions -------------------------------------------------------------------------

async def react(session: Path, api_id: int, api_hash: str, proxy: str, chat_id: int, message_ids: list[int],
                emoji: str) -> int:
    """Set one emoji reaction on each given message, as this account (its own normal reaction)."""
    done = 0
    async with _client(session, api_id, api_hash, proxy) as client:
        await _authorized(client)
        peer = await client.get_input_entity(chat_id)
        for mid in message_ids:
            await client(functions.messages.SendReactionRequest(peer=peer, msg_id=mid,
                                                                reaction=[types.ReactionEmoji(emoticon=emoji)]))
            done += 1
    return done


async def comment_latest(session: Path, api_id: int, api_hash: str, proxy: str, chat_id: int, texts: list[str],
                         limit: int = 5) -> int:
    """Comment on the newest posts of a channel, through its linked discussion group.

    The account must be able to comment (member of the discussion group); otherwise Telegram
    rejects each comment and the error is surfaced.
    """
    async with _client(session, api_id, api_hash, proxy) as client:
        await _authorized(client)
        entity = await client.get_entity(chat_id)
        full = await client(functions.channels.GetFullChannelRequest(entity))
        if not full.full_chat.linked_chat_id:
            raise ValueError("this channel has no discussion group to comment in")
        posts = [m async for m in client.iter_messages(entity, limit=limit) if m.message]
        n = 0
        for i, post in enumerate(reversed(posts)):
            await client.send_message(full.full_chat.linked_chat_id, render(texts[i % len(texts)]), comment_to=post.id)
            n += 1
        return n


async def watch_react(session: Path, api_id: int, api_hash: str, proxy: str, chat_ids: list[int],
                      comment_texts: list[str], reaction: str, emit):
    """Long-runner: react to (and optionally comment on) every new post or message in watched chats.

    Channels: apply `reaction`, and when `comment_texts` is set also comment through the linked
    discussion group. Groups: apply `reaction` only. Cancellable exactly like telegram.listen.
    """
    client = TelegramClient(str(session), api_id, api_hash, proxy=parse_proxy(proxy))
    await _connect(client)
    try:
        await _authorized(client)
        chats = [await client.get_input_entity(c) for c in chat_ids] if chat_ids else None

        async def on_new(event):
            chat = await event.get_chat()
            name = getattr(chat, "title", None) or "chat"
            try:
                if reaction:
                    await event.message.react(reaction)
                    emit(f"reacted {reaction} in {name}")
                if comment_texts and getattr(chat, "broadcast", False):
                    full = await client(functions.channels.GetFullChannelRequest(chat))
                    if full.full_chat.linked_chat_id:
                        text = render(comment_texts[event.message.id % len(comment_texts)], {"channel": name})
                        await client.send_message(full.full_chat.linked_chat_id, text, comment_to=event.message.id)
                        emit(f"commented on {name} post {event.message.id}")
            except Exception as e:
                emit(f"✗ {name}: {type(e).__name__}: {e}")

        client.add_event_handler(on_new, events.NewMessage(chats=chats))
        emit("watching for new posts")
        await client.run_until_disconnected()
    finally:
        await client.disconnect()


# ---- audience: parser, number checker -------------------------------------------------------------

async def parse_participants(session: Path, api_id: int, api_hash: str, proxy: str, chat_id: int,
                             limit: int = 0, search: str = "") -> list[dict]:
    """Fetch a chat's participants as plain dicts (omnigram.broadcast.Recipient fields + bot/deleted).

    Reading a public chat's participants is a normal read; a chat with a hidden member list raises
    Telegram's own error, which the dialog shows instead of working around it.
    """
    async with _client(session, api_id, api_hash, proxy) as client:
        await _authorized(client)
        users = await client.get_participants(chat_id, limit=limit or None, search=search)
        return [{"id": u.id, "first_name": u.first_name or "", "last_name": u.last_name or "",
                 "username": u.username or "", "phone": u.phone or "",
                 "bot": bool(getattr(u, "bot", False)), "deleted": bool(getattr(u, "deleted", False))}
                for u in users]


def _phone_contact(client_id: int, number: str) -> types.InputPhoneContact:
    """The contact Telegram needs to answer "is this number on Telegram" — the name is never used."""
    return types.InputPhoneContact(client_id=client_id, phone=number.lstrip("+"), first_name="Omnigram",
                                   last_name="check")


async def check_numbers(session: Path, api_id: int, api_hash: str, proxy: str, numbers: list[str]) -> list[dict]:
    """Which of `numbers` are on Telegram.

    Imports them as contacts, reads which resolved to real users, then deletes the contacts again.
    The account's own number is reported without importing: Telegram turns it into a *self* contact
    that it reports as deleted but keeps forever (found live), so importing it litters the contact
    list. Telegram asks to retry some numbers once; a second pass covers those.
    """
    async with _client(session, api_id, api_hash, proxy) as client:
        await _authorized(client)
        me = await client.get_me()
        own = (me.phone or "").lstrip("+")
        by_client: dict[int, int] = {}
        users: dict[int, object] = {}
        retried: set[int] = set()
        pending = [_phone_contact(i, n) for i, n in enumerate(numbers) if n.lstrip("+") != own]
        for _ in range(2):  # the second pass covers retry_contacts (Telegram's own ask)
            if not pending:
                break
            result = await client(functions.contacts.ImportContactsRequest(contacts=pending))
            for c in result.imported:
                by_client[c.client_id] = c.user_id
            for u in result.users:
                users[u.id] = u
            pending = [_phone_contact(cid, numbers[cid]) for cid in result.retry_contacts if cid not in retried]
            retried.update(result.retry_contacts)
        rows = []
        for i, n in enumerate(numbers):
            if n.lstrip("+") == own:
                rows.append({"phone": n, "registered": True, "id": me.id, "username": me.username or "",
                             "name": " ".join(filter(None, [me.first_name, me.last_name])), "retry": False,
                             "self": True})
                continue
            u = users.get(by_client.get(i, 0))
            rows.append({"phone": n, "registered": u is not None, "id": getattr(u, "id", 0),
                         "username": (getattr(u, "username", "") or "") if u else "",
                         "name": " ".join(filter(None, [getattr(u, "first_name", ""), getattr(u, "last_name", "")]))
                         if u else "", "retry": i in retried, "self": False})
        if users:
            try:
                await client(functions.contacts.DeleteContactsRequest(id=list(users.values())))
            except errors.RPCError:
                pass  # a leftover contact is harmless; never fail the check over cleanup
        return rows


# ---- promotion: inviter, joining, boosting, story views -------------------------------------------

async def invite_users(session: Path, api_id: int, api_hash: str, proxy: str, chat_id: int, user_ids: list[int],
                       emit) -> dict:
    """Invite users to a group/channel this account administers; one result line per user."""
    invited, failed = 0, 0
    async with _client(session, api_id, api_hash, proxy) as client:
        await _authorized(client)
        channel = await client.get_input_entity(chat_id)
        for uid in user_ids:
            try:
                user = await client.get_input_entity(uid)
                await client(functions.channels.InviteToChannelRequest(channel=channel, users=[user]))
                invited += 1
                emit(f"invited {uid}")
            except Exception as e:
                failed += 1
                emit(f"✗ {uid}: {type(e).__name__}: {e}")
    return {"invited": invited, "failed": failed}


async def join(session: Path, api_id: int, api_hash: str, proxy: str, link: str) -> str:
    """Join a chat by @username, t.me link, or +hash invite link; returns the chat title."""
    async with _client(session, api_id, api_hash, proxy) as client:
        await _authorized(client)
        link = (link or "").strip()
        if not link:
            raise ValueError("empty link")
        try:
            if "joinchat/" in link or link.startswith("+"):
                hash_ = link.split("joinchat/")[-1].lstrip("+").strip("/")
                result = await client(functions.messages.ImportChatInviteRequest(hash_))
            else:
                target = link.rstrip("/").split("/")[-1] if "t.me/" in link else link.lstrip("@")
                result = await client(functions.channels.JoinChannelRequest(channel=target))
        except errors.UserAlreadyParticipantError:
            return f"already a member of {link}"
        chat = result.chats[0]
        return getattr(chat, "title", "") or str(chat.id)


async def boost(session: Path, api_id: int, api_hash: str, proxy: str, chat_id: int) -> str:
    """Apply one premium boost slot to a channel (needs a Premium account that may boost it)."""
    async with _client(session, api_id, api_hash, proxy) as client:
        await _authorized(client)
        channel = await client.get_input_entity(chat_id)
        if not hasattr(functions, "premium"):
            raise ValueError("this Telethon version does not expose the premium boost call")
        result = await client(functions.premium.ApplyBoostRequest(peer=channel, slot=0))
        boosted = getattr(result, "chats", None) or []
        return getattr(boosted[0], "title", "") if boosted else "boosted"


async def view_stories(session: Path, api_id: int, api_hash: str, proxy: str, peer_ids: list[int], emit) -> int:
    """View the current stories of the given peers (the account's own normal story reads)."""
    viewed = 0
    async with _client(session, api_id, api_hash, proxy) as client:
        await _authorized(client)
        for pid in peer_ids:
            try:
                peer = await client.get_input_entity(pid)
                result = await client(functions.stories.GetPeerStoriesRequest(peer=peer))
                ids = [s.id for s in result.stories.stories]
                if ids:
                    await client(functions.stories.IncrementStoryViewsRequest(peer=peer, id=ids))
                    viewed += len(ids)
                    emit(f"viewed {len(ids)} story(ies) of {pid}")
                else:
                    emit(f"no active stories for {pid}")
            except Exception as e:
                emit(f"✗ {pid}: {type(e).__name__}: {e}")
    return viewed


# ---- content: forwarder, cloner, reporter ---------------------------------------------------------

async def forward(session: Path, api_id: int, api_hash: str, proxy: str, from_id: int, to_id: int, limit: int,
                  emit) -> int:
    """Forward the latest `limit` messages from one chat to another (0 = everything)."""
    async with _client(session, api_id, api_hash, proxy) as client:
        await _authorized(client)
        source, target = await client.get_input_entity(from_id), await client.get_input_entity(to_id)
        ids = [m.id async for m in client.iter_messages(source, limit=limit or None)]
        done = 0
        for i in range(0, len(ids), 100):  # forward_messages caps at 100 ids per call
            chunk = ids[i:i + 100]
            await client.forward_messages(target, chunk, source)
            done += len(chunk)
            emit(f"forwarded {done}/{len(ids)}")
        return done


async def clone_chat(session: Path, api_id: int, api_hash: str, proxy: str, source_id: int, title: str,
                     channel: bool, limit: int, emit) -> str:
    """Copy a chat's messages into a new chat the operator owns (real copies, no forward links)."""
    async with _client(session, api_id, api_hash, proxy) as client:
        await _authorized(client)
        source = await client.get_input_entity(source_id)
        created = await client(functions.channels.CreateChannelRequest(title=title, about="", broadcast=channel,
                                                                       megagroup=not channel))
        target = created.chats[0]
        n = 0
        async for m in client.iter_messages(source, limit=limit or None, reverse=True):
            if m.action:  # service messages (joins, pins) have nothing to copy
                continue
            await client.send_message(target, m)
            n += 1
            if n % 25 == 0:
                emit(f"copied {n} message(s)")
        return title


async def report(session: Path, api_id: int, api_hash: str, proxy: str, chat_id: int, message_ids: list[int],
                 reason: str) -> int:
    """Report messages to Telegram's moderation (spam/abuse), on the operator's instruction."""
    async with _client(session, api_id, api_hash, proxy) as client:
        await _authorized(client)
        peer = await client.get_input_entity(chat_id)
        await client(functions.messages.ReportRequest(peer=peer, id=message_ids, message=reason or None))
        return len(message_ids)


# ---- warm-up: own-presence actions, dialogues, online keeper ---------------------------------------

async def _warmup_step(client: TelegramClient, kind: str, targets: list[str], emit):
    if kind == "pause":
        emit("paused")
        return
    if kind == "online":
        await client(functions.account.UpdateStatusRequest(offline=False))
        emit("went online")
        return
    if kind == "read":
        count = 0
        async for _ in client.iter_dialogs(limit=10):
            count += 1
        emit(f"read {count} dialog(s)")
        return
    if kind in ("view", "react"):
        if not targets:
            emit(f"no channel picked to {kind}")
            return
        chat = await client.get_input_entity(targets[0])
        messages = [m async for m in client.iter_messages(chat, limit=5)]
        if kind == "view":
            emit(f"viewed {len(messages)} message(s) in {targets[0]}")
            return
        if not messages:
            emit("nothing to react to")
            return
        await messages[0].react("👍")
        emit(f"reacted in {targets[0]}")
        return
    if kind == "join":
        if not targets:
            emit("no invite link picked to join")
            return
        emit(f"joined {await _join_with(client, targets[0])}")


async def _join_with(client: TelegramClient, link: str) -> str:
    """Join by link/username on an already-connected client (used by warm-up)."""
    try:
        if "joinchat/" in link or link.startswith("+"):
            result = await client(functions.messages.ImportChatInviteRequest(link.split("joinchat/")[-1].lstrip("+").strip("/")))
        else:
            target = link.rstrip("/").split("/")[-1] if "t.me/" in link else link.lstrip("@")
            result = await client(functions.channels.JoinChannelRequest(channel=target))
    except errors.UserAlreadyParticipantError:
        return link
    chat = result.chats[0]
    return getattr(chat, "title", "") or str(chat.id)


MISSED_AFTER = 600  # s; an action this overdue (app was closed, a flood wait ran long) is skipped, never fired late


async def warmup_run(session: Path, api_id: int, api_hash: str, proxy: str, actions, targets: list[str], emit,
                     done: int = 0, progress=lambda done: None) -> str:
    """Run a scheduled omnigram.warmup plan from action `done` on, waiting for each action's `at`.
    Cancellable from the GUI; `progress(n)` reports how many actions are behind us (for resuming).

    Actions only touch the account itself (read, view, react to what it already reads, join the
    operator's own channels, go online, pause). Nothing is sent to a third party. Each action opens
    its own short connection through `_client`, so thousands of waiting warm-ups hold no sockets and
    the ones acting at once stay under `_LIMIT`.
    """
    missed = 0
    for i in range(done, len(actions)):
        action = actions[i]
        wait = action.at - time.time()
        if wait < -MISSED_AFTER:
            missed += 1
        else:
            await asyncio.sleep(max(0.0, wait))
            try:
                async with _client(session, api_id, api_hash, proxy) as client:
                    await _authorized(client)
                    await _warmup_step(client, action.kind, targets, emit)
            except NotAuthorized:
                raise
            except errors.FloodWaitError as e:  # Telethon only sleeps short waits itself; honour the long ones
                emit(f"✗ {action.kind}: flood wait, pausing {e.seconds} s")
                await asyncio.sleep(e.seconds)
            except Exception as e:
                emit(f"✗ {action.kind}: {type(e).__name__}: {e}")
        progress(i + 1)
    return f"skipped {missed} missed action(s)" if missed else ""


async def dialogues(session: Path, api_id: int, api_hash: str, proxy: str, partner: dict, opening: str, reply: str,
                    rounds: int, pause: float, emit):
    """Scripted exchange between two of the operator's own accounts (warm-up between own accounts).

    `partner` carries the other account's session path, credentials and proxy. Messages alternate
    between the two accounts; no third party is involved or represented as someone else.
    """
    a = TelegramClient(str(session), api_id, api_hash, proxy=parse_proxy(proxy))
    b = TelegramClient(str(partner["session"]), partner["api_id"], partner["api_hash"],
                       proxy=parse_proxy(partner.get("proxy", "")))
    await _connect(a)
    await _connect(b)
    try:
        await _authorized(a)
        await _authorized(b)
        me_b = await b.get_me()
        me_a = await a.get_me()
        for i in range(rounds):
            await a.send_message(me_b.id, render(opening, {"round": i + 1}))
            emit(f"round {i + 1}: sent")
            await asyncio.sleep(pause)
            await b.send_message(me_a.id, render(reply, {"round": i + 1}))
            emit(f"round {i + 1}: replied")
            if i + 1 < rounds:
                await asyncio.sleep(pause)
    finally:
        await b.disconnect()
        await a.disconnect()


async def online_keeper(session: Path, api_id: int, api_hash: str, proxy: str, minutes: int, emit):
    """Long-runner: keep the account's online flag fresh for `minutes`. Cancellable from the GUI."""
    client = TelegramClient(str(session), api_id, api_hash, proxy=parse_proxy(proxy))
    await _connect(client)
    try:
        await _authorized(client)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + minutes * 60
        while True:
            await client(functions.account.UpdateStatusRequest(offline=False))
            emit("online")
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            await asyncio.sleep(min(60, remaining))
        emit(f"kept online for {minutes} min")
    finally:
        await client.disconnect()


# ---- funnels: keep a saved funnel moving while the app is open -------------------------------------

async def funnel_watch(session: Path, api_id: int, api_hash: str, proxy: str, path: str, emit, every: int = 60):
    """Long-runner: keep the funnel saved at `path` moving while the app is open.

    Enrolls each person who writes first (private messages, bots and Telegram's own service account
    excluded) and every `every` seconds sends the steps that have come due, advancing them after a
    successful send. Cancellable exactly like telegram.listen.
    """
    from omnigram import funnel as F  # local import: funnel imports only templates, keeps the graph simple

    state = Path(path)
    client = TelegramClient(str(session), api_id, api_hash, proxy=parse_proxy(proxy))
    await _connect(client)
    try:
        await _authorized(client)

        def current() -> tuple:
            data = F.load(state)
            return data if data else (F.Funnel(name=state.stem), {})

        async def on_message(event):
            if not event.is_private:
                return
            sender = await event.get_sender()
            if getattr(sender, "bot", False) or event.sender_id == TELEGRAM_SERVICE:
                return
            funnel_, entries = current()
            if F.enroll(entries, funnel_, event.sender_id, datetime.now()):
                F.save(state, funnel_, entries)
                emit(f"enrolled {getattr(sender, 'first_name', None) or event.sender_id} in '{funnel_.name}'")

        async def ticker():
            while True:
                await asyncio.sleep(every)
                funnel_, entries = current()
                if not funnel_.steps:
                    continue
                now = datetime.now()
                changed = False
                for key in F.due_entries(entries, funnel_, now):
                    entry = entries[key]
                    text = F.render_step(funnel_, entry["step"], entry.get("context") or {})
                    try:
                        await client.send_message(key, text)
                        emit(f"sent step {entry['step'] + 1} to {key}")
                        F.advance(entry, funnel_, now)
                    except Exception as e:  # noqa: BLE001 - one bad recipient must not stop the funnel
                        emit(f"✗ step {entry['step'] + 1} for {key}: {type(e).__name__}: {e}")
                    changed = True
                if changed:
                    F.save(state, funnel_, entries)

        client.add_event_handler(on_message, events.NewMessage(incoming=True))
        emit(f"watching '{state.stem}': first DMs enroll, due steps go out every {every}s")
        ticker_task = asyncio.create_task(ticker())
        try:
            await client.run_until_disconnected()
        finally:
            ticker_task.cancel()
    finally:
        await client.disconnect()


# ---- an account's live connection (the chat window and the autopilot share it, see Link) ----------------

def _person(entity) -> str:
    if entity is None:
        return ""
    if isinstance(entity, types.User):
        return " ".join(filter(None, [entity.first_name, entity.last_name])) or entity.username or str(entity.id)
    return getattr(entity, "title", "") or ""


def _poll_question(poll) -> str:
    question = poll.poll.question
    return getattr(question, "text", question)  # a TextWithEntities in newer layers, a str in older ones


def _media(message) -> tuple[str, str, bool]:
    """(kind, label, has_thumb) for a message's media; ("", "", False) for none (link previews count as none)."""
    f = message.file
    if message.photo:
        return "photo", chat.media_label("photo"), True
    if message.sticker:
        return "sticker", chat.media_label("sticker", emoji=getattr(f, "emoji", "") or ""), True
    if message.gif:
        return "gif", chat.media_label("gif"), True
    if message.voice:
        return "voice", chat.media_label("voice", seconds=f.duration), False
    if message.video or message.video_note:
        return "video", chat.media_label("video", seconds=f.duration), True
    if message.audio:
        title = " – ".join(filter(None, [f.title, f.performer]))
        return "audio", chat.media_label("audio", name=f.name or "", seconds=f.duration, title=title), False
    if message.document:
        label = chat.media_label("document", name=f.name or "", size=f.size or 0)
        return "document", label, bool(message.document.thumbs)
    if message.geo or message.venue:
        return "location", chat.media_label("location"), False
    if message.contact:
        return "contact", chat.media_label("contact"), False
    if message.poll:
        return "poll", f"Poll: {_poll_question(message.poll)}", False
    return "", "", False


def _to_msg(message) -> chat.Msg:
    kind, label, has_thumb = _media(message)
    forwarded = ""
    if message.fwd_from:
        source = _person(getattr(message.forward, "sender", None) or getattr(message.forward, "chat", None))
        forwarded = f"Forwarded from {source or message.fwd_from.from_name or 'a hidden user'}"
    return chat.Msg(
        id=message.id, chat_id=message.chat_id, out=bool(message.out), date=message.date,
        text=message.message or "", sender="" if message.out else _person(message.sender),
        reply_to=message.reply_to.reply_to_msg_id if message.reply_to else None,
        edited=bool(message.edit_date) and not message.edit_hide, forwarded=forwarded,
        media=kind, media_label=label, has_thumb=has_thumb, mentioned=bool(message.mentioned),
        sender_id=message.sender_id or 0)


def _small_thumb(sizes):
    """The preview size closest to ~400 px wide: big enough for a bubble, small enough to load fast."""
    usable = [s for s in sizes or [] if isinstance(s, (types.PhotoSize, types.PhotoSizeProgressive))]
    if not usable:
        return None
    fitting = [s for s in usable if s.w <= 480]
    return max(fitting, key=lambda s: s.w) if fitting else min(usable, key=lambda s: s.w)


class ChatClient:
    """An account's live connection (owned by a Link). run() connects and stays connected until cancelled,
    reporting live changes through on_event(kind, payload) on the Telethon thread; every other coroutine uses
    that same connection. All results are plain data from omnigram.chat.

    Events: ("message", Msg) new, or sent from another device; ("edited", Msg); ("deleted", (chat_id, [ids]));
    ("progress", (label, fraction)). chat_id is None for deletions in private chats and small groups, where
    Telegram does not say which chat the ids belonged to.
    """

    def __init__(self, session: Path, api_id: int, api_hash: str, proxy: str, on_event):
        self.args = (session, api_id, api_hash, proxy)
        self.on_event = on_event
        self.client: TelegramClient | None = None
        self._ready = asyncio.Event()
        self._error: Exception | None = None
        self._peers: dict[int, object] = {}  # chat id -> input peer, from the chat list
        self._messages: dict[tuple[int, int], object] = {}  # (chat id, message id) -> Telethon message, for media
        self._dialog_cursor = None  # where the next page of the chat list starts
        self.self_id = 0

    async def run(self):
        session, api_id, api_hash, proxy = self.args
        client = TelegramClient(str(session), api_id, api_hash, proxy=parse_proxy(proxy))
        try:
            await _connect(client)
            await _authorized(client)
            self.self_id = (await client.get_me(input_peer=True)).user_id
        except Exception as e:
            self._error = e
            self._ready.set()
            await client.disconnect()
            raise
        self.client = client

        async def on_new(event):
            self._remember(event.message)
            if event.message.sender is None:
                await event.message.get_sender()
            self.on_event("message", _to_msg(event.message))

        async def on_edit(event):
            self._remember(event.message)
            self.on_event("edited", _to_msg(event.message))

        async def on_delete(event):
            self.on_event("deleted", (event.chat_id, list(event.deleted_ids)))

        client.add_event_handler(on_new, events.NewMessage())
        client.add_event_handler(on_edit, events.MessageEdited())
        client.add_event_handler(on_delete, events.MessageDeleted())
        self._ready.set()
        try:
            await client.run_until_disconnected()
        finally:
            await client.disconnect()

    async def ready(self):
        """Wait until run() is connected; raises run()'s connection error instead of waiting forever."""
        await self._ready.wait()
        if self._error:
            raise self._error

    def _remember(self, message):
        self._messages[(message.chat_id, message.id)] = message

    def _peer(self, chat_id: int):
        return self._peers.get(chat_id, chat_id)

    async def dialogs(self, more: bool = False, limit: int = 100) -> tuple[list[chat.Chat], bool]:
        """A page of the chat list, newest first; more=True continues after the previous page.
        Returns (chats, whether there may be more)."""
        if not more:
            self._dialog_cursor = None
        kwargs = {}
        if self._dialog_cursor:
            kwargs = dict(zip(("offset_date", "offset_id", "offset_peer"), self._dialog_cursor))
        page = await self.client.get_dialogs(limit=limit, **kwargs)
        out = []
        for d in page:
            self._peers[d.id] = d.input_entity
            last = _to_msg(d.message) if d.message else None
            kind = _kind(d.entity)
            can_send = kind != "channel" or _can(d.entity, "post_messages")
            title = "Saved Messages" if d.id == self.self_id else (d.name or str(d.id))
            admin = bool(getattr(d.entity, "creator", False) or getattr(d.entity, "admin_rights", None))
            out.append(chat.Chat(d.id, title, kind, d.unread_count, chat.preview(last) if last else "",
                                 d.date, can_send, admin))
        if page:
            last = page[-1]
            self._dialog_cursor = (last.date, last.message.id if last.message else 0, last.input_entity)
        return out, len(page) == limit

    async def chat_titles(self, limit: int = 200) -> dict[int, str]:
        """Chat id -> title for the newest `limit` chats, without moving dialogs()' paging cursor."""
        titles = {}
        for d in await self.client.get_dialogs(limit=limit):
            self._peers[d.id] = d.input_entity
            titles[d.id] = "Saved Messages" if d.id == self.self_id else (d.name or str(d.id))
        return titles

    async def history(self, chat_id: int, before_id: int = 0, limit: int = 50) -> list[chat.Msg]:
        """Up to `limit` messages older than `before_id` (0 = start from the newest), oldest first."""
        page = await self.client.get_messages(self._peer(chat_id), limit=limit, offset_id=before_id)
        for message in page:
            self._remember(message)
        return sorted((_to_msg(m) for m in page), key=lambda m: m.id)

    async def send_text(self, chat_id: int, text: str, reply_to: int | None = None) -> chat.Msg:
        message = await self.client.send_message(self._peer(chat_id), text, reply_to=reply_to)
        self._remember(message)
        return _to_msg(message)

    async def send_file(self, chat_id: int, path: str, caption: str = "", compress: bool = True,
                        reply_to: int | None = None) -> chat.Msg:
        """compress=True sends a picture as a photo (Telegram recompresses it); False sends the original file."""
        name = Path(path).name

        def progress(sent, total):
            self.on_event("progress", (f"Uploading {name}", sent / total if total else 0))

        message = await self.client.send_file(self._peer(chat_id), path, caption=caption, reply_to=reply_to,
                                              force_document=not compress, progress_callback=progress)
        self._remember(message)
        return _to_msg(message)

    async def edit(self, chat_id: int, msg_id: int, text: str) -> chat.Msg:
        message = await self.client.edit_message(self._peer(chat_id), msg_id, text)
        self._remember(message)
        return _to_msg(message)

    async def delete(self, chat_id: int, ids: list[int], revoke: bool) -> None:
        """revoke=True deletes for everyone; False only from this account's view."""
        await self.client.delete_messages(self._peer(chat_id), ids, revoke=revoke)

    async def mark_read(self, chat_id: int, max_id: int = 0) -> None:
        await self.client.send_read_acknowledge(self._peer(chat_id), max_id=max_id or None)

    # the three operations autopilot.respond needs besides send_text (this class is its `ops` in the window)

    async def typing(self, chat_id: int, seconds: float) -> None:
        """Show "typing…" in the chat for `seconds`."""
        async with self.client.action(self._peer(chat_id), "typing"):
            await asyncio.sleep(seconds)

    async def latest_id(self, chat_id: int) -> int:
        page = await self.client.get_messages(self._peer(chat_id), limit=1)
        return page[0].id if page else 0

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)

    async def thumbnail(self, chat_id: int, msg_id: int) -> bytes | None:
        """A small preview image (encoded bytes) for a photo/sticker/video/GIF/document message."""
        message = self._messages.get((chat_id, msg_id))
        if message is None:
            return None
        if message.photo:
            thumb = _small_thumb(message.photo.sizes)
        elif message.document:
            thumb = _small_thumb(message.document.thumbs)
        else:
            return None
        if thumb is None:
            return None
        return await self.client.download_media(message, file=bytes, thumb=thumb) or None

    async def download(self, chat_id: int, msg_id: int, folder: Path) -> str:
        """Save a message's media into `folder`; returns the saved file's path."""
        message = self._messages.get((chat_id, msg_id))
        if message is None:
            raise ValueError("that message is no longer loaded; reopen the chat")
        folder.mkdir(parents=True, exist_ok=True)
        label = _media(message)[1] or "file"

        def progress(got, total):
            self.on_event("progress", (f"Downloading {label}", got / total if total else 0))

        path = await self.client.download_media(message, file=str(folder), progress_callback=progress)
        if not path:
            raise ValueError("this message has nothing to download")
        return str(path)


# ---- one live connection per account, shared by the chat window and the AI autopilot ------------------------

AUTOPILOT_TICK = 60  # seconds between re-checks of chats that were waiting for active hours
LINKS: dict[str, "Link"] = {}  # str(session path) -> its live connection; only touched on LOOP


class Responder:
    """Answers every chat whose AI mode is Auto, through autopilot.respond, on a Link's connection.

    Profiles are re-read from `store_path` for every message, so edits in the app apply at once; the chat state
    (in-a-row counter, pauses, flags) is written back through ProfileStore.update, the same locked path the app
    uses. A message the owner sends from any device pauses that chat (they took over). `tz` is the account's
    timezone for active hours (None = this computer's). Every outcome is published as ("ai", {"chat_id",
    "outcome"}) so an open chat window shows what the AI did.

    Groups (negative chat ids): only messages that may be addressed to the account are looked at
    (autopilot.group_trigger, from the event itself — no request for the rest of the group's traffic).
    `managed()` returns the Telegram ids of every account this app manages: their messages are never answered,
    so two managed accounts can't talk to each other in a loop. `owner_name` is the account's display name.
    """

    def __init__(self, client: "ChatClient", store_path: Path, config: "ai.ProviderConfig", tz, emit, publish,
                 managed=None, owner_name: str = ""):
        self.client, self.store_path, self.config, self.tz = client, store_path, config, tz
        self.emit, self.publish = emit, publish
        self.managed = managed or (lambda: set())
        self.owner_name = owner_name
        self.last_own: dict[int, float] = {}  # groups: when the account last wrote there (epoch s)
        self.busy: set[int] = set()  # chats being answered right now
        self.again: set[int] = set()  # chats that got another message meanwhile
        self.waiting: set[int] = set()  # chats skipped for active hours
        self.sent_by_ai: set[int] = set()
        self.titles: dict[int, str] = {}
        self.tasks: set[asyncio.Task] = set()

    async def start(self):
        self.titles.update(await self.client.chat_titles())

    def auto_chats(self) -> list[int]:
        return ai.ProfileStore.load(self.store_path).auto_chats()

    def poke(self, chat_id: int):
        """Look at a chat now (Auto was just switched on or resumed there)."""
        task = asyncio.ensure_future(self.handle(chat_id))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    def tick(self):
        for chat_id in list(self.waiting):
            self.poke(chat_id)

    def stop(self):
        for task in list(self.tasks):
            task.cancel()

    def took_over(self, chat_id: int):
        ai.ProfileStore.update(self.store_path, lambda s: s.set_state(
            chat_id, autopilot.owner_took_over(s.state(chat_id))))

    def on_event(self, kind, payload):  # LOOP, from ChatClient's handlers
        if kind != "message":
            return
        msg = payload
        if msg.chat_id not in self.auto_chats():
            return
        if msg.out:
            self.last_own[msg.chat_id] = msg.date.timestamp()
            if msg.id not in self.sent_by_ai:
                self.took_over(msg.chat_id)
                self.emit(f"you wrote in {self.titles.get(msg.chat_id, msg.chat_id)} — Auto paused there")
                self.publish("ai", {"chat_id": msg.chat_id, "outcome": None})
            return
        if msg.sender_id and msg.sender_id in self.managed():
            return  # another account of this app: never answered, or two of them would talk forever
        if msg.chat_id < 0:
            state = ai.ProfileStore.load(self.store_path).state(msg.chat_id)
            last_own = max([self.last_own.get(msg.chat_id, 0.0), *state.sent_at])
            if not autopilot.group_trigger(msg, self.owner_name, last_own, time.time()):
                return  # not for the account: other members talking among themselves
        self.titles.setdefault(msg.chat_id, msg.sender or str(msg.chat_id))
        self.poke(msg.chat_id)

    async def handle(self, chat_id: int):
        if chat_id in self.busy:
            self.again.add(chat_id)
            return
        self.busy.add(chat_id)
        try:
            while True:
                self.again.discard(chat_id)
                store = ai.ProfileStore.load(self.store_path)
                profile = store.profile(chat_id)
                if profile.mode != "auto" or not self.config.ready:
                    return
                history = await self.client.history(chat_id, limit=max(1, profile.context))
                now = datetime.now(self.tz) if self.tz else datetime.now()
                title = self.titles.get(chat_id, str(chat_id))
                group = chat_id < 0
                if group and history and history[-1].sender_id in self.managed():
                    return
                outcome = await autopilot.respond(self.client, self.config, profile, store.state(chat_id), chat_id,
                                                  title, history, now, group=group, owner_name=self.owner_name)
                self.sent_by_ai.update(m.id for m in outcome.messages)
                sent_at = time.time() if group else None
                if outcome.messages and group:
                    self.last_own[chat_id] = sent_at
                ai.ProfileStore.update(self.store_path, lambda s: s.set_state(
                    chat_id, autopilot.apply(s.state(chat_id), outcome, sent_at)))
                if group and outcome.action == "skipped" and outcome.reason.startswith(("group limit", "too soon")):
                    self.emit(f"{title}: mentioned, but {outcome.reason}")
                (self.waiting.add if "active hours" in outcome.reason else self.waiting.discard)(chat_id)
                if outcome.action == "sent":
                    self.emit(f"replied in {title}: {' / '.join(outcome.parts)[:120]}")
                elif outcome.action == "handoff":
                    self.emit(f"⚑ {title}: {outcome.reason}")
                self.publish("ai", {"chat_id": chat_id, "outcome": outcome})
                if outcome.action != "superseded" and chat_id not in self.again:
                    return
        except Exception as e:  # one chat's failure must not stop the others
            self.emit(f"✗ {self.titles.get(chat_id, chat_id)}: {type(e).__name__}: {e}")
        finally:
            self.busy.discard(chat_id)


class Link:
    """One account's live connection (a ChatClient), shared by everything that needs it at the same time: the chat
    window and the AI autopilot. Each holds it with `hold()`; the last one to let go disconnects it. Telegram ends a
    session that is used from two places at once, and a session file can't be opened twice, so there is never a
    second connection for the same account."""

    def __init__(self, session: Path, api_id: int, api_hash: str, proxy: str):
        self.key = str(session)
        self.client = ChatClient(session, api_id, api_hash, proxy, self._event)
        self.listeners: set = set()
        self.holders = 0
        self.runner: asyncio.Future | None = None
        self.responder: Responder | None = None

    @classmethod
    def get(cls, session: Path, api_id: int, api_hash: str, proxy: str) -> "Link":
        link = LINKS.get(str(session))
        if link is None:
            link = LINKS[str(session)] = cls(session, api_id, api_hash, proxy)
        return link

    def _event(self, kind, payload):
        if self.responder:
            self.responder.on_event(kind, payload)
        self.publish(kind, payload)

    def publish(self, kind, payload):
        for listener in list(self.listeners):
            listener(kind, payload)

    def use_responder(self, store_path: Path, config: "ai.ProviderConfig", tz, emit, managed=None,
                      owner_name: str = ""):
        """Answer Auto chats on this connection. The first holder that asks sets it up; a later one (the autopilot
        joining an open chat window) refreshes the AI config and time zone."""
        if self.responder:
            self.responder.config, self.responder.tz = config, tz
            return
        self.responder = Responder(self.client, store_path, config, tz, emit, self.publish, managed, owner_name)
        if self.client.client is not None:  # already connected: catch up now
            asyncio.ensure_future(self.responder.start())

    async def hold(self, listener=None):
        """Keep the connection open until cancelled (listener(kind, payload) gets its events meanwhile). Raises the
        connection's error if it ends on its own."""
        self.holders += 1
        if listener:
            self.listeners.add(listener)
        if self.runner is None:
            self.runner = asyncio.ensure_future(self._run())
        try:
            await asyncio.shield(self.runner)
        finally:
            self.holders -= 1
            self.listeners.discard(listener)
            if self.holders == 0:
                if LINKS.get(self.key) is self:
                    del LINKS[self.key]
                self.runner.cancel()
                await asyncio.gather(self.runner, return_exceptions=True)  # the session file is free after this

    async def _run(self):
        connection = asyncio.ensure_future(self.client.run())
        try:
            await self.client.ready()
            if self.responder:
                await self.responder.start()
            while True:
                done, _ = await asyncio.wait({connection}, timeout=AUTOPILOT_TICK)
                if done:
                    connection.result()  # the connection ended: raise why
                    return
                if self.responder:
                    self.responder.tick()
        finally:
            connection.cancel()
            await asyncio.gather(connection, return_exceptions=True)
            if self.responder:
                self.responder.stop()


class ChatHandle:
    """The chat window's side of an account's Link. run() holds the shared connection (receiving its events through
    on_event) until cancelled; every ChatClient method works on that same connection once ready() returns.
    `responder` = (store_path, config, tz, emit, managed, owner_name) answers Auto chats while the window is
    open (see Responder)."""

    def __init__(self, session: Path, api_id: int, api_hash: str, proxy: str, on_event, responder=None):
        self.args = (session, api_id, api_hash, proxy)
        self.on_event, self.responder = on_event, responder
        self.link: Link | None = None
        self._linked = asyncio.Event()

    async def run(self):
        self.link = Link.get(*self.args)
        if self.responder:
            self.link.use_responder(*self.responder)
        self._linked.set()
        await self.link.hold(self.on_event)

    async def ready(self):
        await self._linked.wait()
        await self.link.client.ready()

    async def poke(self, chat_id: int):
        if self.link and self.link.responder:
            self.link.responder.poke(chat_id)

    @property
    def self_id(self) -> int:
        return self.link.client.self_id if self.link else 0

    def __getattr__(self, name):  # dialogs, history, send_text, …: the shared ChatClient's
        return getattr(self.link.client, name)


async def run_autopilot(session: Path, api_id: int, api_hash: str, proxy: str, store_path: Path,
                        config: "ai.ProviderConfig", tz, emit, managed=None, owner_name: str = ""):
    """The background AI autopilot: hold the account's Link (until cancelled) with a Responder on it. An open chat
    window shares the same connection, so both run at once."""
    link = Link.get(session, api_id, api_hash, proxy)
    link.use_responder(store_path, config, tz, emit, managed, owner_name)
    emit(f"autopilot on: {len(ai.ProfileStore.load(store_path).auto_chats())} chat(s) in Auto")
    await link.hold()
