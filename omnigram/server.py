"""`omnigram serve`: the engine without a GUI, so accounts keep working while the desktop app (or its computer) is
off. The desktop installs it over SSH and talks to it through an `ssh -L` tunnel (remote.py).

Access: the API listens on a Unix socket inside the data folder, owner-only (0600, in a 0700 folder) — no TCP port,
so nothing is reachable from the network and the SSH key is the only credential. `--port` (for systems without
Unix sockets, and the tests on Windows) listens on 127.0.0.1 only and requires the token in `<data>/token`.

HTTP/JSON for requests (values encoded by wire.py), one WebSocket (/events) for everything that happens: log lines,
jobs starting and ending, account changes, AI profile changes, and the live events of the chats a desktop has open.
Several desktops may be connected at once; they all see the same events, and requests apply in arrival order.

The own-IP rule without a user present: every connection needs a proxy (Engine(require_proxy=True)).
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import secrets
import shutil
import signal
import tempfile
import threading
from pathlib import Path

from aiohttp import WSMsgType, web

from omnigram import __version__, ai, backup, telegram, wire
from omnigram.engine import Call, Engine, apply_result
from omnigram.settings import SERVER_KEYS, Settings, write_private
from omnigram.store import Account

API = 1  # bumped when a route changes incompatibly; the desktop compares it
PUSHABLE = frozenset(SERVER_KEYS) | {"managed_ids", "desktop_tz"}
CHAT_METHODS = frozenset({"dialogs", "history", "send_text", "edit", "delete", "mark_read", "thumbnail", "poke"})
META_FIELDS = ("name", "username", "phone", "proxy", "folder", "roles", "api_id", "api_hash", "status", "spam",
               "user_id")
SHUTDOWN_WAIT = 10  # seconds for jobs to let go of their connections on SIGTERM


def default_data() -> Path:
    return Path(os.environ.get("OMNIGRAM_DATA") or Path.home() / ".local" / "share" / "omnigram")


def encode_files(files: dict[str, bytes]) -> dict[str, str]:
    return {rel: base64.b64encode(data).decode() for rel, data in files.items()}


def decode_files(files: dict[str, str]) -> dict[str, bytes]:
    return {rel: base64.b64decode(data) for rel, data in files.items()}


class ChatSession:
    """A server-side chat window: one ChatHandle on the account's Link, held while any desktop has the account's
    chat window open. Its events go to those desktops."""

    def __init__(self, server: "Server", account: Account):
        engine = server.engine
        config = engine.ai_config()
        responder = None
        if config.ready:
            responder = (engine.ai_store_path(account), config, engine.zone(account),
                         engine.emitter(account, "✨"), engine.managed_ids, account.name)
        self.watchers: set[web.WebSocketResponse] = set()
        self.handle = telegram.ChatHandle(engine.store.path(account), *engine.credentials(account), account.proxy,
                                          lambda kind, payload: server.chat_event(account.session, self, kind,
                                                                                  payload), responder)
        self.task = asyncio.ensure_future(self.handle.run())


class Server:
    def __init__(self, engine: Engine, token: str = ""):
        self.engine, self.token = engine, token
        self.clients: set[web.WebSocketResponse] = set()
        self.chats: dict[str, ChatSession] = {}
        engine.subscribe(self.on_event)
        ai.WATCHERS.append(self.on_profile_saved)
        self.app = web.Application(middlewares=[self.auth, self.errors], client_max_size=2 * 1024 ** 3)
        r = self.app.router
        r.add_get("/status", self.status)
        r.add_post("/settings", self.push_settings)
        r.add_get("/accounts", self.list_accounts)
        r.add_post("/accounts/meta", self.update_meta)
        r.add_put("/accounts/{session}", self.receive_account)
        r.add_post("/accounts/{session}/release", self.release_account)
        r.add_delete("/accounts/{session}", self.remove_account)
        r.add_post("/call", self.call)
        r.add_post("/jobs", self.start_job)
        r.add_delete("/jobs/{key:.+}", self.stop_job)
        r.add_post("/bot", self.bot)
        r.add_post("/check", self.check)
        r.add_get("/ai/{session}", self.get_profile)
        r.add_patch("/ai/{session}", self.patch_profile)
        r.add_get("/backup", self.backup)
        r.add_post("/chat/{session}/upload", self.upload)
        r.add_get("/chat/{session}/download", self.download)
        r.add_post("/chat/{session}/{method}", self.chat_call)
        r.add_get("/events", self.events)
        self.runner: web.AppRunner | None = None

    # ---- plumbing --------------------------------------------------------------------------------------------

    @web.middleware
    async def auth(self, request, handler):
        if self.token:
            given = request.headers.get("Authorization", "").removeprefix("Bearer ")
            if not secrets.compare_digest(given, self.token):
                raise web.HTTPUnauthorized(text=json.dumps({"error": "wrong or missing token"}),
                                           content_type="application/json")
        return await handler(request)

    @web.middleware
    async def errors(self, request, handler):
        try:
            return await handler(request)
        except web.HTTPException:
            raise
        except asyncio.CancelledError:
            raise
        except Exception as e:  # the desktop shows the message as-is
            return web.json_response({"error": str(e) or type(e).__name__, "type": type(e).__name__}, status=400)

    def account(self, session: str) -> Account:
        account = self.engine.account(session)
        if account is None or account.placement != "local":
            raise web.HTTPNotFound(text=json.dumps({"error": f"{session} is not on this server"}),
                                   content_type="application/json")
        return account

    def reply(self, value=None, **fields):
        return web.json_response({**fields, **({"result": wire.encode(value, self.engine.root)} if value is not None
                                                 else {})}, dumps=lambda v: json.dumps(v, ensure_ascii=False))

    async def body(self, request) -> dict:
        return await request.json() if request.can_read_body else {}

    # ---- events ----------------------------------------------------------------------------------------------

    def on_event(self, kind: str, payload: dict):  # any thread
        if kind == "account":
            telegram.LOOP.call_soon_threadsafe(self.engine.save)
        message = json.dumps({"kind": kind, **wire.encode(payload, self.engine.root)}, ensure_ascii=False)
        telegram.LOOP.call_soon_threadsafe(self._broadcast, message, self.clients)

    def _broadcast(self, message: str, clients):
        for ws in list(clients):
            if not ws.closed:
                asyncio.ensure_future(self._send(ws, message))

    @staticmethod
    async def _send(ws, message: str):
        try:
            await ws.send_str(message)
        except (ConnectionError, RuntimeError):
            pass

    def on_profile_saved(self, path: Path, store: ai.ProfileStore):
        if path.parent == self.engine.root / "ai":
            # announce what the file holds when the announcement goes out, not what this save held: two quick
            # writes from two threads could otherwise reach the desktops in the wrong order
            telegram.LOOP.call_soon_threadsafe(self._announce_profile, path)

    def _announce_profile(self, path: Path):
        store = ai.ProfileStore.load(path)
        self.on_event("ai", {"session": path.stem, "data": {"defaults": store.defaults, "chats": store.chats}})

    def chat_event(self, session: str, chat: ChatSession, kind: str, payload):
        message = json.dumps({"kind": "chat", "session": session, "event": kind,
                              "payload": wire.encode(payload, self.engine.root)}, ensure_ascii=False)
        self._broadcast(message, chat.watchers)

    async def events(self, request):
        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(request)
        self.clients.add(ws)
        await ws.send_str(json.dumps({"kind": "snapshot", "version": __version__, "api": API,
                                      "jobs": self.engine.job_list(),
                                      "accounts": wire.encode(self.served(), self.engine.root)}, ensure_ascii=False))
        try:
            async for message in ws:
                if message.type != WSMsgType.TEXT:
                    continue
                op = json.loads(message.data)
                if op.get("op") == "chat_open":
                    await self.open_chat(ws, op["session"])
                elif op.get("op") == "chat_close":
                    await self.close_chat(ws, op["session"])
        finally:
            self.clients.discard(ws)
            for session in [s for s, chat in self.chats.items() if ws in chat.watchers]:
                await self.close_chat(ws, session)
        return ws

    # ---- chat windows ----------------------------------------------------------------------------------------

    async def open_chat(self, ws, session: str):
        try:
            account = self.account(session)
            self.engine.check_proxy(account)
            chat = self.chats.get(session)
            if chat is None:
                chat = self.chats[session] = ChatSession(self, account)
            chat.watchers.add(ws)
            await asyncio.wait_for(chat.handle.ready(), telegram.CONNECT_TIMEOUT + 15)
            await ws.send_str(json.dumps({"kind": "chat_ready", "session": session}))
        except Exception as e:
            await ws.send_str(json.dumps({"kind": "chat_failed", "session": session,
                                          "error": str(e) or type(e).__name__}))
            await self.close_chat(ws, session)

    async def close_chat(self, ws, session: str):
        chat = self.chats.get(session)
        if chat is None:
            return
        chat.watchers.discard(ws)
        if not chat.watchers:
            del self.chats[session]
            chat.task.cancel()
            await asyncio.gather(chat.task, return_exceptions=True)

    def chat(self, session: str) -> telegram.ChatHandle:
        chat = self.chats.get(session)
        if chat is None or chat.handle.link is None:
            raise ValueError("that account's chat window isn't connected")
        return chat.handle

    async def chat_call(self, request):
        method = request.match_info["method"]
        if method not in CHAT_METHODS:
            raise web.HTTPNotFound()
        body = await self.body(request)
        handle = self.chat(request.match_info["session"])
        args = wire.decode(body.get("args", []), self.engine.root)
        kwargs = wire.decode(body.get("kwargs", {}), self.engine.root)
        return self.reply(await getattr(handle, method)(*args, **kwargs), ok=True)

    async def upload(self, request):
        """The file streams in, then goes to Telegram. The desktop dropping the request (Cancel) cancels both."""
        handle = self.chat(request.match_info["session"])
        q = request.query
        folder = Path(tempfile.mkdtemp(dir=self.tmp()))
        try:
            path = folder / Path(q["name"]).name
            with open(path, "wb") as f:
                async for chunk in request.content.iter_chunked(1 << 16):
                    f.write(chunk)
            reply_to = int(q["reply_to"]) if q.get("reply_to") else None
            msg = await handle.send_file(int(q["chat_id"]), str(path), q.get("caption", ""),
                                         q.get("compress") == "1", reply_to)
            return self.reply(msg, ok=True)
        finally:
            shutil.rmtree(folder, ignore_errors=True)

    async def download(self, request):
        handle = self.chat(request.match_info["session"])
        folder = Path(tempfile.mkdtemp(dir=self.tmp()))
        try:
            path = Path(await handle.download(int(request.query["chat_id"]), int(request.query["msg_id"]), folder))
            response = web.StreamResponse(headers={"X-Filename": base64.b64encode(path.name.encode()).decode(),
                                                   "Content-Length": str(path.stat().st_size)})
            await response.prepare(request)
            with open(path, "rb") as f:
                while chunk := f.read(1 << 16):
                    await response.write(chunk)
            await response.write_eof()
            return response
        finally:
            shutil.rmtree(folder, ignore_errors=True)

    def tmp(self) -> Path:
        folder = self.engine.root / "tmp"
        folder.mkdir(exist_ok=True)
        return folder

    # ---- status, settings, accounts --------------------------------------------------------------------------

    def served(self) -> list[Account]:
        return [a for a in self.engine.accounts() if a.placement == "local"]

    async def status(self, request):
        return web.json_response({"version": __version__, "api": API, "jobs": self.engine.job_list(),
                                  "accounts": len(self.served())})

    async def push_settings(self, request):
        values = (await self.body(request)).get("values", {})
        self.engine.settings.update({k: v for k, v in values.items() if k in PUSHABLE})
        return web.json_response({"ok": True})

    async def list_accounts(self, request):
        return self.reply(self.served(), ok=True)

    async def update_meta(self, request):
        """The desktop edited server accounts (proxy, folder, …): take its fields."""
        for incoming in wire.decode((await self.body(request)).get("accounts", []), self.engine.root):
            if (account := self.engine.account(incoming.session)) and account.placement == "local":
                for name in META_FIELDS:
                    setattr(account, name, getattr(incoming, name))
        self.engine.save()
        return web.json_response({"ok": True})

    async def receive_account(self, request):
        """Move to server: the account's files arrive, the server proves it can open the session, then resumes the
        account's saved jobs. Any failure leaves nothing behind."""
        session = request.match_info["session"]
        body = await self.body(request)
        incoming: Account = wire.decode(body["account"], self.engine.root)
        if incoming.session != session:
            raise ValueError("account and path disagree")
        existing = self.engine.account(session)
        if existing and existing.placement == "local":
            raise ValueError(f"{session} is already on this server")
        self.engine.check_proxy(incoming)
        if existing:
            self.engine._own.remove(existing)
        self.engine.remove_files(session)
        self.engine.receive_files(session, decode_files(body.get("files", {})))
        incoming.placement = "local"  # here: it connects from here
        self.engine._own.append(incoming)
        try:
            result = await Call(self.engine, incoming, "check").coroutine()
            apply_result(incoming, "check", result)
            if incoming.status != "active":
                raise ValueError("the session isn't logged in")
        except Exception as e:
            self.engine._own.remove(incoming)
            self.engine.remove_files(session)
            self.engine.save()
            raise ValueError(f"the server couldn't open this session: {e}") from e
        self.engine.save()
        self.engine.resume()
        self.engine.publish("account", {"account": incoming})
        return self.reply(incoming, ok=True)

    async def release_account(self, request):
        """Move back, step 1: stop everything on the account (keeping its saved jobs, so the desktop resumes them)
        and hand over its files. From here the server never connects it; step 2 (DELETE) removes it."""
        account = self.account(request.match_info["session"])
        await self.let_go(account.session, keep=True)
        files = self.engine.account_files(account.session)
        account.placement = "released"
        self.engine.save()
        return self.reply(account, files=encode_files(files), ok=True)

    async def remove_account(self, request):
        session = request.match_info["session"]
        account = self.engine.account(session)
        if account is None:
            return web.json_response({"ok": True})
        await self.let_go(session, keep=False)
        self.engine.remove_files(session)
        self.engine._own.remove(account)
        self.engine.save()
        return web.json_response({"ok": True})

    async def let_go(self, session: str, keep: bool):
        """Stop the account's jobs and chat windows and wait until its session file is free."""
        if chat := self.chats.get(session):
            for ws in list(chat.watchers):
                await self.close_chat(ws, session)
        await self.engine.release(session, keep)

    # ---- calls and jobs --------------------------------------------------------------------------------------

    def call_from(self, body: dict) -> Call:
        account = self.account(body["session"])
        return Call(self.engine, account, body["fn"], wire.decode(body.get("args", []), self.engine.root),
                    wire.decode(body.get("kwargs", {}), self.engine.root))

    async def call(self, request):
        body = await self.body(request)
        call = self.call_from(body)
        if call.name in ("check", "check_spam"):  # the server's copy of the account learns the result too
            try:
                result = await call.coroutine()
            except Exception as e:
                apply_result(call.account, call.name, e)
                self.engine.publish("account", {"account": call.account})
                raise
            apply_result(call.account, call.name, result)
            self.engine.publish("account", {"account": call.account})
            return self.reply(result, ok=True)
        return self.reply(await call.coroutine(), ok=True)

    async def start_job(self, request):
        body = await self.body(request)
        self.engine.start(body["key"], self.call_from(body), body["verb"])
        return web.json_response({"ok": True})

    async def stop_job(self, request):
        return web.json_response({"ok": self.engine.stop(request.match_info["key"])})

    async def bot(self, request):
        if (await self.body(request)).get("on"):
            if why := self.engine.bot_ready():
                raise ValueError(why)
            if not self.engine.running("bot"):
                self.engine.start_bot()
        else:
            self.engine.stop("bot")
        return web.json_response({"ok": True})

    async def check(self, request):
        body = await self.body(request)
        accounts = [self.account(s) for s in body.get("sessions", [])]
        free = [a for a in accounts if a.proxy and not self.engine.busy(a.session)]
        self.engine.check(free, body.get("fn", "check"), body.get("verb", "checking"))
        return web.json_response({"ok": True, "started": len(free), "skipped": len(accounts) - len(free)})

    # ---- AI profiles and backups -----------------------------------------------------------------------------

    async def get_profile(self, request):
        store = ai.ProfileStore.load(self.engine.ai_store_path(self.account(request.match_info["session"])))
        return web.json_response({"defaults": store.defaults, "chats": store.chats})

    async def patch_profile(self, request):
        """Merge the desktop's edit: `defaults` replaces the account defaults; each `chats` entry sets (or, with
        null, removes) only the keys it names, so a state the autopilot wrote meanwhile isn't overwritten."""
        body = await self.body(request)

        def change(store: ai.ProfileStore):
            if "defaults" in body:
                store.defaults = body["defaults"]
            for chat_id, entry in body.get("chats", {}).items():
                target = store.chats.setdefault(str(chat_id), {})
                for key, value in entry.items():
                    if value is None:
                        target.pop(key, None)
                    else:
                        target[key] = value

        ai.ProfileStore.update(self.engine.ai_store_path(self.account(request.match_info["session"])), change)
        return web.json_response({"ok": True})

    async def backup(self, request):
        accounts = self.served()
        extra = {}
        for account in accounts:
            extra.update({rel: data for rel, data in self.engine.account_files(account.session).items()
                          if not rel.startswith("sessions/")})
        folder = Path(tempfile.mkdtemp(dir=self.tmp()))
        try:
            path = folder / "backup.zip"
            await asyncio.to_thread(backup.export_backup, self.engine.store, accounts, path, extra)
            response = web.StreamResponse(headers={"Content-Length": str(path.stat().st_size)})
            await response.prepare(request)
            with open(path, "rb") as f:
                while chunk := f.read(1 << 16):
                    await response.write(chunk)
            await response.write_eof()
            return response
        finally:
            shutil.rmtree(folder, ignore_errors=True)

    # ---- start and stop --------------------------------------------------------------------------------------

    async def start(self, socket_path: Path | None = None, port: int | None = None) -> str:
        # handler_cancellation: a desktop dropping a request (Cancel on an upload) stops the work behind it
        self.runner = web.AppRunner(self.app, handle_signals=False, handler_cancellation=True)
        await self.runner.setup()
        if port is not None:
            site = web.TCPSite(self.runner, "127.0.0.1", port)
            await site.start()
            bound = site._server.sockets[0].getsockname()[1]
            return f"http://127.0.0.1:{bound}"
        socket_path.unlink(missing_ok=True)
        old = os.umask(0o177)  # the socket is created owner-only, not chmod-ed after a window where it isn't
        try:
            await web.UnixSite(self.runner, str(socket_path)).start()
        finally:
            os.umask(old)
        return f"unix:{socket_path}"

    async def stop(self):
        for session in list(self.chats):
            for ws in list(self.chats[session].watchers):
                await self.close_chat(ws, session)
        for ws in list(self.clients):
            await ws.close()
        if self.runner:
            await self.runner.cleanup()


def load_token(data: Path) -> str:
    path = data / "token"
    if not path.exists():
        write_private(path, secrets.token_urlsafe(32))
    return path.read_text("utf-8").strip()


def serve(data: Path, socket_path: Path | None = None, port: int | None = None, stop: threading.Event | None = None,
          started=None, log=None):
    """Run until `stop` is set (or SIGTERM/SIGINT). `started(address, token)` is called once listening; `log(line)`
    gets every log line (the command prints them, and journald keeps them)."""
    data.mkdir(parents=True, exist_ok=True)
    if os.name == "posix":
        data.chmod(0o700)
    telegram.start()
    engine = Engine(data, Settings(data / "settings.json"), require_proxy=True,
                    on_main=lambda fn: telegram.LOOP.call_soon_threadsafe(fn))
    if log:
        engine.subscribe(lambda kind, payload: log(payload["line"]) if kind == "log" else None)
    engine.reload()
    token = load_token(data) if port is not None else ""
    server = Server(engine, token)
    address = asyncio.run_coroutine_threadsafe(server.start(socket_path or data / "omnigram.sock", port),
                                               telegram.LOOP).result()
    engine.log(f"→ omnigram {__version__} serving {len(server.served())} account(s) on {address}")
    engine.resume()
    stop = stop or threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, lambda *_: stop.set())
        except (ValueError, OSError):  # not the main thread (tests), or not supported here
            pass
    if started:
        started(address, token)
    while not stop.wait(0.5):
        pass
    engine.shutdown()  # jobs stay saved and resume on the next start
    waited = 0.0
    while engine.jobs and waited < SHUTDOWN_WAIT:
        stop.wait(0.2)
        waited += 0.2
    asyncio.run_coroutine_threadsafe(server.stop(), telegram.LOOP).result(SHUTDOWN_WAIT)
    ai.WATCHERS.remove(server.on_profile_saved)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="omnigram serve", description="Run Omnigram's engine without a GUI.")
    parser.add_argument("--data", type=Path, default=default_data(), help="data folder (default %(default)s)")
    parser.add_argument("--socket", type=Path, help="Unix socket to listen on (default <data>/omnigram.sock)")
    parser.add_argument("--port", type=int, help="listen on 127.0.0.1:PORT with the token in <data>/token instead "
                                                 "(where Unix sockets aren't available)")
    args = parser.parse_args(argv)
    serve(args.data, args.socket, args.port, log=lambda line: print(line, flush=True),
          started=lambda address, token: print(f"omnigram {__version__}: listening on {address}", flush=True))
