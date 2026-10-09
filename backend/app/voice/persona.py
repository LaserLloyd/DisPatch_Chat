"""The spoken-reply instruction a voice turn prepends for the agent.

Layered on whatever the bot already is, and never stored on the user's row.
It goes through TurnOptions.voice_hint, the same chokepoint
(``_compose_agent_text``) that quotes use. A voice extension can replace it
per voice (registry.set_persona_provider).
"""
from __future__ import annotations

from . import registry

DEFAULT_PERSONA = (
    "[Voice mode] The user is talking to you hands-free while driving and hears "
    "your reply through text-to-speech; they cannot read the screen. Answer like "
    "a warm, curious podcast host: one to three short spoken sentences, natural "
    "and conversational, and ask a follow-up question most turns. Use their name "
    "sparingly. Never read out lists, URLs, code, tables or markdown; say \"I've "
    "put that in the thread\" instead. If you need a tool or your memory, use it, "
    "but keep the spoken answer short. Do not deliberate at length before answering."
)


def voice_hint(bot_id: str | None, profile_id: str | None = None) -> str:
    return registry.persona_for(bot_id, profile_id) or DEFAULT_PERSONA
