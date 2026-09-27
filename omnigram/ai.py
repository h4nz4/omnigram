"""AI replies: provider settings, per-account/per-chat profiles, prompts, and the two model clients.

Two kinds of model, each doing what it is good at:
- a chat LLM (OpenRouter or any OpenAI-compatible endpoint) *writes* the reply;
- Jev (TypeSafe's System One model) makes the *decisions* around it: the contact's language and mood,
  whether the message needs a reply at all, whether the owner should take over, whether the other side is a
  bot, and whether a draft is safe to send. Jev returns typed probabilities, so the thresholds that act on
  them live in code (autopilot.py) where they can be read and changed.

The AI always writes as the account's real owner ("About me"); it is never set up as an invented person.
Nothing here imports Qt or Telethon: HTTP goes through urllib, and callers run it off the GUI thread.
"""
from __future__ import annotations

import json
import random
import re
import threading
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, time
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from omnigram import chat

OPENROUTER = "https://openrouter.ai/api/v1"
# Jev is served two ways with the same {state, questions} -> {answers} contract: through OpenRouter's Decisions
# API (the OpenRouter key; no "latest" alias there, so jev-latest maps to the current release), or TypeSafe direct.
JEV_OPENROUTER = "https://openrouter.ai/api/alpha/decisions"
JEV_OPENROUTER_LATEST = "typesafe/jev-1.13"
JEV_TYPESAFE = "https://api.typesafe.ai/v1/systemone"
DEFAULT_MODEL = "openai/gpt-4o-mini"
HANDOFF = "HANDOFF"  # what the LLM answers instead of a reply when the owner should take over


# ---- app-wide provider settings ---------------------------------------------------------------------

@dataclass
class ProviderConfig:
    """Settings → AI. `base_url` is only used for the custom provider; keys are never logged.

    Jev goes through OpenRouter by default. With OpenRouter as the provider that needs no second key; with a
    custom provider, `jev_key` is the OpenRouter key used for Jev. jev_via="typesafe" calls TypeSafe directly
    with `jev_key` as the TypeSafe key.
    """
    provider: str = "openrouter"  # openrouter | custom
    base_url: str = ""
    key: str = ""
    model: str = DEFAULT_MODEL
    jev_key: str = ""
    jev_model: str = "jev-latest"
    jev_via: str = "openrouter"  # openrouter | typesafe

    @property
    def url(self) -> str:
        return OPENROUTER if self.provider == "openrouter" else self.base_url.rstrip("/")

    @property
    def ready(self) -> bool:
        return bool(self.url and self.key)

    @property
    def jev_needs_own_key(self) -> bool:
        return self.jev_via == "typesafe" or self.provider != "openrouter"

    def jev_target(self) -> tuple[str, str, str]:
        """(endpoint, key, model) for a Jev request."""
        if self.jev_via == "typesafe":
            return JEV_TYPESAFE, self.jev_key, self.jev_model or "jev-latest"
        model = self.jev_model or "jev-latest"
        model = JEV_OPENROUTER_LATEST if model == "jev-latest" else model
        model = model if model.startswith("typesafe/") else f"typesafe/{model}"
        return JEV_OPENROUTER, self.jev_key if self.jev_needs_own_key else self.key, model

    @property
    def jev_ready(self) -> bool:
        return bool(self.jev_target()[1])


def _post(url: str, key: str, payload: dict, timeout: float, extra_headers: dict | None = None) -> dict:
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {key}", **(extra_headers or {})}
    request = Request(url, data=json.dumps(payload).encode(), method="POST", headers=headers)
    try:
        with urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as e:  # surface the provider's own explanation (bad key, unknown model, rate limit)
        body = e.read().decode("utf-8", "replace")[:300]
        raise ValueError(f"{url.split('/')[2]} answered {e.code}: {body}") from None


def list_models(config: ProviderConfig) -> list[tuple[str, str]]:
    """[(model id, display name)] from the provider's /models, for the Settings picker."""
    headers = {"Authorization": f"Bearer {config.key}"} if config.key else {}
    with urlopen(Request(config.url + "/models", headers=headers), timeout=20) as response:
        data = json.loads(response.read().decode("utf-8")).get("data", [])
    models = [(m["id"], m.get("name") or m["id"]) for m in data if m.get("id")]
    return sorted(models, key=lambda m: m[1].lower())


def complete(config: ProviderConfig, messages: list[dict], model: str = "", max_tokens: int = 400,
             temperature: float = 0.8) -> str:
    """One chat-completion call; returns the reply text. Blocking: run it in a worker thread."""
    if not config.ready:
        raise ValueError("no AI provider configured (Settings → AI)")
    payload = {"model": model or config.model or DEFAULT_MODEL, "messages": messages, "max_tokens": max_tokens,
               "temperature": temperature}
    extra = {"X-Title": "Omnigram"} if config.provider == "openrouter" else None
    body = _post(config.url + "/chat/completions", config.key, payload, 60, extra)
    try:
        return (body["choices"][0]["message"]["content"] or "").strip()
    except (KeyError, IndexError, TypeError):
        raise ValueError(f"unexpected answer from the AI provider: {str(body)[:200]}") from None


def jev(config: ProviderConfig, state, questions: dict) -> dict:
    """One Jev request: every question is answered against the same `state`, in parallel.
    Returns {question id: answer}; each answer has 'noul', or 'choice'/'probabilities'/'confidence',
    or 'score'/'confidence'. Blocking: run it in a worker thread."""
    if not config.jev_ready:
        raise ValueError("no key for Jev (Settings → AI)")
    url, key, model = config.jev_target()
    extra = {"X-Title": "Omnigram"} if url == JEV_OPENROUTER else None
    body = _post(url, key, {"state": state, "model": model, "questions": questions}, 30, extra)
    return body.get("answers", {})


# ---- profiles: account defaults + per-chat overrides --------------------------------------------------

MODES = ("off", "draft", "auto")
_STORE_LOCK = threading.Lock()  # one process-wide lock is plenty: these files are small and rarely written
WATCHERS: list = []  # watcher(path, store) after every ProfileStore.update


@dataclass
class Profile:
    """How the AI writes in one chat. Account defaults hold every field; a chat stores only what it overrides."""
    mode: str = "off"  # off | draft | auto (chats only; the account default is always off)
    model: str = ""  # "" = the app-wide model
    about_me: str = ""  # the account owner, in your words: the AI writes as this person
    primary_language: str = "English"
    secondary_language: str = ""
    instructions: str = ""  # anything else: topics to avoid, how to sign off, …
    formality: str = "neutral"  # casual | neutral | formal
    length: str = "short"  # short | medium | long
    emoji: str = "some"  # none | some | lots
    delay_min: int = 20  # seconds before an automatic reply starts
    delay_max: int = 90
    typing: bool = True  # show "typing…" for about as long as a person would take
    split: bool = True  # send a long reply as a few shorter messages
    follow_up: bool = True  # ask a question back / keep the conversation going
    max_in_row: int = 6  # automatic replies in a row before the chat is handed back to you
    active_hours: bool = False
    active_start: str = "09:00"
    active_end: str = "23:00"
    context: int = 20  # recent messages the AI sees
    group_max_per_hour: int = 4  # groups: at most this many automatic messages an hour…
    group_cooldown: int = 3  # …and at least this many minutes apart


PROFILE_FIELDS = [f.name for f in fields(Profile)]


def resolve(defaults: dict, override: dict) -> Profile:
    """The chat's effective profile: its own values where set, else the account's."""
    merged = {**asdict(Profile()), **{k: v for k, v in defaults.items() if k in PROFILE_FIELDS}}
    merged.update({k: v for k, v in override.items() if k in PROFILE_FIELDS and v is not None})
    return Profile(**merged)


@dataclass
class ChatState:
    """What happened in a chat, kept next to its profile: flags for you, and the in-a-row counter."""
    paused: bool = False  # you took over (sent a message yourself), or the AI handed off
    flag: str = ""  # why the chat needs you ("" = nothing)
    in_row: int = 0  # automatic replies since you last wrote
    sent_at: list[float] = field(default_factory=list)  # groups: when (epoch s) it answered in the last hour


@dataclass
class ProfileStore:
    """`<data>/ai/<session>.json`: {"defaults": {...}, "chats": {id: {"profile": {...}, "state": {...}}}}."""
    path: Path
    defaults: dict = field(default_factory=dict)
    chats: dict = field(default_factory=dict)  # str(chat id) -> {"profile": {...}, "state": {...}}

    @classmethod
    def load(cls, path: Path) -> ProfileStore:
        if not path.exists():
            return cls(path)
        data = json.loads(path.read_text("utf-8"))
        return cls(path, data.get("defaults", {}), data.get("chats", {}))

    @classmethod
    def update(cls, path: Path, change) -> ProfileStore:
        """Read, change(store), save — as one step. The GUI (editors, the chat window) and the background
        autopilot (its own thread) both write this file; every write goes through here so none is lost."""
        with _STORE_LOCK:
            store = cls.load(path)
            change(store)
            store.save()
        for watcher in list(WATCHERS):  # a server tells its desktops; a desktop tells the server (remote.py)
            watcher(path, store)
        return store

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")  # write-then-rename: never a half-written file
        tmp.write_text(json.dumps({"defaults": self.defaults, "chats": self.chats}, ensure_ascii=False, indent=1),
                       "utf-8")
        tmp.replace(self.path)

    def override(self, chat_id: int) -> dict:
        return self.chats.get(str(chat_id), {}).get("profile", {})

    def profile(self, chat_id: int) -> Profile:
        return resolve(self.defaults, self.override(chat_id))

    def set_override(self, chat_id: int, values: dict):
        """Store only the fields that differ from the account defaults (so later default changes still apply)."""
        base = asdict(resolve(self.defaults, {}))
        entry = self.chats.setdefault(str(chat_id), {})
        entry["profile"] = {k: v for k, v in values.items() if k in PROFILE_FIELDS and (k == "mode" or v != base[k])}

    def state(self, chat_id: int) -> ChatState:
        return ChatState(**self.chats.get(str(chat_id), {}).get("state", {}))

    def set_state(self, chat_id: int, state: ChatState):
        self.chats.setdefault(str(chat_id), {})["state"] = asdict(state)

    def auto_chats(self) -> list[int]:
        return [int(cid) for cid, entry in self.chats.items() if entry.get("profile", {}).get("mode") == "auto"]


# ---- prompts --------------------------------------------------------------------------------------------

LENGTHS = {"short": "one or two sentences", "medium": "a few sentences", "long": "a full, detailed paragraph"}
EMOJI = {"none": "Don't use emoji.", "some": "Use an emoji now and then, where a person would.",
         "lots": "Use emoji freely."}
FORMALITY = {"casual": "Write casually, like texting a friend.", "neutral": "Write in a friendly, natural tone.",
             "formal": "Write politely and formally."}


def reply_language(profile: Profile, detected: str | None) -> str:
    """Mirror the contact when they write in the primary or secondary language; otherwise the primary."""
    if detected == "secondary" and profile.secondary_language:
        return profile.secondary_language
    return profile.primary_language or "English"


def system_prompt(profile: Profile, chat_title: str, language: str | None, mood_hint: str = "",
                  group: bool = False) -> str:
    languages = profile.primary_language + (f" or {profile.secondary_language}" if profile.secondary_language
                                            else "")
    lang_rule = (f"Reply in {language}." if language else
                 f"Reply in the contact's language if it is {languages}; otherwise reply in "
                 f"{profile.primary_language}.")
    parts = [
        "You are writing Telegram messages as the owner of this account, in their voice. You are their "
        "assistant; never claim to be a different person, and never invent facts about the owner.",
        "Only say what the owner did, is doing or plans to do when the notes below say so. When asked about their "
        "day, week or plans and the notes don't cover it, answer warmly without specifics (the spirit of \"all "
        "good\", \"can't complain\", \"the usual\", \"busy but fine\" — vary it, in the reply's language; never "
        "mention work, projects, trips or plans that aren't in the notes) and turn the conversation back to the "
        "other person.",
        f"About the owner: {profile.about_me.strip()}" if profile.about_me.strip() else
        "Nothing is known about the owner beyond this chat; stay general about yourself.",
        *(group_rules(chat_title) if group else [f"You are chatting with: {chat_title}."]),
        lang_rule,
        FORMALITY.get(profile.formality, FORMALITY["neutral"]),
        f"Keep replies to {LENGTHS['short'] if group else LENGTHS.get(profile.length, LENGTHS['short'])}.",
        EMOJI.get(profile.emoji, EMOJI["some"]),
        "Ask a natural follow-up question when it keeps the conversation going." if profile.follow_up else
        "Answer what was said; don't push the conversation further.",
        f"If the message needs the owner personally — a commitment about money, meetings or promises, something "
        f"you can't answer from what you know, or a request to talk to the real person — reply with exactly "
        f"{HANDOFF} and nothing else.",
        "Write only the message text: no quotes, no name prefix, no explanation.",
    ]
    if mood_hint:
        parts.insert(3, mood_hint)
    if profile.instructions.strip():
        parts.append(f"Also: {profile.instructions.strip()}")
    return "\n".join(parts)


def group_rules(chat_title: str) -> list[str]:
    return [f"This is the group chat \"{chat_title}\". Several people talk here; each message from someone else "
            "starts with their name.",
            "Answer only the latest message: it is addressed to the owner. Reply to what it asks or says, on its "
            "topic. Don't greet the whole group, don't answer questions meant for someone else, and never speak "
            "for other members."]


def conversation(history: list[chat.Msg], limit: int) -> list[dict]:
    """The last `limit` messages as chat turns: yours are the assistant's, everyone else's the user's."""
    turns = []
    for msg in history[-limit:]:
        text = msg.text.strip() or f"[{msg.media_label or 'message'}]"
        if msg.out:
            turns.append({"role": "assistant", "content": text})
        else:
            turns.append({"role": "user", "content": f"{msg.sender}: {text}" if msg.sender else text})
    return turns


def messages_for(profile: Profile, history: list[chat.Msg], chat_title: str, language: str | None,
                 mood_hint: str = "", group: bool = False) -> list[dict]:
    return [{"role": "system", "content": system_prompt(profile, chat_title, language, mood_hint, group)},
            *conversation(history, profile.context)]


def clean_reply(text: str) -> str:
    """Strip what models add despite being told not to: surrounding quotes, a 'Me:' prefix."""
    text = text.strip().strip('"“”').strip()
    return re.sub(r"^(me|you|assistant)\s*:\s*", "", text, flags=re.IGNORECASE).strip()


def is_handoff(text: str) -> bool:
    return clean_reply(text).upper().rstrip(".!") == HANDOFF


# ---- pacing -------------------------------------------------------------------------------------------------

def split_reply(text: str, enabled: bool, max_parts: int = 3) -> list[str]:
    """A long reply as up to `max_parts` messages, split at paragraph or sentence ends — how people text."""
    text = text.strip()
    if not enabled or len(text) < 120:
        return [text]
    pieces = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    if len(pieces) == 1:
        pieces = [p.strip() for p in re.split(r"(?<=[.!?…])\s+", text) if p.strip()]
    merged: list[str] = []  # a piece with no words ("😊", "!!") stays with the one before: sent alone it looks off
    for piece in pieces:
        if merged and not re.search(r"\w", piece):
            merged[-1] += " " + piece
        else:
            merged.append(piece)
    pieces = merged
    if len(pieces) <= 1:
        return [text]
    size = -(-len(pieces) // min(max_parts, len(pieces)))  # ceil: spread evenly over at most max_parts
    return [" ".join(pieces[i:i + size]) for i in range(0, len(pieces), size)]


def typing_seconds(text: str) -> float:
    """About how long a person takes to type `text` (~6 characters a second), within 2-25 s."""
    return max(2.0, min(25.0, len(text) / 6))


def reply_delay(profile: Profile, rng: random.Random | None = None) -> float:
    low, high = sorted((max(0, profile.delay_min), max(0, profile.delay_max)))
    return (rng or random).uniform(low, high)


def within_active_hours(profile: Profile, now: datetime) -> bool:
    """Whether `now` (already in the account's local time) is inside the active window; always true when off."""
    if not profile.active_hours:
        return True
    start, end = (time.fromisoformat(profile.active_start), time.fromisoformat(profile.active_end))
    current = now.time()
    return start <= current < end if start < end else current >= start or current < end  # windows past midnight
