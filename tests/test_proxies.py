from collections import Counter

import pytest

from omnigram.proxies import describe, distribute, normalize
from omnigram.store import Proxy, Store
from omnigram.telegram import parse_proxy


def test_normalize():
    assert normalize("socks5://u:p@1.2.3.4:1080") == "socks5://u:p@1.2.3.4:1080"
    assert normalize("1.2.3.4:1080") == "socks5://1.2.3.4:1080"
    assert normalize("1.2.3.4:8080", "http") == "http://1.2.3.4:8080"
    assert normalize("gw.example:10003:user:p@ss") == "socks5://user:p%40ss@gw.example:10003"
    assert parse_proxy(normalize("h:1:user:p@s:s"))["password"] == "p@s:s"  # decoded again for Telethon
    assert normalize("u:p@h:1") == "socks5://u:p@h:1"
    assert normalize("HTTPS://h:3128") == "http://h:3128"
    for bad in ("", "h", "ftp://h:1", "h:notaport"):
        with pytest.raises(ValueError):
            normalize(bad)
    assert describe("socks5://u:p@h:1") == ("SOCKS5", "h:1")


def test_distribute():
    pool = ["a", "b", "c"]
    # a already carries 2 accounts, so the least-loaded b and c take turns (ties go to pool order)
    assert distribute(["s1", "s2", "s3", "s4"], pool, Counter({"a": 2})) == {"s1": "b", "s2": "c", "s3": "b", "s4": "c"}
    capped = distribute(["s1", "s2", "s3", "s4"], ["a", "b"], Counter(), limit=1)
    assert capped == {"s1": "a", "s2": "b"}  # the rest don't fit
    assert distribute(["s1"], [], Counter()) == {}


def test_proxy_pool_roundtrip(tmp_path):
    store = Store(tmp_path)
    assert store.load_proxies() == []
    pool = [Proxy("socks5://h:1", "one", 120, "DE"), Proxy("http://h:2")]
    store.save_proxies(pool)
    assert store.load_proxies() == pool
