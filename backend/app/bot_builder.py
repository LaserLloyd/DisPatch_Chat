"""AI build: turn a plain-language description into a draft personal bot (4.0).

Pure logic, no HTTP and no provider: :func:`build_draft` is handed an async ``complete``
callable (``(system, user) -> text``) so the route can use the real provider chain and the
tests a fake one. The meta-prompt lives in ``bot_templates/ai-build.md`` (one template, a
repair section after a ``---REPAIR---`` line).

The model's reply is parsed, validated against the editor's limits, and on failure asked
once more (the repair retry). A second failure raises :class:`DraftFailed`. The user's
description and the draft are never logged here (they are private chat content).
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import personas

TEMPLATE_PATH = Path(__file__).resolve().parent / "bot_templates" / "ai-build.md"
_REPAIR_MARK = "\n---REPAIR---\n"

MAX_DESCRIPTION_IN = 1000        # what the person may type
NAME_MAX = 40
EMOJI_MAX = 8
BLURB_MAX = 400
PERSONA_MAX = 4000               # the editor's limit for an own personality text
PERSONA_MIN = 20

SYSTEM_PROMPT = "You write chat character setups as strict JSON."

Complete = Callable[[str, str], Awaitable[str]]


class DraftFailed(Exception):
    """The model did not produce a usable draft, even after the repair retry.

    ``problem`` names what was wrong with the last reply; it never contains the reply."""

    def __init__(self, problem: str) -> None:
        super().__init__(problem)
        self.problem = problem


class DraftInvalid(ValueError):
    """One reply failed validation; the message is fed back to the model for the retry."""


@dataclass(frozen=True)
class Draft:
    name: str
    emoji: str
    description: str
    persona: str

    def to_dict(self) -> dict[str, str]:
        return {"name": self.name, "emoji": self.emoji,
                "description": self.description, "persona": self.persona}


def _template() -> tuple[str, str]:
    text = TEMPLATE_PATH.read_text(encoding="utf-8")
    first, _, repair = text.partition(_REPAIR_MARK)
    return first.strip(), repair.strip()


def first_prompt(description: str) -> str:
    return _template()[0].replace("{{DESCRIPTION}}", description)


def repair_prompt(description: str, problem: str) -> str:
    return _template()[1].replace("{{PROBLEM}}", problem).replace("{{DESCRIPTION}}", description)


# --------------------------------------------------------------------------- #
# Parsing and validation
# --------------------------------------------------------------------------- #

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.S | re.I)
_THINK_RE = re.compile(r"<think>.*?</think>", re.S | re.I)


def _json_object(raw: str) -> Any:
    text = _THINK_RE.sub("", raw or "").strip()
    fenced = _FENCE_RE.search(text)
    if fenced:
        text = fenced.group(1).strip()
    try:
        return json.loads(text)
    except ValueError:
        pass
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise DraftInvalid("it was not a JSON object")
    try:
        return json.loads(text[start:end + 1])
    except ValueError as e:
        raise DraftInvalid("it was not valid JSON") from e


def _text(obj: dict, key: str, *, required: bool, limit: int) -> str:
    value = obj.get(key)
    if value is None:
        if required:
            raise DraftInvalid(f'the "{key}" key is missing')
        return ""
    if not isinstance(value, str):
        raise DraftInvalid(f'"{key}" must be a string')
    value = value.replace("\r\n", "\n").strip()
    if required and not value:
        raise DraftInvalid(f'"{key}" is empty')
    if len(value) > limit:
        raise DraftInvalid(f'"{key}" is {len(value)} characters; the limit is {limit}')
    return value


def validate(raw: str) -> Draft:
    """The reply as a :class:`Draft`, or :class:`DraftInvalid` saying what is wrong."""
    obj = _json_object(raw)
    if not isinstance(obj, dict):
        raise DraftInvalid("the top level must be a JSON object")
    name = _text(obj, "name", required=True, limit=NAME_MAX)
    emoji = _text(obj, "emoji", required=False, limit=EMOJI_MAX)
    blurb = _text(obj, "description", required=False, limit=BLURB_MAX)
    persona = _text(obj, "persona", required=True, limit=PERSONA_MAX)
    if len(persona) < PERSONA_MIN:
        raise DraftInvalid('"persona" is too short to describe a character')
    try:
        persona = personas.check_text(persona)
        name.encode("utf-8")
        emoji.encode("utf-8")
        blurb.encode("utf-8")
    except (ValueError, UnicodeEncodeError) as e:
        raise DraftInvalid("it contained text that cannot be stored") from e
    return Draft(name=name, emoji=emoji, description=blurb, persona=persona)


async def build_draft(description: str, complete: Complete) -> Draft:
    """Ask the model for a draft; one repair retry on an unusable reply.

    Provider errors (``complete`` raising) propagate unchanged: they are not draft
    problems. Raises :class:`DraftFailed` when both replies were unusable."""
    description = (description or "").strip()
    reply = await complete(SYSTEM_PROMPT, first_prompt(description))
    try:
        return validate(reply)
    except DraftInvalid as first:
        problem = str(first)
    reply = await complete(SYSTEM_PROMPT, repair_prompt(description, problem))
    try:
        return validate(reply)
    except DraftInvalid as second:
        raise DraftFailed(str(second)) from None
