"""Pydantic models for REST/WebSocket payloads."""

from __future__ import annotations

from datetime import date as _date
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator


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


class BotOrderItem(BaseModel):
    id: str
    order: int = 0
    visible: bool | None = None   # None = leave unchanged
    safe: bool | None = None      # None = leave unchanged (Safe-Mode visibility)
    reactions: bool | None = None # None = leave unchanged (reaction images)
    avatar_pool: bool | None = None  # None = leave unchanged (thread avatar pool)


class UpdateBotOrderIn(BaseModel):
    bots: list[BotOrderItem]


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

    _validate_date = field_validator("date")(_check_real_date)

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
    lands in the right conversation and Safe-Mode scoping can be applied) or
    leave it off for an app-wide pop.
    """
    reaction: str = Field(..., max_length=64)
    thread_id: str | None = Field(default=None, max_length=128)
    bot_id: str | None = Field(default=None, max_length=64)
    actor: str | None = Field(default=None, max_length=60)   # display name
    # Drives the overlay's byline + icon. View-tier decisions still come from
    # the session/decoy state, but "agent" fires must also resolve actor/bot_id
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
    # 1 because a request through this route always has a placeholder sitting
    # in a thread; a caller that knows its picture is not urgent can say so.
    priority: int = Field(default=1, ge=1, le=3)


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
