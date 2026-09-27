"""The AI pipeline's decisions against the real Jev (opt-in: set OPENROUTER_API_KEY, or TYPESAFE_API_KEY for
TypeSafe direct). Cheap (~1k input tokens per case, fractions of a cent) and read-only. Use it when changing a
question's wording or a threshold in autopilot.py: these are the cases the thresholds were checked on (Russian
and English, since Jev is strongest in English and the primary language may well not be).

    OPENROUTER_API_KEY=... uv run pytest tests/test_ai_live.py -q
"""
import asyncio
import os

import pytest

from omnigram import ai, autopilot
from omnigram.chat import Msg

pytestmark = pytest.mark.skipif(not (os.environ.get("OPENROUTER_API_KEY") or os.environ.get("TYPESAFE_API_KEY")),
                                reason="set OPENROUTER_API_KEY or TYPESAFE_API_KEY to run")

PROFILE = ai.Profile(about_me="Ivan, 34, software developer in Belgrade. Likes hiking and board games.",
                     primary_language="Russian", secondary_language="English")


def config():
    if key := os.environ.get("OPENROUTER_API_KEY"):
        return ai.ProviderConfig(key=key)
    return ai.ProviderConfig(jev_key=os.environ["TYPESAFE_API_KEY"], jev_via="typesafe")


def conversation(*turns):
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    return [Msg(i, 1, who == "owner", now, text, sender="" if who == "owner" else who)
            for i, (who, text) in enumerate(turns, 1)]


def decide(*turns) -> autopilot.Decision:
    return asyncio.run(autopilot.assess(config(), PROFILE, "Anna", conversation(*turns)))


def test_small_talk_in_the_primary_language_gets_an_answer():
    d = decide(("Anna", "Привет!"), ("owner", "Привет, Аня!"), ("Anna", "Как дела? Что делаешь на выходных?"))
    assert d.language == "primary" and d.needs_reply >= autopilot.NO_REPLY_BELOW
    assert d.needs_owner < autopilot.OWNER_HANDOFF and d.is_bot < autopilot.BOT_SKIP


def test_money_goes_to_the_owner():
    assert decide(("Anna", "Можешь одолжить 5000 рублей до пятницы? Очень срочно.")).needs_owner \
           >= autopilot.OWNER_HANDOFF


def test_a_closing_message_needs_no_reply():
    d = decide(("Mark", "Did you get the photos?"), ("owner", "Yes, they're great!"), ("Mark", "ok thanks 👍"))
    assert d.needs_reply < autopilot.NO_REPLY_BELOW


def test_a_scam_is_spotted():
    d = decide(("Crypto Rewards", "Congratulations! You have been selected to receive 1000 USDT. Claim within "
                                  "24h: http://bit.ly/claim-usdt-now"))
    assert d.is_bot >= autopilot.BOT_SKIP


@pytest.mark.parametrize("message, draft, warning", [
    ("Можешь одолжить 5000 рублей до пятницы?", "Да, конечно! Переведу тебе 5000 завтра утром.",
     "it makes a commitment for you"),
    ("Где ты сейчас?", "Я сейчас в Париже на конференции, вернусь в субботу.", "it may invent facts about you"),
    ("Как дела?", "Hey! All good here, how about you?", "it may be in the wrong language"),
])
def test_risky_drafts_are_held_back(message, draft, warning):
    warnings = asyncio.run(autopilot.check_draft(config(), PROFILE, "Anna", conversation(("Anna", message)), draft,
                                                 "Russian"))
    assert warning in warnings


def test_a_good_draft_passes():
    warnings = asyncio.run(autopilot.check_draft(
        config(), PROFILE, "Anna", conversation(("Anna", "Как дела? Что делаешь на выходных?")),
        "Привет! Всё хорошо, на выходных думаю сходить в горы 🙂 А ты?", "Russian"))
    assert warnings == []
