"""The spoken-reply instruction a voice turn prepends for the agent.

Layered on whatever the bot already is, and never stored on the user's row.
It goes through TurnOptions.voice_hint, the same chokepoint
(``_compose_agent_text``) that quotes use. A voice extension can replace it
per voice (registry.set_persona_provider); the driver's reply-length and
follow-up choices (the ``start`` frame) are still appended to it.
"""
from __future__ import annotations

from . import registry
from .config import REPLY_STYLES

_LENGTH = {
    "short": "one short spoken sentence, two at most",
    "normal": "one to three short spoken sentences",
    "longer": "a short spoken paragraph, five or six sentences at most",
}
_FOLLOW_UP = {
    True: "and ask a follow-up question most turns",
    False: "and do not end with a question unless you truly need an answer to go on",
}


def _persona(style: str, follow_up: bool) -> str:
    return (
        "[Voice mode] The user is talking to you hands-free while driving and hears "
        "your reply through text-to-speech; they cannot read the screen. Answer like "
        f"a warm, curious podcast host: {_LENGTH[style]}, natural and conversational, "
        f"{_FOLLOW_UP[follow_up]}. Use their name "
        "sparingly. Never read out lists, URLs, code, tables or markdown; say \"I've "
        "put that in the thread\" instead. If you need a tool or your memory, use it, "
        "but keep the spoken answer short. Do not deliberate at length before answering."
    )


#: The default (normal length, follow-up questions on).
DEFAULT_PERSONA = _persona("normal", True)


def style_line(style: str, follow_up: bool) -> str:
    """The driver's choices as one sentence, for an extension's own persona."""
    return f"[Voice reply style] Answer in {_LENGTH[style]}, {_FOLLOW_UP[follow_up]}."


def voice_hint(bot_id: str | None, profile_id: str | None = None,
               style: str = "normal", follow_up: bool = True) -> str:
    style = style if style in REPLY_STYLES else "normal"
    follow_up = bool(follow_up)
    custom = registry.persona_for(bot_id, profile_id)
    if custom:
        if (style, follow_up) == ("normal", True):
            return custom
        return f"{custom}\n{style_line(style, follow_up)}"
    return _persona(style, follow_up)
