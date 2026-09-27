"""Proxy pool helpers: parse the usual proxy-list formats, spread accounts over a pool, probe a proxy.

The pool itself is store.Proxy rows in proxies.json; an account's proxy is still just its URL (Account.proxy).
"""
import asyncio
import json
import time
from collections import Counter
from urllib.parse import quote, urlsplit

from omnigram.telegram import PROXY_SCHEMES, parse_proxy

TELEGRAM_DC = ("149.154.167.51", 443)  # DC2; what Telethon would connect to anyway
GEO_HOST, GEO_PORT = "ipwho.is", 443  # key-free JSON geo over TLS; sees the proxy's exit IP, nothing else
_PROBES = asyncio.Semaphore(50)  # ponytail: cap concurrent probe sockets so "ping all" on a huge pool can't flood FDs


def normalize(line: str, default_scheme: str = "socks5") -> str:
    """One proxy in any common list format -> 'scheme://[user:pass@]host:port'. ValueError if unparseable.

    Accepts scheme://user:pass@host:port, user:pass@host:port, host:port:user:pass, host:port.
    """
    line = line.strip()
    scheme, sep, rest = line.partition("://")
    if not sep:
        scheme, rest = default_scheme, line
    scheme = scheme.lower()
    if scheme in ("socks", "socks5h"):
        scheme = "socks5"
    elif scheme == "https":
        scheme = "http"
    parts = rest.split(":", 3)
    if len(parts) == 4 and parts[1].isdigit():  # host:port:user:pass (the password may hold ':' or '@')
        host, port, user, password = parts
        rest = f"{quote(user, safe='')}:{quote(password, safe='')}@{host}:{port}"
    url = f"{scheme}://{rest}"
    if scheme not in PROXY_SCHEMES:
        raise ValueError(f"unsupported proxy type {scheme!r}")
    parse_proxy(url)  # raises ValueError with a readable message
    return url


def describe(url: str) -> tuple[str, str]:
    """('SOCKS5', 'host:port') for the table."""
    u = urlsplit(url)
    return u.scheme.upper(), f"{u.hostname}:{u.port}"


def distribute(sessions: list[str], pool: list[str], load: Counter, limit: int = 0) -> dict[str, str]:
    """Give each session the least-loaded proxy in `pool`; `load` = accounts already on each proxy (not counting
    `sessions`). limit > 0 caps accounts per proxy; sessions that don't fit are left out of the result."""
    load = Counter({url: load[url] for url in pool})
    plan = {}
    for session in sessions:
        free = [url for url in pool if not limit or load[url] < limit]
        if not free:
            break
        url = min(free, key=lambda u: (load[u], pool.index(u)))
        plan[session] = url
        load[url] += 1
    return plan


async def ping(url: str) -> int:
    """Milliseconds to open a connection to Telegram through the proxy."""
    from python_socks.async_.asyncio import Proxy
    async with _PROBES:
        start = time.perf_counter()
        sock = await Proxy.from_url(url).connect(*TELEGRAM_DC, timeout=10)
        sock.close()
        return round((time.perf_counter() - start) * 1000)


async def geo(url: str) -> str:
    """Country code of the proxy's exit IP, looked up through the proxy itself.

    Over TLS on 443 rather than plain HTTP on 80: many proxies pass only TLS traffic (found live —
    the connection opens on 80 but no response ever arrives), and Telegram itself only needs 443.
    """
    import ssl

    from python_socks.async_.asyncio import Proxy

    async with _PROBES:
        sock = await Proxy.from_url(url).connect(GEO_HOST, GEO_PORT, timeout=10)
        reader, writer = await asyncio.open_connection(sock=sock)
        try:
            await writer.start_tls(ssl.create_default_context(), server_hostname=GEO_HOST)
            writer.write(f"GET / HTTP/1.0\r\nHost: {GEO_HOST}\r\nAccept: application/json\r\n\r\n".encode())
            await writer.drain()
            response = await asyncio.wait_for(reader.read(), 10)
        finally:
            writer.close()
    body = json.loads(response.partition(b"\r\n\r\n")[2] or b"{}")
    code = body.get("country_code")
    if not code:
        raise ValueError(f"lookup failed: {str(body)[:120]}")
    return code
