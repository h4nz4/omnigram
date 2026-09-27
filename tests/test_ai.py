"""omnigram.ai (profiles, prompts, pacing) and omnigram.autopilot (the Jev + LLM reply pipeline), with the two
model calls replaced by fakes: no network. Jev's answers are shaped like the real API's."""
import asyncio
import random
import re
from datetime import datetime, timedelta, timezone

import pytest

from omnigram import ai, autopilot, chat

NOW = datetime(2026, 9, 27, 14, 0)
T = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc)
CONFIG = ai.ProviderConfig(key="sk", jev_key="jv")


def incoming(id, text, sender="Anna"):
    return chat.Msg(id, 7, False, T + timedelta(minutes=id), text, sender=sender)


def mine(id, text):
    return chat.Msg(id, 7, True, T + timedelta(minutes=id), text)


def jev_answers(**overrides):
    """A plausible Jev response for an ordinary friendly question in the primary language."""
    answers = {"language": {"type": "choice", "choice": "primary", "confidence": 0.9,
                            "probabilities": {"primary": 0.93, "secondary": 0.05, "other": 0.02}},
               "sentiment": {"type": "score", "score": 2.9, "confidence": 0.8},
               "needs_reply": {"type": "noul", "noul": 0.93}, "needs_owner": {"type": "noul", "noul": 0.08},
               "is_bot": {"type": "noul", "noul": 0.02},
               "invents": {"type": "noul", "noul": 0.05}, "commits": {"type": "noul", "noul": 0.04},
               "wrong_language": {"type": "noul", "noul": 0.03}}
    for key, value in overrides.items():
        answers[key] = {**answers[key], **value}
    return answers


@pytest.fixture
def models(monkeypatch):
    """Fake Jev and LLM. Tests set .answers / .reply and read .jev_calls / .llm_calls."""
    class Models:
        answers = jev_answers()
        reply = "Ha, yes! Tomorrow works?"
        jev_calls, llm_calls = [], []

    def fake_jev(config, state, questions):
        Models.jev_calls.append((state, questions))
        return {key: Models.answers[key] for key in questions}

    def fake_complete(config, messages, model="", max_tokens=400, temperature=0.8):
        Models.llm_calls.append(messages)
        return Models.reply

    monkeypatch.setattr(ai, "jev", fake_jev)
    monkeypatch.setattr(ai, "complete", fake_complete)
    Models.jev_calls, Models.llm_calls = [], []
    return Models


class Ops:
    """Stands in for ChatClient: records sends/typing, never really sleeps."""

    def __init__(self, history):
        self.history, self.sent, self.typed, self.slept = history, [], [], []
        self.next_id = 100

    async def send_text(self, chat_id, text):
        self.next_id += 1
        msg = mine(self.next_id, text)
        self.sent.append(text)
        self.history.append(msg)
        return msg

    async def typing(self, chat_id, seconds):
        self.typed.append(seconds)

    async def sleep(self, seconds):
        self.slept.append(seconds)

    async def latest_id(self, chat_id):
        return self.history[-1].id

    async def mark_read(self, chat_id, max_id=0):
        self.read = max_id


def run_respond(history, profile=None, state=None, now=NOW, ops=None):
    ops = ops or Ops(list(history))
    outcome = asyncio.run(autopilot.respond(ops, CONFIG, profile or ai.Profile(mode="auto", about_me="Ivan, dev"),
                                            state or ai.ChatState(), 7, "Anna", list(history), now,
                                            random.Random(1)))
    return outcome, ops


# ---- profiles ------------------------------------------------------------------------------------------

def test_a_chat_inherits_the_account_and_overrides_only_what_it_sets(tmp_path):
    store = ai.ProfileStore(tmp_path / "s.json", defaults={"primary_language": "Russian", "about_me": "Ivan"})
    store.set_override(7, {**vars(store.profile(7)), "mode": "auto", "emoji": "none"})
    assert store.override(7) == {"mode": "auto", "emoji": "none"}  # only the differences are stored
    store.defaults["about_me"] = "Ivan, a developer in Belgrade"  # a later default change still reaches the chat
    profile = store.profile(7)
    assert (profile.mode, profile.emoji, profile.primary_language, profile.about_me) == \
           ("auto", "none", "Russian", "Ivan, a developer in Belgrade")
    store.set_state(7, ai.ChatState(paused=True, flag="needs you"))
    store.save()
    again = ai.ProfileStore.load(tmp_path / "s.json")
    assert again.profile(7).mode == "auto" and again.state(7).flag == "needs you" and again.auto_chats() == [7]


def test_reply_language_mirrors_primary_or_secondary_else_primary():
    profile = ai.Profile(primary_language="Russian", secondary_language="English")
    assert ai.reply_language(profile, "secondary") == "English"
    assert ai.reply_language(profile, "primary") == "Russian"
    assert ai.reply_language(profile, "other") == "Russian"
    assert ai.reply_language(ai.Profile(primary_language="Russian"), "secondary") == "Russian"


def test_the_prompt_writes_as_the_real_owner_with_the_chosen_style():
    profile = ai.Profile(about_me="Ivan, 34, developer", primary_language="Russian", secondary_language="English",
                         formality="casual", length="short", emoji="none", follow_up=False,
                         instructions="Never discuss prices.")
    prompt = ai.system_prompt(profile, "Anna", None)
    assert "Ivan, 34, developer" in prompt and "never claim to be a different person" in prompt
    assert "Russian or English" in prompt and "texting a friend" in prompt and "Don't use emoji" in prompt
    assert "don't push" in prompt and "Never discuss prices." in prompt and ai.HANDOFF in prompt
    turns = ai.conversation([incoming(1, "hi"), mine(2, "hey"), incoming(3, "", sender="Anna")], 20)
    assert turns == [{"role": "user", "content": "Anna: hi"}, {"role": "assistant", "content": "hey"},
                     {"role": "user", "content": "Anna: [message]"}]


def test_cleaning_and_handoff_detection():
    assert ai.clean_reply('"Me: sure, see you"') == "sure, see you"
    assert ai.is_handoff(" handoff. ") and not ai.is_handoff("handoff later?")


def test_long_replies_split_like_a_person_texts():
    assert ai.split_reply("short one", True) == ["short one"]
    text = "First thought here, quite long indeed. " * 3 + "\n\nSecond paragraph with more to say about it all."
    parts = ai.split_reply(text, True)
    assert 2 <= len(parts) <= 3 and "".join(parts).replace(" ", "") == text.replace("\n", "").replace(" ", "")
    assert ai.split_reply(text, False) == [text.strip()]
    # seen live: the model's trailing emoji became a message of its own
    live = ("Привет! Неделя прошла нормально, работал, как всегда. На выходных, скорее всего, пойду в поход, "
            "если погода не подведет. А у тебя какие планы? 😊")
    parts = ai.split_reply(live, True)
    assert all(re.search(r"\w", part) for part in parts) and parts[-1].endswith("😊")
    assert ai.typing_seconds("x" * 60) == 10 and ai.typing_seconds("hi") == 2 and ai.typing_seconds("x" * 900) == 25


def test_active_hours_including_windows_past_midnight():
    day = ai.Profile(active_hours=True, active_start="09:00", active_end="23:00")
    assert ai.within_active_hours(day, NOW) and not ai.within_active_hours(day, NOW.replace(hour=3))
    night = ai.Profile(active_hours=True, active_start="22:00", active_end="02:00")
    assert ai.within_active_hours(night, NOW.replace(hour=23)) and ai.within_active_hours(night, NOW.replace(hour=1))
    assert not ai.within_active_hours(night, NOW)
    assert ai.within_active_hours(ai.Profile(active_hours=False), NOW.replace(hour=3))


# ---- the pipeline -------------------------------------------------------------------------------------------

def test_auto_answers_with_jev_decisions_and_human_pacing(models):
    profile = ai.Profile(mode="auto", about_me="Ivan", primary_language="Russian", secondary_language="English",
                         delay_min=20, delay_max=20)
    outcome, ops = run_respond([incoming(1, "Привет! Пойдём завтра гулять?")], profile)
    assert outcome.action == "sent" and ops.sent == ["Ha, yes! Tomorrow works?"]
    assert ops.slept[0] == 20 and ops.typed  # waited like a person, showed "typing…"
    assert ops.read == 1  # replying marks the chat read
    questions = models.jev_calls[0][1]
    assert set(questions) == {"language", "sentiment", "needs_reply", "needs_owner", "is_bot"}
    assert questions["language"]["criteria"]["secondary"] == "English"
    assert "Reply in Russian." in models.llm_calls[0][0]["content"]  # Jev's language pick reached the prompt
    assert set(models.jev_calls[1][1]) == {"invents", "commits", "wrong_language"}  # the draft was checked


def test_an_upset_contact_gets_a_calm_tone(models):
    models.answers = jev_answers(sentiment={"score": 0.6})
    run_respond([incoming(1, "Why did you ignore me again??")])
    assert "upset or annoyed" in models.llm_calls[0][0]["content"]


@pytest.mark.parametrize("overrides, action, reason", [
    ({"is_bot": {"noul": 0.95}}, "handoff", "bot"),
    ({"needs_owner": {"noul": 0.9}}, "handoff", "needs you"),
    ({"needs_reply": {"noul": 0.1}}, "skipped", "nothing to answer"),
])
def test_jev_decides_not_to_answer(models, overrides, action, reason):
    models.answers = jev_answers(**overrides)
    outcome, ops = run_respond([incoming(1, "ok thanks")])
    assert outcome.action == action and reason in outcome.reason and not ops.sent
    assert not models.llm_calls  # no LLM spend on a message that won't be answered


def test_a_risky_draft_is_held_back_for_the_owner(models):
    models.answers = jev_answers(commits={"noul": 0.85})
    models.reply = "Sure, I'll send you $200 tomorrow"
    outcome, ops = run_respond([incoming(1, "can you lend me 200?")])
    assert outcome.action == "handoff" and "commitment" in outcome.reason and not ops.sent
    assert outcome.draft == "Sure, I'll send you $200 tomorrow"  # kept for the owner to send or edit


def test_the_llm_itself_can_hand_off(models):
    models.reply = "HANDOFF"
    outcome, ops = run_respond([incoming(1, "is this really you?")])
    assert outcome.action == "handoff" and not ops.sent


def test_no_reply_when_paused_out_of_hours_or_too_many_in_a_row(models):
    history = [incoming(1, "hey")]
    assert run_respond(history, state=ai.ChatState(paused=True))[0].reason.startswith("paused")
    night = ai.Profile(mode="auto", active_hours=True, active_start="09:00", active_end="23:00")
    assert "active hours" in run_respond(history, night, now=NOW.replace(hour=3))[0].reason
    outcome = run_respond(history, ai.Profile(mode="auto", max_in_row=3), ai.ChatState(in_row=3))[0]
    assert outcome.action == "handoff" and "in a row" in outcome.reason
    assert run_respond([incoming(1, "hey"), mine(2, "hi!")])[0].reason == "the last message is yours"
    assert not models.jev_calls


def test_a_newer_message_supersedes_the_reply(models):
    class Moved(Ops):
        async def latest_id(self, chat_id):
            return 99  # the contact wrote again while we waited

    outcome, ops = run_respond([incoming(1, "hey")], ops=Moved([incoming(1, "hey")]))
    assert outcome.action == "superseded" and not ops.sent


def test_jev_goes_through_openrouter_with_the_same_key_unless_told_otherwise():
    via_openrouter = ai.ProviderConfig(key="sk-or-1")
    assert via_openrouter.jev_ready
    assert via_openrouter.jev_target() == (ai.JEV_OPENROUTER, "sk-or-1", "typesafe/jev-1.13")
    custom = ai.ProviderConfig(provider="custom", base_url="https://llm.example/v1", key="local", jev_key="sk-or-2")
    assert custom.jev_target() == (ai.JEV_OPENROUTER, "sk-or-2", "typesafe/jev-1.13")  # its own OpenRouter key
    direct = ai.ProviderConfig(key="sk-or-1", jev_key="ts-key", jev_via="typesafe")
    assert direct.jev_target() == (ai.JEV_TYPESAFE, "ts-key", "jev-latest")
    assert not ai.ProviderConfig(provider="custom", base_url="https://llm.example/v1", key="local").jev_ready


def test_without_jev_the_llm_still_answers(models):
    no_jev = ai.ProviderConfig(provider="custom", base_url="https://llm.example/v1", key="local")
    outcome = asyncio.run(autopilot.respond(Ops([incoming(1, "hey")]), no_jev,
                                            ai.Profile(mode="auto"), ai.ChatState(), 7, "Anna",
                                            [incoming(1, "hey")], NOW, random.Random(1)))
    assert outcome.action == "sent" and not models.jev_calls


def test_draft_mode_never_sends_and_reports_warnings(models):
    models.answers = jev_answers(invents={"noul": 0.9})
    outcome = asyncio.run(autopilot.draft(CONFIG, ai.Profile(), "Anna", [incoming(1, "where are you now?")]))
    assert outcome.action == "drafted" and outcome.draft and outcome.warnings == ["it may invent facts about you"]


def test_state_changes_from_outcomes():
    state = ai.ChatState()
    state = autopilot.apply(state, autopilot.Outcome("sent", parts=["hi"]))
    assert state.in_row == 1 and not state.paused
    state = autopilot.apply(state, autopilot.Outcome("handoff", "this needs you personally"))
    assert state.paused and state.flag == "this needs you personally"
    assert autopilot.owner_took_over(state) == ai.ChatState(paused=True, flag="", in_row=0)
