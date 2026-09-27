from omnigram.telegram import first_hit, words


def test_listener_matching():
    assert words(" Foo, bar ,, BAZ ") == ["foo", "bar", "baz"]
    assert words("") == []
    assert first_hit(["price", "buy"], "Want to BUY this") == "buy"
    assert first_hit([], "anything") is None
    assert first_hit(["x"], "") is None


def test_peer_error_explains_unresolved_users():
    """Telethon's raw 'input entity' text sends users to the docs; ours must name the ways out."""
    from omnigram.telegram import peer_error
    raw = ValueError("Could not find the input entity for PeerUser(user_id=1) (PeerUser). Please read "
                     "https://docs.telethon.dev/en/stable/concepts/entities.html to find out more details.")
    message = peer_error("1685764671", raw)
    assert "@username" in message and "Parser" in message and "Number checker" in message
    assert "docs.telethon.dev" not in message


def test_peer_error_keeps_other_errors_readable():
    from omnigram.telegram import peer_error
    assert peer_error("x", RuntimeError("boom")) == "RuntimeError: boom"


def test_parse_participants_passes_search_as_str(monkeypatch):
    """Live Telegram raised "TypeError: bytes or str expected, not NoneType" while this passed
    search=None (Telethon's default is ''). This pins the call shape that actually works."""
    import asyncio
    from pathlib import Path

    from omnigram import telegram as tg

    seen = {}

    class FakeClient:
        async def get_participants(self, chat_id, limit=None, search=""):
            seen.update(chat_id=chat_id, limit=limit, search=search)
            return []

    class FakeContext:
        async def __aenter__(self):
            return FakeClient()

        async def __aexit__(self, *exc):
            return False

    async def fake_authorized(client):
        return None

    monkeypatch.setattr(tg, "_client", lambda *a, **k: FakeContext())
    monkeypatch.setattr(tg, "_authorized", fake_authorized)
    assert asyncio.run(tg.parse_participants(Path("x.session"), 1, "hash", "", 42, limit=0, search="")) == []
    assert seen == {"chat_id": 42, "limit": None, "search": ""}
    assert isinstance(seen["search"], str)  # None is what broke it


def test_check_numbers_never_imports_the_accounts_own_number(monkeypatch):
    """Live: importing your own number creates a self-contact that Telegram reports as deleted but
    keeps forever. Ours must answer for it without touching the contact list."""
    import asyncio
    from pathlib import Path
    from types import SimpleNamespace

    from omnigram import telegram as tg

    imported: list[str] = []
    deleted: list = []

    class FakeClient:
        async def get_me(self):
            return SimpleNamespace(id=7, phone="84993297918", username="me_user",
                                   first_name="Me", last_name="Self")

        async def __call__(self, request):
            if hasattr(request, "contacts"):  # ImportContactsRequest
                imported.extend(c.phone for c in request.contacts)
                return SimpleNamespace(
                    imported=[SimpleNamespace(client_id=c.client_id, user_id=100 + c.client_id)
                              for c in request.contacts],
                    users=[SimpleNamespace(id=100 + c.client_id, username="", first_name="F", last_name="L")
                           for c in request.contacts],
                    retry_contacts=[])
            deleted.extend(request.id)  # DeleteContactsRequest
            return SimpleNamespace()

    class FakeContext:
        async def __aenter__(self):
            return FakeClient()

        async def __aexit__(self, *exc):
            return False

    async def fake_authorized(client):
        return None

    monkeypatch.setattr(tg, "_client", lambda *a, **k: FakeContext())
    monkeypatch.setattr(tg, "_authorized", fake_authorized)
    rows = asyncio.run(tg.check_numbers(Path("x.session"), 1, "hash", "",
                                        ["+84993297918", "+15555550100"]))
    assert imported == ["15555550100"], f"own number must not be imported, imported={imported}"
    own, other = rows
    assert own["registered"] is True and own["self"] is True and own["id"] == 7
    assert other["registered"] is True and other["self"] is False
    # only the number that was actually imported is deleted again; the own number is never touched
    assert [u.id for u in deleted] == [101], deleted
