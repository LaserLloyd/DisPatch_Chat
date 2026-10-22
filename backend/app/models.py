"""Pydantic models for REST/WebSocket payloads."""

from __future__ import annotations

from datetime import date as _date
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class BotOut(BaseModel):
    id: str
    name: str
    emoji: str = ""
    avatar: str
    avatar_url: str
    model_hint: str = ""
    order: int = 0
    visible: bool = True
    safe: bool = False
    color: str = ""
    reactions: bool = False       # may this bot fire reaction images?
    image_jobs: bool = False      # may this bot request a generated picture?
    image_workflow: str = ""      # its default image-server workflow ("" = theirs)
    # Which direct LLM provider backs this bot ("" = the agent backend). The id
    # only — never the base URL and never the key; see config.Bot.to_dict.
    api_provider: str = ""


class ThreadOut(BaseModel):
    id: str
    bot_id: str
    title: str | None = None
    created_at: str
    updated_at: str
    status: str = "idle"          # 'idle' | 'thinking' | 'error'
    is_archived: bool = False
    is_pinned: bool = False
    last_message: str | None = None
    message_count: int = 0
    unread_since: str | None = None   # oldest unread bot message (None = read)
    # Content-addressed id of the bot's avatar when this thread started, or
    # None for threads that predate the feature (they render the live avatar).
    avatar_snapshot: str | None = None
    avatar_url: str = ""              # resolved snapshot URL, "" when none
    # Per-thread UI choices: {"model": str, "thinking": str, "scene": str}.
    # None means "the bot's own defaults" — which is every thread that predates
    # the feature.
    prefs: dict | None = None
    # Which profile this conversation belongs to (4.0 ownership). SERVER-SIDE ONLY:
    # ``exclude=True`` keeps it out of every model_dump / JSON response and WS frame, so
    # no client ever learns another profile's id from a thread payload. None = an
    # install that has no Owner yet (read as the Owner once one exists).
    owner_profile_id: str | None = Field(default=None, exclude=True)


class MessageOut(BaseModel):
    id: str
    thread_id: str
    role: str                     # 'user' | 'assistant' | 'system'
    content: str
    created_at: str
    media_url: str | None = None
    metadata: dict[str, Any] | None = None


class CreateThreadIn(BaseModel):
    bot_id: str


#: 3.x visibility keys a client may no longer send: who uses a character is each profile's
#: household list (People), and the Admin's "Hide" is PATCH /api/characters/{id} ``hidden``.
LEGACY_BOT_KEYS = ("visible", "safe", "hide_unlocked", "audience", "profiles")


class BotOrderItem(BaseModel):
    id: str
    order: int = 0
    reactions: bool | None = None # None = leave unchanged (reaction images)
    avatar_pool: bool | None = None  # None = leave unchanged (thread avatar pool)
    image_jobs: bool | None = None   # None = leave unchanged ([[pic:]] pictures, 2.3.7)
    # Voice chat (the voice add-on): None = leave unchanged, "" = the add-on's
    # default. Opaque to DisPatch; the add-on's validator checks the value
    # (422), and without the add-on only "" or the stored value is accepted.
    voice: str | None = Field(default=None, max_length=80)
    voice_rp: str | None = Field(default=None, max_length=32)

    @model_validator(mode="before")
    @classmethod
    def _no_legacy_keys(cls, data: Any) -> Any:
        if isinstance(data, dict):
            sent = [k for k in LEGACY_BOT_KEYS if k in data]
            if sent:
                raise ValueError(f"{sent[0]!r} is no longer accepted (4.0: People decides who "
                                 "uses a character; use 'hidden' on the character to hide it)")
        return data


class UpdateBotOrderIn(BaseModel):
    bots: list[BotOrderItem]


# --------------------------------------------------------------------------- #
# Bot management (POST /api/bots, PATCH /api/bots/{id})
# --------------------------------------------------------------------------- #

#: A bot id minted by the API. Hand-written ids in config.yaml may be anything
#: (the shipped test roster has `AI_Sage`); the API only ever creates these.
BOT_ID_PATTERN = r"^[a-z0-9_-]{1,32}$"
#: An agent id on the agent backend (letters, digits, - and _ -- the same rule
#: the Bots tab checks and its hint text states).
AGENT_ID_PATTERN = r"^[A-Za-z0-9_-]{1,64}$"


def _clean_text(v: str | None) -> str | None:
    return None if v is None else v.strip()


class BotCreateIn(BaseModel):
    """Body of POST /api/bots. Strict: an unknown key is a 422."""
    model_config = ConfigDict(extra="forbid")

    id: str | None = Field(default=None, pattern=BOT_ID_PATTERN)
    name: str = Field(min_length=1, max_length=40)
    emoji: str = Field(default="", max_length=8)
    backend: Literal["direct", "agent"] = "direct"
    agent: str | None = Field(default=None, pattern=AGENT_ID_PATTERN)
    model_hint: str = Field(default="", max_length=60)
    quick_actions: bool = True
    tools: bool | None = None        # direct-API bots only; default on

    @field_validator("name", "emoji", "model_hint")
    @classmethod
    def _strip(cls, v: str) -> str:
        return v.strip()

    @model_validator(mode="after")
    def _shape(self) -> BotCreateIn:
        if not self.name:
            raise ValueError("name must not be blank")
        if self.backend == "direct" and self.agent:
            raise ValueError("agent is only for backend 'agent'")
        if self.backend == "agent" and self.tools is not None:
            raise ValueError("tools is only for direct-API bots")
        return self


class BotPatchIn(BaseModel):
    """Body of PATCH /api/bots/{id}: present keys are set, absent ones left
    alone. Strict: an unknown key is a 422."""
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=40)
    emoji: str | None = Field(default=None, max_length=8)
    model_hint: str | None = Field(default=None, max_length=60)
    quick_actions: bool | None = None
    reactions: bool | None = None
    avatar_pool: bool | None = None
    image_jobs: bool | None = None
    tools: bool | None = None
    # The bot's personality (direct-API: advisor.persona; legacy-API:
    # api.system_prompt). "" = back to the shipped default. See app/personas.py.
    persona: str | None = Field(default=None, max_length=8000)

    @field_validator("name", "emoji", "model_hint")
    @classmethod
    def _strip(cls, v: str | None) -> str | None:
        return _clean_text(v)

    @model_validator(mode="after")
    def _something(self) -> BotPatchIn:
        if not self.model_fields_set:
            raise ValueError("nothing to change")
        if "name" in self.model_fields_set and not self.name:
            raise ValueError("name must not be blank")
        # An explicit null is not "leave alone": it would be ambiguous.
        for k in self.model_fields_set:
            if getattr(self, k) is None:
                raise ValueError(f"{k} must not be null")
        return self


_DATE_RE = r"^\d{4}-\d{2}-\d{2}$"


def _check_real_date(v: str | None) -> str | None:
    if v is not None:
        _date.fromisoformat(v)  # raises ValueError on impossible dates
    return v


# One ceiling for one message, named once. The REST alias used to slice to this
# number and return 200 while /api/inject 422'd on the same body — same
# operation, two behaviours, and the silent one lost the tail of an agent's
# report. The WS send path enforces it too.
MESSAGE_MAX_CHARS = 65536


class InjectIn(BaseModel):
    """Inbound message pushed BY OpenClaw (proactive / daily threads).

    Either target an explicit `thread_id`, or supply `bot_id` (+ optional
    `date`) to find-or-create that bot's daily thread.
    """
    bot_id: str | None = None
    thread_id: str | None = None
    role: Literal["assistant", "user", "system"] = "assistant"
    content: str = Field(default="", max_length=MESSAGE_MAX_CHARS)
    # Alias for `content`, accepted-not-documented: `text` is the #1 caller
    # mistake (every other chat transport names the field that), and with
    # content defaulting to "" it used to persist an EMPTY bubble — the agent
    # believed it posted, the family saw a blank message. Explicit content wins.
    text: str | None = Field(default=None, max_length=MESSAGE_MAX_CHARS, exclude=True)
    media_url: str | None = None
    date: str | None = Field(default=None, pattern=_DATE_RE)  # YYYY-MM-DD; defaults to today
    title: str | None = Field(default=None, max_length=200)
    metadata: dict[str, Any] | None = None
    # Web Push: ping remembered devices about this post. `true` titles it
    # "<bot> posted"; a string is the title ("Morning brief posted"). The
    # push never carries the content.
    notify: bool | str | None = Field(default=None)
    # 4.0 idempotency: a caller-chosen key (a run id, a cron slot). A second inject with the
    # same key stores nothing and answers the first one's message with `duplicate: true`.
    delivery_key: str | None = Field(default=None, min_length=1, max_length=160,
                                     pattern=r"^[A-Za-z0-9._:@/+-]+$")

    _validate_date = field_validator("date")(_check_real_date)

    @field_validator("notify")
    @classmethod
    def _short_notify(cls, v: bool | str | None) -> bool | str | None:
        if isinstance(v, str):
            v = " ".join(v.split())[:80]
            return v or None
        return v

    @model_validator(mode="after")
    def _text_alias_and_nonempty(self) -> InjectIn:
        if not self.content and self.text:
            self.content = self.text
        if not self.content.strip() and not self.media_url:
            raise ValueError(
                "content is required — the message text field is `content`")
        return self


class DailyThreadIn(BaseModel):
    bot_id: str
    date: str | None = Field(default=None, pattern=_DATE_RE)  # YYYY-MM-DD; defaults to today
    title: str | None = Field(default=None, max_length=200)

    _validate_date = field_validator("date")(_check_real_date)


# --------------------------------------------------------------------------- #
# Reaction images (ephemeral overlay pack)
# --------------------------------------------------------------------------- #


class FireReactionIn(BaseModel):
    """Fire a reaction overlay onto every connected device.

    `reaction` is an id, alias, or name. Target a thread (so the collapsed trace
    lands in the right conversation and its owner's sockets get it) or
    leave it off for an app-wide pop.
    """
    reaction: str = Field(..., max_length=64)
    thread_id: str | None = Field(default=None, max_length=128)
    bot_id: str | None = Field(default=None, max_length=64)
    actor: str | None = Field(default=None, max_length=60)   # display name
    # Drives the overlay's byline + icon. Who sees it is the WS scoper's decision,
    # but "agent" fires must also resolve actor/bot_id
    # to a reactions-enabled bot (fire_reaction) so bot names can't be spoofed.
    actor_kind: Literal["user", "agent"] | None = None
    caption: str | None = Field(default=None, max_length=120)
    duration_ms: int | None = Field(default=None, ge=0, le=120_000)
    trace: bool = True          # leave the collapsed one-liner in the thread


class ImageJobIn(BaseModel):
    """Ask for a picture in a thread and get an id back, not a picture.

    One call, and the agent's turn is free again: DisPatch writes the
    placeholder, drives the image server, and rewrites that same message when
    the render lands (or fails). `workflow` and the size fields are optional —
    omitted, the image server picks its own default, which is what an agent
    that does not know this rig's workflow names should do.
    """

    bot_id: str = Field(..., max_length=64)
    thread_id: str = Field(..., max_length=128)
    prompt: str = Field(..., max_length=2000)
    workflow: str | None = Field(default=None, max_length=80)
    # "3:2" style. Mutually exclusive with width/height in practice; the image
    # server prefers explicit pixels when both arrive, and so do we.
    ratio: str | None = Field(default=None, max_length=8)
    width: int | None = Field(default=None, ge=64, le=4096)
    height: int | None = Field(default=None, ge=64, le=4096)
    negative: str | None = Field(default=None, max_length=2000)
    # Shown with the finished picture, and in the pending line so the thread
    # says what is coming rather than just that something is.
    caption: str | None = Field(default=None, max_length=200)
    # The rig's queue band: 1 interactive, 2 normal, 3 background. Defaults to
    # 3 per the rig contract — an omitted priority means background, and tier
    # 1 is reserved for work a person is actively waiting on. A caller that
    # knows a person is waiting on this specific picture should say so.
    priority: int = Field(default=3, ge=1, le=3)


class ReactionPatchIn(BaseModel):
    name: str | None = Field(default=None, max_length=60)
    aliases: list[str] | None = None
    category: str | None = Field(default=None, max_length=40)
    safe: bool | None = None
    duration_ms: int | None = Field(default=None, ge=0, le=120_000)


class ReactionSettingsIn(BaseModel):
    """Partial overlay — keys not present are left unchanged.

    ``bot_id`` selects WHOSE pool. Every sibling endpoint takes it at the query
    or top level, so callers wrote `{"bot_id": "<bot>", "values": {...}}` — and
    until 2026-08-26 this model had no such field, pydantic dropped it, and the
    handler read it out of `values` only. The write silently landed on the
    DEFAULT bot's config. Accepted in both places now; the handler 400s on a
    disagreement rather than picking a winner.
    """
    bot_id: str | None = None
    values: dict[str, Any] = Field(default_factory=dict)

    def target_bot(self) -> str:
        """The bot this write is for. Raises ValueError if the two spellings
        disagree — silently choosing one is how the original bug felt fine."""
        top = str(self.bot_id or "").strip()
        inner = str(self.values.get("bot_id") or "").strip()
        if top and inner and top.casefold() != inner.casefold():
            raise ValueError(
                f"bot_id given twice and they disagree ({top!r} vs {inner!r})")
        return top or inner


class GenerateReactionIn(BaseModel):
    prompt: str = Field(..., max_length=1000)
    name: str | None = Field(default=None, max_length=60)
    style: str | None = Field(default=None, max_length=60)
    workflow: str | None = Field(default=None, max_length=60)
    safe: bool = False


# --------------------------------------------------------------------------- #
# Message feedback (thumbs up/down on one of the bot's own replies)
# --------------------------------------------------------------------------- #


class MessageFeedbackIn(BaseModel):
    """Thumbs feedback on one of the bot's own replies.

    Literal enums ONLY, never free text — that is the whole security property.
    Any profile that may chat can vote, and the vote it records is later quoted
    back into the agent's own prompt (see main._pending_feedback_lines): if
    `reason` were a free string, a caller could inject arbitrary prompt text
    through it. Closed vocabularies
    can't do that no matter who is asking.
    """
    vote: Literal["up", "down"]
    reason: Literal[
        "inaccurate", "unhelpful", "too_long", "off_topic", "tone", "other",
    ] | None = None


class AdvisorActionIn(BaseModel):
    """Body of an advisor handoff card's Send / Dismiss.

    `message_id` is the card the button was pressed on. It is checked against
    the ledger's `card_message_id`: a button on any OTHER message — a forged
    card that copied a real request id — is refused.
    """
    message_id: str = Field(min_length=1, max_length=64)
