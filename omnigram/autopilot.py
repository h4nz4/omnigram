"""The AI reply pipeline, shared by the chat window (Draft and Auto modes) and the background autopilot.

    incoming message
      → Jev: which language, what mood, does it need a reply, does the owner need to take over, is it a bot?
      → the chat LLM writes a reply in the owner's voice (or answers HANDOFF)
      → Jev checks the draft: invented facts about the owner? a commitment? the wrong language?
      → pacing: a human delay, "typing…", long replies split — then a freshness check before each send

Jev answers are probabilities; the THRESHOLDS below turn them into actions, in code, where they can be tuned.
Without a Jev key the decision steps are skipped (every message is answered unless the LLM itself hands off),
which is why Settings recommends one.

Nothing here touches Telethon or Qt: sending goes through an `ops` object (ChatClient in the window, a small
Telethon adapter in the background runner, a fake in tests).
"""
from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass, field
from datetime import datetime

from omnigram import ai, chat

# When Jev's probability crosses these, the pipeline acts. Tuned conservatively: when in doubt, hand the chat to
# the owner rather than send. Revisit them on real conversations (Jev is strongest in English; other languages
# work with lower accuracy, per TypeSafe's docs).
BOT_SKIP = 0.8  # is_bot at or above this: don't answer, flag the chat
OWNER_HANDOFF = 0.6  # needs_owner at or above this: hand the chat back to the owner
NO_REPLY_BELOW = 0.35  # needs_reply below this: nothing to answer ("ok", "thanks", a lone sticker)
DRAFT_ISSUE = 0.6  # a draft check at or above this: don't auto-send; show the draft to the owner instead
LANGUAGE_CONFIDENCE = 0.4  # below this, let the LLM pick the language itself (from the rule in the prompt)
UPSET_AT_OR_BELOW, WARM_AT_OR_ABOVE = 1.2, 3.0  # sentiment levels 0-4 that change the tone hint

SENTIMENT_LEVELS = ["Hostile or very upset", "Annoyed or negative", "Neutral", "Friendly or positive",
                    "Very warm or enthusiastic"]


@dataclass
class Decision:
    """Jev's reading of the latest incoming message. None = not assessed (no Jev key)."""
    language: str | None = None  # primary | secondary | other
    language_confidence: float = 0.0
    sentiment: float | None = None  # 0 (hostile) … 4 (very warm)
    needs_reply: float | None = None
    needs_owner: float | None = None
    is_bot: float | None = None

    def mood_hint(self) -> str:
        if self.sentiment is None:
            return ""
        if self.sentiment <= UPSET_AT_OR_BELOW:
            return "The contact seems upset or annoyed: stay calm, patient and kind; don't argue."
        if self.sentiment >= WARM_AT_OR_ABOVE:
            return "The contact is warm and friendly: match their energy."
        return ""


@dataclass
class Outcome:
    action: str  # sent | drafted | skipped | handoff | superseded
    reason: str = ""
    parts: list[str] = field(default_factory=list)  # what was sent
    draft: str = ""  # the text the AI wrote (drafted, or held back on handoff)
    warnings: list[str] = field(default_factory=list)
    decision: Decision | None = None
    messages: list[chat.Msg] = field(default_factory=list)  # the sent messages, for the chat window to show


# ---- Jev: the decisions -----------------------------------------------------------------------------------

def _state(profile: ai.Profile, chat_title: str, history: list[chat.Msg]) -> dict:
    """What Jev sees: the owner, the chat, the recent conversation and the message being answered."""
    turns = [{"from": "owner" if m.out else (m.sender or "contact"),
              "text": m.text.strip() or f"[{m.media_label or 'message'}]"} for m in history[-profile.context:]]
    latest = next((t["text"] for t in reversed(turns) if t["from"] != "owner"), "")
    return {"owner": {"about": profile.about_me.strip() or "(nothing written)",
                      "languages": [lang for lang in (profile.primary_language, profile.secondary_language) if lang]},
            "chat": {"with": chat_title}, "conversation": turns, "latest_message": latest}


def assessment_questions(profile: ai.Profile) -> dict:
    languages = {"primary": f"{profile.primary_language}"}
    if profile.secondary_language:
        languages["secondary"] = profile.secondary_language
    languages["other"] = "Any other language, or too short or ambiguous to tell"
    return {
        "language": {"type": "choice", "instructions": "Which language is `latest_message` written in?",
                     "criteria": languages},
        "sentiment": {"type": "score",
                      "instructions": "How does the contact feel in `latest_message`, read in the context of "
                                      "`conversation`?",
                      "criteria": SENTIMENT_LEVELS},
        "needs_reply": {"type": "noul", "instructions": "Would a person naturally write back to `latest_message`?",
                        "criteria": {"true": "It asks or requests something, shares news that invites an answer, "
                                             "or opens a conversation.",
                                     "false": "It closes the exchange (ok, thanks, bye, a lone emoji or sticker) "
                                              "or needs no answer."}},
        "needs_owner": {"type": "noul",
                        "instructions": "Does `latest_message` need the account owner personally, rather than an "
                                        "assistant writing in their voice from `owner.about` and `conversation`?",
                        "criteria": {"true": "A request to talk to the real person or to call; money, prices, "
                                             "payments, meetings, dates or promises; a sensitive, emotional or "
                                             "safety matter; or a question about the owner that `owner.about` "
                                             "does not answer.",
                                     "false": "Everyday conversation that can be answered from `owner.about` and "
                                              "`conversation`."}},
        "is_bot": {"type": "noul",
                   "instructions": "Is the other side of `conversation` an automated bot, a spammer or a scam "
                                   "attempt, rather than a person talking to the owner?"},
    }


def read_assessment(answers: dict) -> Decision:
    language = answers.get("language", {})
    return Decision(language=language.get("choice"), language_confidence=float(language.get("confidence", 0)),
                    sentiment=answers.get("sentiment", {}).get("score"),
                    needs_reply=answers.get("needs_reply", {}).get("noul"),
                    needs_owner=answers.get("needs_owner", {}).get("noul"),
                    is_bot=answers.get("is_bot", {}).get("noul"))


async def assess(config: ai.ProviderConfig, profile: ai.Profile, chat_title: str,
                 history: list[chat.Msg]) -> Decision:
    if not config.jev_ready:
        return Decision()
    answers = await asyncio.to_thread(ai.jev, config, _state(profile, chat_title, history),
                                      assessment_questions(profile))
    return read_assessment(answers)


def check_questions() -> dict:
    return {
        "invents": {"type": "noul",
                    "instructions": "Does `draft` state facts about the owner (plans, history, whereabouts, "
                                    "preferences, identity) that neither `owner.about` nor `conversation` supports?"},
        "commits": {"type": "noul",
                    "instructions": "Does `draft` agree to or promise something on the owner's behalf: money, a "
                                    "price, a payment, a meeting, a date, a call, or another personal commitment?"},
        "wrong_language": {"type": "noul",
                           "instructions": "Is `draft` written in a language other than `expected_language`?"},
    }


DRAFT_WARNINGS = {"invents": "it may invent facts about you", "commits": "it makes a commitment for you",
                  "wrong_language": "it may be in the wrong language"}


async def check_draft(config: ai.ProviderConfig, profile: ai.Profile, chat_title: str, history: list[chat.Msg],
                      draft: str, language: str | None) -> list[str]:
    """Jev's second look, at the reply before it goes out. Returns human-readable warnings (empty = fine)."""
    if not config.jev_ready:
        return []
    state = {**_state(profile, chat_title, history), "draft": draft,
             "expected_language": language or profile.primary_language}
    answers = await asyncio.to_thread(ai.jev, config, state, check_questions())
    return [DRAFT_WARNINGS[key] for key in DRAFT_WARNINGS
            if answers.get(key, {}).get("noul", 0) >= DRAFT_ISSUE]


# ---- the LLM: the words ---------------------------------------------------------------------------------------

def _language(profile: ai.Profile, decision: Decision) -> str | None:
    if decision.language is None or decision.language_confidence < LANGUAGE_CONFIDENCE:
        return None  # the prompt's own rule decides
    return ai.reply_language(profile, decision.language)


async def write(config: ai.ProviderConfig, profile: ai.Profile, chat_title: str, history: list[chat.Msg],
                decision: Decision) -> str:
    messages = ai.messages_for(profile, history, chat_title, _language(profile, decision), decision.mood_hint())
    max_tokens = {"short": 150, "medium": 350, "long": 700}.get(profile.length, 300)
    return ai.clean_reply(await asyncio.to_thread(ai.complete, config, messages, profile.model, max_tokens))


# ---- the two modes -----------------------------------------------------------------------------------------------

async def draft(config: ai.ProviderConfig, profile: ai.Profile, chat_title: str,
                history: list[chat.Msg]) -> Outcome:
    """Draft mode: a suggested reply for the owner to edit and send; nothing is sent here."""
    decision = await assess(config, profile, chat_title, history)
    text = await write(config, profile, chat_title, history, decision)
    if ai.is_handoff(text):
        return Outcome("handoff", "the AI thinks you should answer this one yourself", decision=decision)
    warnings = await check_draft(config, profile, chat_title, history, text, _language(profile, decision))
    return Outcome("drafted", draft=text, warnings=warnings, decision=decision)


async def respond(ops, config: ai.ProviderConfig, profile: ai.Profile, state: ai.ChatState, chat_id: int,
                  chat_title: str, history: list[chat.Msg], now_local: datetime,
                  rng: random.Random | None = None) -> Outcome:
    """Auto mode: decide, write, check and — if everything holds — send with human pacing.

    `ops` provides: send_text(chat_id, text) -> chat.Msg, typing(chat_id, seconds), sleep(seconds),
    latest_id(chat_id) -> int, mark_read(chat_id, max_id). The caller applies the outcome to the chat's ChatState
    and saves it.
    """
    if not history or history[-1].out:
        return Outcome("skipped", "the last message is yours")
    if state.paused:
        return Outcome("skipped", "paused: you took over this chat")
    if not ai.within_active_hours(profile, now_local):
        return Outcome("skipped", "outside active hours; it waits")
    if state.in_row >= profile.max_in_row:
        return Outcome("handoff", f"{state.in_row} automatic replies in a row — your turn")
    answering = history[-1].id

    decision = await assess(config, profile, chat_title, history)
    if (decision.is_bot or 0) >= BOT_SKIP:
        return Outcome("handoff", "looks like a bot or spam — not answering", decision=decision)
    if (decision.needs_owner or 0) >= OWNER_HANDOFF:
        return Outcome("handoff", "this needs you personally", decision=decision)
    if decision.needs_reply is not None and decision.needs_reply < NO_REPLY_BELOW:
        return Outcome("skipped", "nothing to answer", decision=decision)

    text = await write(config, profile, chat_title, history, decision)
    if ai.is_handoff(text):
        return Outcome("handoff", "the AI asked you to take over", decision=decision)
    warnings = await check_draft(config, profile, chat_title, history, text, _language(profile, decision))
    if warnings:
        return Outcome("handoff", "held back: " + "; ".join(warnings), draft=text, warnings=warnings,
                       decision=decision)

    await ops.sleep(ai.reply_delay(profile, rng))
    sent: list[chat.Msg] = []
    for number, part in enumerate(ai.split_reply(text, profile.split)):
        if number:
            await ops.sleep((rng or random).uniform(1.0, 3.0))
        if profile.typing:
            await ops.typing(chat_id, ai.typing_seconds(part))
        # the conversation may have moved on while we waited or "typed": a newer message gets its own reply
        latest = await ops.latest_id(chat_id)
        if latest != answering and latest not in {m.id for m in sent}:
            return Outcome("sent" if sent else "superseded", "a newer message arrived", [m.text for m in sent],
                           text, decision=decision, messages=sent)
        sent.append(await ops.send_text(chat_id, part))
    await ops.mark_read(chat_id, answering)  # replying reads the chat, as it does in Telegram
    return Outcome("sent", parts=[m.text for m in sent], draft=text, decision=decision, messages=sent)


def apply(state: ai.ChatState, outcome: Outcome) -> ai.ChatState:
    """How an outcome changes the chat's state: a reply counts toward the in-a-row limit; a handoff pauses the
    chat and flags it for the owner."""
    if outcome.action == "sent":
        return ai.ChatState(state.paused, state.flag, state.in_row + 1)
    if outcome.action == "handoff":
        return ai.ChatState(True, outcome.reason, state.in_row)
    return state


def owner_took_over(state: ai.ChatState) -> ai.ChatState:
    """The owner wrote in the chat themselves: Auto stops there until they switch it back on."""
    return ai.ChatState(paused=True, flag="", in_row=0)
