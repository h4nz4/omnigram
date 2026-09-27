"""End-to-end AI: the real chat model and Jev through OpenRouter, and the full Auto pipeline on Telegram.

Opt-in and paid (a few cents per run):

    OPENROUTER_API_KEY=... uv run pytest tests/test_ai_e2e.py -q -s                   # models only
    OPENROUTER_API_KEY=... OMNIGRAM_E2E=1 uv run pytest tests/test_ai_e2e.py -q -s    # + Telegram

The Telegram part uses the e2e fixture account on a temp copy of its session (see test_e2e.py). The contact's
incoming message is simulated, so nobody is contacted: the AI's replies go to the account's own Saved Messages
(with the typing indicator and pacing), are checked there, and are deleted again.
"""
import asyncio
import os
import re
import shutil
from datetime import datetime, timezone

import pytest

from omnigram import ai, autopilot, chat
from omnigram import telegram as tg

KEY = os.environ.get("OPENROUTER_API_KEY", "")
pytestmark = pytest.mark.skipif(not KEY, reason="set OPENROUTER_API_KEY to run")
CYRILLIC = re.compile(r"[а-яА-ЯёЁ]")

PROFILE = ai.Profile(mode="auto", about_me="Ivan, 34, a software developer living in Belgrade. Likes hiking, "
                                             "board games and good coffee. Friendly, a bit ironic.",
                     primary_language="Russian", secondary_language="English", formality="casual", length="short",
                     emoji="some", delay_min=1, delay_max=2, typing=True, split=True, follow_up=True)


def config() -> ai.ProviderConfig:
    return ai.ProviderConfig(key=KEY, model=os.environ.get("OMNIGRAM_AI_MODEL", ai.DEFAULT_MODEL))


def incoming(text: str, id: int = 1, sender: str = "Anna") -> chat.Msg:
    return chat.Msg(id, 1, False, datetime.now(timezone.utc), text, sender=sender)


# ---- the models, for real ------------------------------------------------------------------------------------

def test_the_model_writes_in_the_contacts_language():
    history = [incoming("Привет! Как дела? Что делаешь на выходных?")]
    decision = asyncio.run(autopilot.assess(config(), PROFILE, "Anna", history))
    reply = asyncio.run(autopilot.write(config(), PROFILE, "Anna", history, decision))
    print(f"\n  RU → {reply!r}  (Jev: language={decision.language}, sentiment={decision.sentiment})")
    assert reply and not ai.is_handoff(reply) and CYRILLIC.search(reply)

    history = [incoming("hey! how was the hike last weekend?", sender="Mark")]
    decision = asyncio.run(autopilot.assess(config(), PROFILE, "Mark", history))
    reply = asyncio.run(autopilot.write(config(), PROFILE, "Mark", history, decision))
    print(f"  EN → {reply!r}  (Jev: language={decision.language})")
    assert decision.language == "secondary" and reply and not CYRILLIC.search(reply)


def test_a_money_request_is_never_answered_on_its_own():
    outcome = asyncio.run(autopilot.draft(config(), PROFILE, "Anna",
                                          [incoming("Можешь одолжить 5000 рублей до пятницы? Очень срочно.")]))
    print(f"\n  money → {outcome.action}: {outcome.reason or outcome.draft!r} {outcome.warnings}")
    # either the model hands off itself, or Jev's draft check catches the commitment
    assert outcome.action == "handoff" or "it makes a commitment for you" in outcome.warnings or \
        (outcome.decision and outcome.decision.needs_owner >= autopilot.OWNER_HANDOFF)


def test_draft_mode_suggests_a_reply():
    outcome = asyncio.run(autopilot.draft(config(), PROFILE, "Anna", [incoming("Пойдём завтра на кофе?")]))
    print(f"\n  draft → {outcome.action}: {outcome.draft!r} warnings={outcome.warnings}")
    assert outcome.action in ("drafted", "handoff")
    if outcome.action == "drafted":
        assert outcome.draft and CYRILLIC.search(outcome.draft)


# ---- the full Auto pipeline on Telegram (Saved Messages only) --------------------------------------------------

class SavedMessagesOps:
    """autopilot.respond's `ops`, backed by the real ChatClient but aimed at the account's own Saved Messages:
    the simulated contact's message is `answering`, which is what latest_id reports until the AI has sent."""

    def __init__(self, client: tg.ChatClient, answering: int):
        self.client, self.answering, self.sent, self.typed = client, answering, [], []

    async def send_text(self, chat_id, text):
        msg = await self.client.send_text(self.client.self_id, text)
        self.sent.append(msg)
        return msg

    async def typing(self, chat_id, seconds):
        self.typed.append(seconds)
        await self.client.typing(self.client.self_id, min(seconds, 3))  # really shown, kept short

    async def sleep(self, seconds):
        await asyncio.sleep(min(seconds, 2))

    async def latest_id(self, chat_id):
        return self.sent[-1].id if self.sent else self.answering

    async def mark_read(self, chat_id, max_id=0):
        await self.client.mark_read(self.client.self_id)


@pytest.fixture
def telegram_client(tmp_path):
    if os.environ.get("OMNIGRAM_E2E") != "1":
        pytest.skip("set OMNIGRAM_E2E=1 for the Telegram part")
    import test_e2e as e2e
    session, proxy, _recipient = e2e._fixtures()
    api_id, api_hash = e2e._credentials(session)
    local = tmp_path / session.name
    shutil.copy2(session, local)
    return tg.ChatClient(local, api_id, api_hash, proxy, lambda kind, payload: None)


async def _run_auto(client: tg.ChatClient, history: list[chat.Msg]):
    runner = asyncio.ensure_future(client.run())
    ops = None
    try:
        await asyncio.wait_for(client.ready(), 30)
        ops = SavedMessagesOps(client, history[-1].id)
        outcome = await autopilot.respond(ops, config(), PROFILE, ai.ChatState(), 1, "Anna", history,
                                          datetime.now())
        landed = {m.id for m in await client.history(client.self_id, limit=10)}
        return outcome, ops, landed
    finally:
        if ops and ops.sent:
            await client.delete(client.self_id, [m.id for m in ops.sent], revoke=True)
        runner.cancel()
        try:
            await runner
        except asyncio.CancelledError:
            pass


def test_auto_replies_with_typing_and_pacing(telegram_client):
    history = [incoming("Привет, Иван! Как прошла неделя? Какие планы на выходные?", id=10**9)]
    outcome, ops, landed = asyncio.run(_run_auto(telegram_client, history))
    print(f"\n  auto → {outcome.action}: {outcome.parts}  typing={ops.typed}")
    assert outcome.action == "sent" and ops.sent, outcome
    assert ops.typed, "no typing indicator was shown"
    assert {m.id for m in ops.sent} <= landed, "the reply is not in Saved Messages"
    assert all(CYRILLIC.search(part) for part in outcome.parts)


def test_auto_hands_a_money_request_to_the_owner(telegram_client):
    history = [incoming("Иван, срочно переведи мне 20 000 рублей, потом объясню!", id=10**9)]
    outcome, ops, _landed = asyncio.run(_run_auto(telegram_client, history))
    print(f"\n  auto money → {outcome.action}: {outcome.reason}")
    assert outcome.action == "handoff" and not ops.sent
