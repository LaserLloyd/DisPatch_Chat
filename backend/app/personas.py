"""Personality presets for the bots that take a system prompt from DisPatch.

A direct-API bot (the advisor backend) reads its personality from
``advisor.persona``; a legacy ``api:`` bot from ``api.system_prompt``. This
module is the one place both are read and written, so the Bots tab's
Personality editor and the AI models Connect field can never disagree.

The persona is only the FIRST block of a direct bot's system prompt: the date,
RULES, the tool sentences, the voice section and the profile are appended by
:func:`advisor.build_system_prompt` and are not editable here.

Presets are data: ``personas/<id>.md`` files with a small front-matter block
(``title``, ``description``, ``order``) and the persona text as the body. The
shipped default (:data:`advisor.DEFAULT_PERSONA`) is listed first as
``default``; it lives in code because it is also the fallback when a bot has
no persona at all.
"""
from __future__ import annotations

import functools
import re
from dataclasses import dataclass
from pathlib import Path

from . import config

PRESET_DIR = Path(__file__).resolve().parent / "personas"
#: The same ceiling the Connect route applies (llm_api.MAX_PERSONA_CHARS).
MAX_CHARS = 8000
#: A preset is a character sketch, not a document.
PRESET_MAX_CHARS = 1200
DEFAULT_ID = "default"
_ID_RE = re.compile(r"^[a-z0-9-]{1,32}$")
_FRONT_RE = re.compile(r"\A---\n(.*?)\n---\n(.*)\Z", re.S)


@dataclass(frozen=True)
class Preset:
    id: str
    title: str
    description: str
    text: str
    order: int

    def to_dict(self) -> dict:
        return {"id": self.id, "title": self.title,
                "description": self.description, "text": self.text}


def _parse(path: Path) -> Preset | None:
    m = _FRONT_RE.match(path.read_text(encoding="utf-8").replace("\r\n", "\n"))
    if not m or not _ID_RE.match(path.stem):
        return None
    meta: dict[str, str] = {}
    for line in m.group(1).splitlines():
        k, sep, v = line.partition(":")
        if sep:
            meta[k.strip()] = v.strip()
    text = m.group(2).strip()
    if not text or not meta.get("title"):
        return None
    try:
        order = int(meta.get("order") or 1000)
    except ValueError:
        order = 1000
    return Preset(path.stem, meta["title"], meta.get("description", ""), text, order)


@functools.cache
def presets() -> tuple[Preset, ...]:
    """Every preset, the shipped default first, then by ``order``."""
    from .advisor import DEFAULT_PERSONA
    found = [p for p in (_parse(f) for f in sorted(PRESET_DIR.glob("*.md"))) if p]
    found.sort(key=lambda p: (p.order, p.id))
    default = Preset(DEFAULT_ID, "DisPatch default",
                     "The shipped advisor: warm, quick-witted, leads with the answer.",
                     DEFAULT_PERSONA, 0)
    return (default, *[p for p in found if p.id != DEFAULT_ID])


def public() -> dict:
    return {"presets": [p.to_dict() for p in presets()],
            "default_id": DEFAULT_ID, "max_chars": MAX_CHARS}


# --------------------------------------------------------------------------- #
# Which bots have an editable personality, and what it is
# --------------------------------------------------------------------------- #


def kind(bot: config.Bot) -> str:
    """``direct`` (advisor.persona), ``legacy-api`` (api.system_prompt) or
    ``none`` (an agent / app bot: its personality lives in its runtime)."""
    if config.is_direct(bot):
        return "direct"
    if bot.api:
        return "legacy-api"
    return "none"


def current(bot: config.Bot) -> tuple[str, bool]:
    """(the text the bot runs with, whether it is a stored custom value)."""
    k = kind(bot)
    if k == "direct":
        from .advisor import DEFAULT_PERSONA
        stored = str((bot.advisor or {}).get("persona") or "").strip()
        return (stored or DEFAULT_PERSONA, bool(stored))
    if k == "legacy-api":
        stored = str((bot.api or {}).get("system_prompt") or "").strip()
        return (stored, bool(stored))
    return ("", False)


def check_text(text: str) -> str:
    """The text as it can be stored. Raises ValueError for one that cannot be
    written as UTF-8 (a lone surrogate from a JSON ``\\ud800`` escape): the
    roster write would fail halfway with a 500 and no message."""
    t = text or ""
    try:
        t.encode("utf-8")
    except UnicodeEncodeError as e:
        raise ValueError("A personality must be valid text (no lone surrogates)") from e
    return t


def normalise(text: str, kind: str = "direct") -> str:
    """Line endings and trailing space tidied. For a direct-API bot the shipped
    default is stored as "no persona" so a later change to it reaches the bot;
    a legacy ``api:`` bot has no default persona of its own (an empty
    ``system_prompt`` means the provider's plain default), so its text is kept
    as typed."""
    from .advisor import DEFAULT_PERSONA
    t = check_text(text).replace("\r\n", "\n").strip()
    if kind == "direct" and t.replace("{owner}", "{user}") == DEFAULT_PERSONA.strip():
        return ""
    return t


def fill(text: str, name: str, owner: str = "the user") -> str:
    """The two documented placeholders filled in. One rule for both bot kinds:
    a preset says ``{name}`` and ``{user}`` whichever backend runs it.
    ``{owner}`` is the older spelling of ``{user}`` and is still filled in, so
    a persona saved before the rename keeps working. (The ``owner`` argument
    and the ``owner:`` config key keep their names: they are internal.)"""
    return (text or "").replace("{name}", name).replace("{user}", owner).replace("{owner}", owner)


def apply(bot: config.Bot, text: str) -> config.Bot:
    """The bot with its personality set to ``text`` ("" = back to the default).

    Raises ValueError for a bot with no editable personality or a text over
    :data:`MAX_CHARS`. Returns a new Bot; the caller persists it with
    ``config.upsert_bot`` (atomic, keeps every other key)."""
    from dataclasses import replace
    k = kind(bot)
    if k == "none":
        raise ValueError("This bot's personality lives in its agent workspace, not in DisPatch")
    t = normalise(text, k)
    if len(t) > MAX_CHARS:
        raise ValueError(f"A personality is at most {MAX_CHARS} characters")
    if k == "direct":
        adv = dict(bot.advisor or {})
        if t:
            adv["persona"] = t
        else:
            adv.pop("persona", None)
        return replace(bot, advisor=adv)
    api = dict(bot.api or {})
    if t:
        api["system_prompt"] = t
    else:
        api.pop("system_prompt", None)
    return replace(bot, api=api)


def row_fields(bot: config.Bot) -> dict:
    """What the Bots tab row carries (full-access routes only)."""
    k = kind(bot)
    text, custom = current(bot)
    return {"persona_kind": k, "persona": text, "persona_custom": custom}
