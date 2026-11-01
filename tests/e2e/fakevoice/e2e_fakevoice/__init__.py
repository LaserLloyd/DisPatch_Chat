"""A fake voice add-on for the e2e harness (tests/e2e/_scratch.py).

It stands in for DisPatch Voice in a scratch instance, with none of its code:
a tiny /api/voice router, a bot-field validator for ``voice``, and a frontend
whose mount() registers the shell's ``bots.rowExtra`` slot (a Voice picker in
Settings -> Characters -> Details) and a chat-header Voice button. That is the
surface the headless matrix drives ("the voice picker saves `voice`", "the
voice button is visible"). It never makes a sound and never reaches a rig.

Loaded with ``DISPATCH_EXTENSIONS=e2e_fakevoice:EXTENSION`` and this package's
parent directory on PYTHONPATH (``_scratch.Scratch(fake_voice=True)`` does both).
"""
from __future__ import annotations

from pathlib import Path

#: The voices the fake picker offers. "" = the bot's default.
VOICES = ("", "builtin:e2e_alto", "builtin:e2e_bass")

EXTENSION = {
    "id": "voice", "name": "E2E Fake Voice", "version": "0.0.1-e2e", "api": [1, 1],
    "setup": "e2e_fakevoice:setup",
}


def _validator(value: str) -> None:
    if value not in VOICES:
        raise ValueError(f"Unknown voice: {value}")


def _rp_validator(value: str) -> None:
    if value not in ("", "all"):
        raise ValueError(f"Unknown role-play mode: {value}")


def setup(ctx) -> None:
    from fastapi import APIRouter

    router = APIRouter()

    @router.get("/status")
    def status():
        return {"ok": True, "enabled": True, "ready": True, "fake": True,
                "voices": [{"id": v, "name": v.split(":")[-1] or "Default"} for v in VOICES]}

    ctx.include_router(router, prefix="/api/voice")
    ctx.serve_static(Path(__file__).parent / "static", entry="index.js", locales=True)
    ctx.set_bot_field_validator("voice", _validator)
    ctx.set_bot_field_validator("voice_rp", _rp_validator)
