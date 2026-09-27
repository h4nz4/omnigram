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


def _fake_warmup(monkeypatch, step):
    """warmup_run with the network, the clock and sleeping replaced; returns the recorded sleeps."""
    from omnigram import telegram as tg

    sleeps = []

    class FakeContext:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *exc):
            return False

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    async def fake_authorized(client):
        return None

    monkeypatch.setattr(tg, "_client", lambda *a, **k: FakeContext())
    monkeypatch.setattr(tg, "_authorized", fake_authorized)
    monkeypatch.setattr(tg, "_warmup_step", step)
    monkeypatch.setattr(tg.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(tg.time, "time", lambda: 10_000.0)
    return sleeps


def test_warmup_run_waits_for_each_action_and_skips_long_missed_ones(monkeypatch):
    """After a restart, actions whose time passed long ago are skipped — never fired in a burst."""
    import asyncio
    from pathlib import Path

    from omnigram import telegram as tg
    from omnigram.warmup import Action

    done_kinds, progress = [], []

    async def step(client, kind, targets, emit):
        done_kinds.append(kind)

    sleeps = _fake_warmup(monkeypatch, step)
    actions = [Action("read", 0, 10_000 - tg.MISSED_AFTER - 1),  # missed while the app was closed
               Action("pause", 0, 10_000 - 5),                 # a little late: still runs, right away
               Action("online", 0, 10_300)]                     # runs after waiting 300 s
    result = asyncio.run(tg.warmup_run(Path("x.session"), 1, "h", "", actions, [], print, 0, progress.append))
    assert done_kinds == ["pause", "online"]
    assert sleeps == [0.0, 300.0]
    assert progress == [1, 2, 3]
    assert result == "skipped 1 missed action(s)"


def test_warmup_run_resumes_from_done(monkeypatch):
    import asyncio
    from pathlib import Path

    from omnigram import telegram as tg
    from omnigram.warmup import Action

    done_kinds = []

    async def step(client, kind, targets, emit):
        done_kinds.append(kind)

    _fake_warmup(monkeypatch, step)
    actions = [Action("read", 0, 10_000), Action("online", 0, 10_000)]
    asyncio.run(tg.warmup_run(Path("x.session"), 1, "h", "", actions, [], print, 1))
    assert done_kinds == ["online"]


def test_warmup_run_honours_flood_wait(monkeypatch):
    """Telethon only sleeps short flood waits itself; a long one must pause the run, not be ignored."""
    import asyncio
    from pathlib import Path

    from telethon import errors

    from omnigram import telegram as tg
    from omnigram.warmup import Action

    async def step(client, kind, targets, emit):
        if kind == "read":
            raise errors.FloodWaitError(request=None, capture=900)

    sleeps = _fake_warmup(monkeypatch, step)
    lines = []
    actions = [Action("read", 0, 10_000), Action("online", 0, 10_000)]
    asyncio.run(tg.warmup_run(Path("x.session"), 1, "h", "", actions, [], lines.append))
    assert 900 in sleeps
    assert any("flood wait" in line for line in lines)


def test_warmup_run_stops_when_the_session_is_logged_out(monkeypatch):
    import asyncio
    from pathlib import Path

    import pytest

    from omnigram import telegram as tg
    from omnigram.warmup import Action

    async def step(client, kind, targets, emit):
        raise tg.NotAuthorized("session is not logged in")

    _fake_warmup(monkeypatch, step)
    with pytest.raises(tg.NotAuthorized):
        asyncio.run(tg.warmup_run(Path("x.session"), 1, "h", "", [Action("read", 0, 10_000)], [], print))
