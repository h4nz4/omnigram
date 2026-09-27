"""The desktop's side of an Omnigram server (server.py): SSH (the system's own `ssh`/`scp`), installing and updating
the server, the tunnel to its API, and a client that makes server accounts work like local ones.

No Qt here. Everything network-bound runs on telegram.LOOP; `publish(kind, payload)` reports what happens (on the
Telethon thread — the desktop hands it to the GUI thread).

Trust: SSH runs non-interactively (BatchMode, key only — no passwords are asked for or stored) against a
known_hosts file in the data folder that holds only host keys the user confirmed by fingerprint. The API is reached
through `ssh -L` to the server's owner-only Unix socket, so the SSH key is the only credential.

Placement: an account runs on this computer or on the server, never both — Telegram ends a session used from two
IPs at once. A server account's session stays here as a locked backup (Engine.busy refuses it, see window.py).
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import socket
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

import aiohttp

from omnigram import ai, telegram, wire
from omnigram.engine import Call, Engine
from omnigram.server import META_FIELDS, decode_files, encode_files
from omnigram.store import Account

RECONNECT = (2, 5, 10, 20, 30, 60)  # seconds between reconnect attempts; the last repeats
TUNNEL_WAIT = 25  # seconds for a new tunnel to answer
UNIT = """[Unit]
Description=Omnigram server
After=network-online.target
Wants=network-online.target

[Service]
ExecStart=%h/.local/bin/omnigram serve
Restart=on-failure
RestartSec=5
TimeoutStopSec=30

[Install]
WantedBy=default.target
"""
NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0  # the GUI app has no console to show


class RemoteError(Exception):
    """The server (or ssh) said no; the message is meant for the user."""

    def __init__(self, message: str, kind: str = ""):
        super().__init__(message)
        self.kind = kind


@dataclass
class ServerConfig:
    host: str = ""
    port: int = 22
    user: str = ""
    key: str = ""  # path of the private key file (the key itself is never copied or stored)
    socket: str = ""  # the server's API socket, an absolute path learned when installing/connecting

    @classmethod
    def from_settings(cls, settings) -> ServerConfig:
        return cls(str(settings.get("server_host")), int(settings.get("server_port", 22) or 22),
                   str(settings.get("server_user")), str(settings.get("server_key")),
                   str(settings.get("server_socket")))

    def to_settings(self) -> dict:
        return {"server_host": self.host, "server_port": self.port, "server_user": self.user,
                "server_key": self.key, "server_socket": self.socket}

    @property
    def ready(self) -> bool:
        return bool(self.host and self.user and self.key)

    @property
    def target(self) -> str:
        return f"{self.user}@{self.host}"

    @property
    def known_name(self) -> str:
        return self.host if self.port == 22 else f"[{self.host}]:{self.port}"


# ---- ssh ---------------------------------------------------------------------------------------------------

def _run(args: list[str], stdin: bytes | None = None, timeout: float = 300) -> str:
    done = subprocess.run(args, input=stdin, capture_output=True, timeout=timeout, creationflags=NO_WINDOW)
    if done.returncode != 0:
        error = done.stderr.decode("utf-8", "replace").strip() or done.stdout.decode("utf-8", "replace").strip()
        raise RemoteError(error or f"{Path(args[0]).name} failed ({done.returncode})")
    return done.stdout.decode("utf-8", "replace")


class SSH:
    def __init__(self, config: ServerConfig, known_hosts: Path):
        self.config, self.known_hosts = config, known_hosts

    def options(self) -> list[str]:
        return ["-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes", "-o", f"UserKnownHostsFile={self.known_hosts}",
                "-o", "ConnectTimeout=15", "-o", "IdentitiesOnly=yes", "-i", self.config.key]

    def command(self, script: str) -> list[str]:
        return ["ssh", *self.options(), "-p", str(self.config.port), self.config.target, script]

    async def run(self, script: str, stdin: bytes | None = None, timeout: float = 300) -> str:
        return await asyncio.to_thread(_run, self.command(script), stdin, timeout)

    async def copy(self, local: Path, remote: str):
        await asyncio.to_thread(_run, ["scp", *self.options(), "-P", str(self.config.port), str(local),
                                       f"{self.config.target}:{remote}"])

    # host keys: nothing is trusted until the user confirmed its fingerprint

    def trusted(self) -> bool:
        if not self.known_hosts.exists():
            return False
        try:
            _run(["ssh-keygen", "-F", self.config.known_name, "-f", str(self.known_hosts)], timeout=15)
            return True
        except RemoteError:
            return False

    def scan(self) -> tuple[list[str], list[str]]:
        """The server's host keys (known_hosts lines) and their SHA256 fingerprints, for the user to confirm."""
        lines = [line for line in _run(["ssh-keyscan", "-p", str(self.config.port), "-T", "10", self.config.host],
                                       timeout=30).splitlines() if line and not line.startswith("#")]
        if not lines:
            raise RemoteError(f"{self.config.host} didn't show an SSH host key (is port {self.config.port} right?)")
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "keys"
            path.write_text("\n".join(lines) + "\n", "utf-8")
            fingerprints = [" ".join(line.split()[1:2] + line.split()[-1:]) for line in
                            _run(["ssh-keygen", "-l", "-f", str(path)], timeout=15).splitlines() if line]
        return lines, fingerprints

    def trust(self, lines: list[str]):
        self.known_hosts.parent.mkdir(parents=True, exist_ok=True)
        with open(self.known_hosts, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        if os.name == "posix":
            self.known_hosts.chmod(0o600)

    def tunnel(self, local_port: int, remote_socket: str) -> subprocess.Popen:
        return subprocess.Popen(
            ["ssh", *self.options(), "-N", "-o", "ExitOnForwardFailure=yes", "-o", "ServerAliveInterval=15",
             "-o", "ServerAliveCountMax=3", "-L", f"127.0.0.1:{local_port}:{remote_socket}",
             "-p", str(self.config.port), self.config.target],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, creationflags=NO_WINDOW)


# ---- installing and updating ----------------------------------------------------------------------------------

def find_wheel(work: Path) -> Path:
    """This version's package for the server: bundled with a built app (build.py puts it in server/), else built
    from the source checkout with `uv build`."""
    bundled = Path(__file__).resolve().parent.parent / "server"
    wheels = sorted(bundled.glob("omnigram-*.whl"))
    if wheels:
        return wheels[-1]
    project = Path(__file__).resolve().parent.parent
    if not (project / "pyproject.toml").exists():
        raise RemoteError("this build carries no server package (server/omnigram-*.whl)")
    work.mkdir(parents=True, exist_ok=True)
    for old in work.glob("omnigram-*.whl"):
        old.unlink()
    _run(["uv", "build", "--wheel", "--out-dir", str(work), str(project)], timeout=300)
    return sorted(work.glob("omnigram-*.whl"))[-1]


async def install(ssh: SSH, wheel: Path, step) -> str:
    """Install (or update) the server over SSH; `step(text)` reports progress. Returns the API socket's path.
    Stops at the first failing step with the server's own error, before changing anything if systemd is missing."""
    step("Checking the server…")
    facts = (await ssh.run('echo "$HOME"; uname -sm; test -d /run/systemd/system && echo systemd || echo none'))
    home, system, init = (facts.strip().splitlines() + ["", "", ""])[:3]
    if not system.startswith("Linux"):
        raise RemoteError(f"the server runs {system or 'an unknown system'}; Omnigram's server needs Linux")
    if init != "systemd":
        raise RemoteError("systemd is required on the server (nothing was changed)")
    step("Installing uv (if missing)…")
    await ssh.run('command -v uv >/dev/null || test -x "$HOME/.local/bin/uv" || '
                  '(curl -LsSf https://astral.sh/uv/install.sh | sh) >/dev/null', timeout=600)
    step(f"Copying {wheel.name}…")
    await ssh.run('mkdir -p "$HOME/.cache/omnigram"')
    await ssh.copy(wheel, f".cache/omnigram/{wheel.name}")
    step("Installing Omnigram (without the desktop GUI)…")
    await ssh.run(f'"$HOME/.local/bin/uv" tool install --force --python 3.13 "$HOME/.cache/omnigram/{wheel.name}"',
                  timeout=900)
    step("Setting up the service…")
    await ssh.run('mkdir -p "$HOME/.config/systemd/user" && cat > "$HOME/.config/systemd/user/omnigram.service"',
                  stdin=UNIT.encode())
    try:
        await ssh.run('loginctl enable-linger "$(id -un)"')
    except RemoteError as e:  # it still runs while you're logged in; say what to do about the rest
        step(f"⚠ Couldn't keep it running after logout ({e}). Ask the server's admin to run "
             f"`sudo loginctl enable-linger {ssh.config.user}`.")
    step("Starting it…")
    await ssh.run("systemctl --user daemon-reload && systemctl --user enable omnigram >/dev/null 2>&1 && "
                  "systemctl --user restart omnigram")
    return f"{home.rstrip('/')}/.local/share/omnigram/omnigram.sock"


# ---- the client ---------------------------------------------------------------------------------------------

class RemoteCall:
    """A Call for a server account: awaiting it runs it on the server (window.call returns one of these)."""

    def __init__(self, remote: Remote, account: Account, fn, args=(), kwargs=None):
        call = Call(None, account, fn, args, kwargs)
        self.remote, self.account, self.name, self.args, self.kwargs = remote, account, call.name, call.args, \
            call.kwargs

    def body(self) -> dict:
        root = self.remote.engine.root
        return {"session": self.account.session, "fn": self.name,
                "args": wire.encode(self.args, root, portable=True), "kwargs": wire.encode(self.kwargs, root, True)}

    def __await__(self):
        return self.remote.run_call(self).__await__()


class Remote:
    def __init__(self, engine: Engine, publish):
        self.engine, self.publish = engine, publish
        self.config = ServerConfig.from_settings(engine.settings)
        self.state = "off"  # off | connecting | online | offline
        self.error = ""
        self.version, self.api = "", 0
        self.jobs: dict[str, str] = {}  # server job key -> verb
        self.accounts: dict[str, Account] = {}  # the server's accounts, by session
        self.chat_listeners: dict[str, set] = {}
        self.mirrors: dict[str, dict] = {}  # session -> the AI profile as the server last announced it
        self.online = asyncio.Event()
        self._future = None
        self._tunnel: subprocess.Popen | None = None
        self._http: aiohttp.ClientSession | None = None
        self._ws = None
        self._url = ""
        self._pushed_meta: dict[str, dict] = {}
        ai.WATCHERS.append(self.on_profile_saved)

    @property
    def ssh(self) -> SSH:
        return SSH(self.config, self.engine.root / "known_hosts")

    def configure(self, config: ServerConfig):
        self.config = config
        self.engine.settings.update(config.to_settings())

    def _set_state(self, state: str, error: str = ""):
        self.state, self.error = state, error
        (self.online.set if state == "online" else self.online.clear)()
        self.publish("server_state", {"state": state, "error": error, "version": self.version})

    # ---- connecting ------------------------------------------------------------------------------------------

    def start(self):
        """Connect and stay connected (reconnecting with backoff) until stop()."""
        if self._future is None or self._future.done():
            self._future = asyncio.run_coroutine_threadsafe(self._run(), telegram.LOOP)

    def stop(self):
        if self._future:
            self._future.cancel()
            self._future = None

    async def _run(self):
        attempt = 0
        self._set_state("connecting")
        try:
            while True:
                try:
                    await self._connect()
                    attempt = 0
                    await self._listen()
                    error = "the connection to the server dropped"
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    error = str(e) or type(e).__name__
                await self._close()
                self._set_state("offline", error)
                await asyncio.sleep(RECONNECT[min(attempt, len(RECONNECT) - 1)])
                attempt += 1
        finally:
            await self._close()
            self._set_state("off")

    async def _connect(self):
        if not self.config.ready:
            raise RemoteError("set the server's host, user and SSH key first")
        if not self.config.socket:
            home = (await self.ssh.run('echo "$HOME"')).strip()
            self.configure(ServerConfig(**{**vars(self.config), "socket": f"{home}/.local/share/omnigram/omnigram.sock"}))
        port = free_port()
        self._tunnel = self.ssh.tunnel(port, self.config.socket)
        self._url = f"http://127.0.0.1:{port}"
        self._http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None, sock_connect=10))
        for _ in range(TUNNEL_WAIT * 4):
            if self._tunnel.poll() is not None:
                raise RemoteError(self._tunnel.stderr.read().decode("utf-8", "replace").strip()
                                  or "ssh couldn't open the tunnel")
            try:
                status = await self.request("GET", "/status", timeout=5)
                break
            except (aiohttp.ClientError, asyncio.TimeoutError, RemoteError):
                await asyncio.sleep(0.25)
        else:
            raise RemoteError("the server doesn't answer (is it installed and running? Server → Install)")
        self.version, self.api = status["version"], status["api"]
        self._ws = await self._http.ws_connect(self._url + "/events", heartbeat=30)

    async def _listen(self):
        async for message in self._ws:
            if message.type != aiohttp.WSMsgType.TEXT:
                break
            self._on_message(json.loads(message.data))

    async def _close(self):
        if self._ws is not None:
            await self._ws.close()
            self._ws = None
        if self._http is not None:
            await self._http.close()
            self._http = None
        if self._tunnel is not None:
            self._tunnel.terminate()
            try:
                await asyncio.to_thread(self._tunnel.wait, 5)
            except subprocess.TimeoutExpired:
                self._tunnel.kill()
            self._tunnel = None
        for listeners in self.chat_listeners.values():
            for listener in list(listeners):
                listener("connection", False)

    async def request(self, method: str, path: str, body=None, timeout: float | None = 60) -> dict:
        if self._http is None:
            raise RemoteError("not connected to the server")
        async with self._http.request(method, self._url + path, json=body,
                                      timeout=aiohttp.ClientTimeout(total=timeout)) as response:
            reply = await response.json(content_type=None) if response.content_length != 0 else {}
        if "error" in reply:
            raise RemoteError(reply["error"], reply.get("type", ""))
        return reply

    # ---- events --------------------------------------------------------------------------------------------

    def _on_message(self, m: dict):
        kind = m.get("kind")
        root = self.engine.root
        if kind == "snapshot":
            self.jobs = {job["key"]: job["verb"] for job in m["jobs"]}
            self.accounts = {a.session: a for a in wire.decode(m["accounts"], root)}
            self.version, self.api = m["version"], m["api"]
            self._set_state("online")
            asyncio.ensure_future(self._after_connect())
            self.publish("snapshot", {"accounts": list(self.accounts.values()), "jobs": dict(self.jobs)})
            for session, listeners in self.chat_listeners.items():
                for listener in list(listeners):
                    listener("connection", True)
        elif kind == "job_started":
            self.jobs[m["key"]] = m["verb"]
            self.publish(kind, m)
        elif kind == "job_ended":
            self.jobs.pop(m["key"], None)
            self.publish(kind, m)
        elif kind == "account":
            account = wire.decode(m["account"], root)
            if account.placement == "local":
                self.accounts[account.session] = account
            self.publish(kind, {"account": account})
        elif kind == "ai":
            self._write_mirror(m["session"], m["data"])
            self.publish(kind, {"session": m["session"]})
        elif kind == "chat":
            for listener in list(self.chat_listeners.get(m["session"], ())):
                listener(m["event"], wire.decode(m["payload"], root))
        elif kind in ("chat_ready", "chat_failed"):
            for listener in list(self.chat_listeners.get(m["session"], ())):
                listener(kind, m.get("error", ""))
        elif kind == "log":
            self.publish("log", {"session": m.get("session", ""), "line": "☁ " + m["line"]})

    async def _after_connect(self):
        """Once online: the server gets this desktop's settings, and the AI profile mirrors catch up."""
        try:
            await self.push_settings()
            for session in list(self.accounts):
                self._write_mirror(session, await self.request("GET", f"/ai/{session}"))
        except Exception as e:  # the next reconnect tries again
            self.publish("log", {"session": "", "line": f"✗ server: {e}"})

    # ---- AI profiles: a local mirror, edits sent as merges --------------------------------------------------

    def _write_mirror(self, session: str, data: dict):
        self.mirrors[session] = json.loads(json.dumps(data))
        path = self.engine.root / "ai" / f"{session}.json"
        ai.ProfileStore(path, data.get("defaults", {}), data.get("chats", {})).save()  # not update(): no echo

    def on_profile_saved(self, path: Path, store: ai.ProfileStore):
        """The desktop edited a server account's AI profile (chat window, editors): send the server what changed."""
        session = path.stem
        if path.parent != self.engine.root / "ai" or session not in self.accounts or self.state != "online":
            return
        before = self.mirrors.get(session, {"defaults": {}, "chats": {}})
        patch: dict = {}
        if store.defaults != before.get("defaults", {}):
            patch["defaults"] = store.defaults
        chats = {}
        for chat_id, entry in store.chats.items():
            old = before.get("chats", {}).get(chat_id, {})
            changed = {k: v for k, v in entry.items() if old.get(k) != v}
            changed.update({k: None for k in old if k not in entry})
            if changed:
                chats[chat_id] = changed
        if chats:
            patch["chats"] = chats
        if patch:
            self.mirrors[session] = {"defaults": store.defaults, "chats": json.loads(json.dumps(store.chats))}
            asyncio.run_coroutine_threadsafe(self._patch(session, patch), telegram.LOOP)

    async def _patch(self, session: str, patch: dict):
        try:
            await self.request("PATCH", f"/ai/{session}", patch)
        except Exception as e:
            self.publish("log", {"session": session, "line": f"✗ server: AI settings not saved there: {e}"})

    # ---- settings and accounts -----------------------------------------------------------------------------

    async def push_settings(self):
        values = {**self.engine.settings.for_server(), "desktop_tz": self.engine.local_tz or "",
                  "managed_ids": sorted(self.engine.managed_ids())}
        await self.request("POST", "/settings", {"values": values})

    async def push_meta(self, accounts: list[Account]):
        """Send the server the desktop's edits of its accounts (proxy, folder, …), only when something changed."""
        changed = []
        for account in accounts:
            fields = {name: getattr(account, name) for name in META_FIELDS}
            if self._pushed_meta.get(account.session) != fields:
                self._pushed_meta[account.session] = fields
                changed.append(account)
        if changed:
            await self.request("POST", "/accounts/meta", {"accounts": wire.encode(changed)})

    async def move_in(self, account: Account, files: dict[str, bytes]) -> Account:
        reply = await self.request("PUT", f"/accounts/{account.session}",
                                   {"account": wire.encode(account), "files": encode_files(files)}, timeout=120)
        moved = wire.decode(reply["result"])
        self.accounts[moved.session] = moved
        return moved

    async def release(self, session: str) -> tuple[Account, dict[str, bytes]]:
        reply = await self.request("POST", f"/accounts/{session}/release", timeout=120)
        return wire.decode(reply["result"]), decode_files(reply["files"])

    async def remove(self, session: str):
        await self.request("DELETE", f"/accounts/{session}")
        self.accounts.pop(session, None)
        self.mirrors.pop(session, None)

    async def download_backup(self, dest: Path):
        async with self._http.get(self._url + "/backup", timeout=aiohttp.ClientTimeout(total=None)) as response:
            if response.status != 200:
                raise RemoteError((await response.json(content_type=None)).get("error", "backup failed"))
            with open(dest, "wb") as f:
                async for chunk in response.content.iter_chunked(1 << 16):
                    f.write(chunk)

    # ---- calls and jobs ------------------------------------------------------------------------------------

    def call(self, account: Account, fn, *args, **kwargs) -> RemoteCall:
        return RemoteCall(self, account, fn, args, kwargs)

    async def run_call(self, call: RemoteCall):
        reply = await self.request("POST", "/call", call.body(), timeout=None)
        return wire.decode(reply.get("result"), self.engine.root)

    async def start_job(self, key: str, call: RemoteCall, verb: str):
        await self.request("POST", "/jobs", {"key": key, "verb": verb, **call.body()})

    async def stop_job(self, key: str):
        await self.request("DELETE", f"/jobs/{key}")

    async def bot(self, on: bool):
        await self.request("POST", "/bot", {"on": on})

    def busy(self, session: str, own: str = "") -> str:
        from omnigram.engine import SHARED, job_kind, job_session
        if self.state != "online":
            return "on the server, which is offline"
        for key, verb in self.jobs.items():
            if job_session(key) == session and key != f"{own}/{session}" and not (
                    own in SHARED and job_kind(key) in SHARED):
                return verb
        return ""

    def chat_client(self, account: Account, on_event) -> RemoteChatClient:
        return RemoteChatClient(self, account, on_event)


class RemoteChatClient:
    """The chat window's connection for a server account: the server holds the account's Link (so its autopilot
    keeps running alongside) and forwards the chat's events; the same methods as telegram.ChatHandle. A dropped
    tunnel shows as ("connection", False) and, once back, ("connection", True): the window then refreshes."""

    def __init__(self, remote: Remote, account: Account, on_event):
        self.remote, self.account, self.on_event = remote, account, on_event
        self.session = account.session
        self.self_id = 0
        self._ready: asyncio.Future | None = None
        self._opened: asyncio.Future | None = None

    def _event(self, kind, payload):  # LOOP
        if kind in ("chat_ready", "chat_failed"):
            if self._opened and not self._opened.done():
                if kind == "chat_ready":
                    self._opened.set_result(None)
                else:
                    self._opened.set_exception(RemoteError(payload))
            return
        if kind == "connection" and payload and self._ready and self._ready.done():
            asyncio.ensure_future(self._reopen())
            return
        self.on_event(kind, payload)

    async def _open(self):
        self._opened = asyncio.get_running_loop().create_future()
        await self.remote._ws.send_str(json.dumps({"op": "chat_open", "session": self.session}))
        await asyncio.wait_for(self._opened, telegram.CONNECT_TIMEOUT + 30)

    async def _reopen(self):
        try:
            await self._open()
            self.on_event("connection", True)
        except Exception as e:
            self.on_event("connection", False)
            self.remote.publish("log", {"session": self.session, "line": f"✗ chats: {e}"})

    async def run(self):
        if self.remote.state != "online":
            raise RemoteError("the server is offline")
        self._ready = asyncio.get_running_loop().create_future()
        self.remote.chat_listeners.setdefault(self.session, set()).add(self._event)
        try:
            try:
                await self._open()
                self._ready.set_result(None)
            except Exception as e:
                self._ready.set_exception(e)
                raise
            await asyncio.Event().wait()  # until the window closes (cancelled)
        finally:
            self.remote.chat_listeners.get(self.session, set()).discard(self._event)
            if self.remote._ws is not None and not self.remote._ws.closed:
                try:
                    await self.remote._ws.send_str(json.dumps({"op": "chat_close", "session": self.session}))
                except (ConnectionError, RuntimeError):
                    pass

    async def ready(self):
        while self._ready is None:
            await asyncio.sleep(0.01)
        await asyncio.shield(self._ready)

    async def _chat(self, method: str, *args, **kwargs):
        reply = await self.remote.request("POST", f"/chat/{self.session}/{method}",
                                          {"args": wire.encode(list(args)), "kwargs": wire.encode(kwargs)},
                                          timeout=None)
        return wire.decode(reply.get("result"), self.remote.engine.root)

    async def dialogs(self, more: bool = False, limit: int = 100):
        chats, more = await self._chat("dialogs", more, limit)
        return chats, more

    async def history(self, chat_id: int, before_id: int = 0, limit: int = 50):
        return await self._chat("history", chat_id, before_id, limit)

    async def send_text(self, chat_id: int, text: str, reply_to: int | None = None):
        return await self._chat("send_text", chat_id, text, reply_to)

    async def edit(self, chat_id: int, msg_id: int, text: str):
        return await self._chat("edit", chat_id, msg_id, text)

    async def delete(self, chat_id: int, ids: list[int], revoke: bool):
        return await self._chat("delete", chat_id, ids, revoke)

    async def mark_read(self, chat_id: int, max_id: int = 0):
        return await self._chat("mark_read", chat_id, max_id)

    async def thumbnail(self, chat_id: int, msg_id: int):
        return await self._chat("thumbnail", chat_id, msg_id)

    async def poke(self, chat_id: int):
        return await self._chat("poke", chat_id)

    async def send_file(self, chat_id: int, path: str, caption: str = "", compress: bool = True,
                        reply_to: int | None = None):
        """Streams the file to the server, which sends it. Cancelling (the window's Cancel) drops the request,
        and the server stops its side too."""
        name = Path(path).name
        size = max(1, Path(path).stat().st_size)
        label = f"Uploading {name}"

        async def chunks():
            sent = 0
            with open(path, "rb") as f:
                while chunk := f.read(1 << 16):
                    sent += len(chunk)
                    self.on_event("progress", (label, sent / size / 2))  # the first half: to the server
                    yield chunk

        query = {"chat_id": str(chat_id), "caption": caption, "compress": "1" if compress else "0", "name": name,
                 **({"reply_to": str(reply_to)} if reply_to else {})}
        async with self.remote._http.post(f"{self.remote._url}/chat/{self.session}/upload", params=query,
                                          data=chunks(), timeout=aiohttp.ClientTimeout(total=None)) as response:
            reply = await response.json(content_type=None)
        if "error" in reply:
            raise RemoteError(reply["error"])
        return wire.decode(reply["result"])

    async def download(self, chat_id: int, msg_id: int, folder: Path) -> str:
        async with self.remote._http.get(f"{self.remote._url}/chat/{self.session}/download",
                                         params={"chat_id": str(chat_id), "msg_id": str(msg_id)},
                                         timeout=aiohttp.ClientTimeout(total=None)) as response:
            if response.status != 200:
                raise RemoteError((await response.json(content_type=None)).get("error", "download failed"))
            name = base64.b64decode(response.headers["X-Filename"]).decode()
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / Path(name).name
            partial = path.with_name(path.name + ".part")
            try:
                with open(partial, "wb") as f:
                    async for chunk in response.content.iter_chunked(1 << 16):
                        f.write(chunk)
                partial.replace(path)
            finally:
                partial.unlink(missing_ok=True)
        return str(path)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
