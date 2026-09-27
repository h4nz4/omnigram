"""The Proxies page, on a stand-in window (a temp Store and a real AccountModel) — no MainWindow, no real
app-data folder. It must share ticks with the Accounts page and never keep per-account widgets."""
from types import SimpleNamespace

import pytest

from omnigram.dialogs import ProxyPage
from omnigram.store import Account, Proxy, Store
from omnigram.window import AccountFilter, AccountModel

POOLED, HAND_SET = "socks5://1.1.1.1:1080", "socks5://2.2.2.2:1080"


@pytest.fixture
def page(qapp, tmp_path):
    store = Store(tmp_path)
    store.save_proxies([Proxy(POOLED, "pooled")])
    model = AccountModel()
    model.set_accounts([Account("a", name="A", proxy=HAND_SET), Account("b", name="B"), Account("c", name="C")])
    log = []
    window = SimpleNamespace(store=store, model=model, log=log.append, changed=lambda: None)
    page = ProxyPage(window, AccountFilter())
    page.reload()
    return page


def shown(page) -> list[str]:
    f = page.filter
    return sorted(page.window.model.accounts[f.mapToSource(f.index(r, 0)).row()].session for r in range(f.rowCount()))


def test_reload_adopts_a_proxy_set_by_hand_into_the_pool(page):
    assert [p.url for p in page.pool] == [POOLED, HAND_SET]
    assert [p.url for p in page.window.store.load_proxies()] == [POOLED, HAND_SET]


def test_ticks_are_the_accounts_page_ticks(page):
    page.window.model.set_checked({"b"}, True)  # ticked on the Accounts page
    assert [a.session for a in page.ticked()] == ["b"]
    assert page.tick_count.text() == "1 ticked"


def test_assign_sets_the_selected_proxy_on_ticked_accounts(page):
    page.window.model.set_checked({"b", "c"}, True)
    page.table.selectRow(0)
    page.assign()
    assert {a.session: a.proxy for a in page.window.model.accounts} == {"a": HAND_SET, "b": POOLED, "c": POOLED}
    assert page.table.item(0, 4).text() == "2"  # the Accts column follows


def test_filters_and_tick_shown(page):
    page.show_combo.setCurrentIndex(1)  # without proxy
    assert shown(page) == ["b", "c"]
    page.tick(True)
    assert page.window.model.checked == {"b", "c"}
    page.show_combo.setCurrentIndex(3)  # on the selected proxy
    page.table.selectRow(1)
    assert shown(page) == ["a"]
    page.search.setText("zzz")
    assert shown(page) == []
