import pytest

from omnigram.store import Account, Store
from omnigram.telegram import parse_proxy


def test_store_roundtrip_and_trash(tmp_path):
    store = Store(tmp_path)
    (store.sessions / "a.session").touch()
    (store.sessions / "b.session").touch()
    a, b = store.load()
    a.phone, a.proxy, b.status = "84993297918", "socks5://u:p@1.2.3.4:1080", "dead"
    store.save([a, b])
    assert store.load() == [a, b]
    assert a.geo == "VN"
    store.move_to_trash(b)
    assert [x.session for x in store.load()] == ["a"]
    assert (store.trash / "b.session").exists()


def test_parse_proxy():
    assert parse_proxy("") is None
    assert parse_proxy("socks5://u:p@1.2.3.4:1080") == {
        "proxy_type": "socks5", "addr": "1.2.3.4", "port": 1080, "username": "u", "password": "p", "rdns": True}
    for bad in ("1.2.3.4:1080", "ftp://h:1", "socks5://host"):
        with pytest.raises(ValueError):
            parse_proxy(bad)
    assert Account("x").geo == ""
