"""Starter packs: ready-made sets of characters you can install in one step.

A pack is a YAML file in ``app/starter_packs/`` (``schema: 1``, ``id``, ``name``, ``description``,
optional ``defaults``, and ``characters``, each of which is a *Character manifest*
- ``docs/contracts/manifests/character.schema.json``). One ships with DisPatch:

``assistants``      four general helpers (explain, write, code, plan)

(The 3.x ``audience`` / ``safe`` / ``profiles`` keys are ignored with a log line when a pack file
still carries them: 4.0 decides who uses a character with each profile's household list,
``characters.may_use``.)

Installing a pack creates its characters as ordinary roster entries (so they can be edited,
reordered and removed like any other) and is idempotent: a character whose id already exists is
reported as skipped, never overwritten. ``connection`` / ``model`` point the new characters at one of
your Connections; without one they are created unconnected and answer with the usual "not connected
yet" notice until you assign one in Settings > Characters.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from . import characters, config
from .contracts.manifests import ID_PATTERN, CharacterManifest

log = logging.getLogger("local-chat.packs")

PACK_DIR = Path(__file__).resolve().parent / "starter_packs"

#: 3.x character keys a pack file may still carry; dropped (with a warning), never stored.
_LEGACY_KEYS = frozenset({"audience", "safe", "hide_unlocked", "profiles", "guardrails"})


class PackError(Exception):
    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


@lru_cache(maxsize=1)
def _load_all() -> dict[str, dict]:
    out: dict[str, dict] = {}
    for f in sorted(PACK_DIR.glob("*.yaml")):
        try:
            doc = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
            pack_id = str(doc["id"])
            if f.stem != pack_id:
                raise ValueError(f"id {pack_id!r} must equal the file name {f.stem!r}")
            import re
            if not re.fullmatch(ID_PATTERN, pack_id):
                raise ValueError(f"bad pack id {pack_id!r}")
            chars = []
            for raw in doc.get("characters") or []:
                legacy = sorted(k for k in _LEGACY_KEYS if k in raw)
                if legacy:
                    log.warning("starter pack %s: ignoring the 3.x key(s) %s on %r",
                                f.name, ", ".join(legacy), raw.get("id"))
                m = CharacterManifest.model_validate(
                    {**{k: v for k, v in raw.items() if k not in _LEGACY_KEYS}, "pack": pack_id})
                chars.append(m)
            if not chars:
                raise ValueError("a pack needs at least one character")
            out[pack_id] = {"id": pack_id, "name": str(doc["name"]), "description": str(doc.get("description") or ""),
                            "defaults": dict(doc.get("defaults") or {}), "characters": chars}
        except (OSError, yaml.YAMLError, KeyError, ValueError, ValidationError) as e:
            log.error("starter pack %s is unusable: %s", f.name, e)
    return out


def list_packs() -> list[dict]:
    """Every pack with its characters (persona text omitted - it is long) and what is installed."""
    have = {b.id for b in config.load_bots()}
    out = []
    for p in _load_all().values():
        chars = [{"id": c.id, "name": c.name, "emoji": c.emoji, "description": c.description,
                  "voice": c.voice, "installed": c.id in have}
                 for c in p["characters"]]
        out.append({"id": p["id"], "name": p["name"], "description": p["description"],
                    "characters": chars,
                    "installed": all(c["installed"] for c in chars),
                    "partly_installed": any(c["installed"] for c in chars)})
    return out


def get_pack(pack_id: str) -> dict:
    p = _load_all().get(pack_id)
    if p is None:
        raise PackError("starter_pack_not_found", "Unknown starter pack", 404)
    return p


def install(pack_id: str, *, connection: str | None = None, model: str | None = None,
            only: list[str] | None = None) -> dict:
    """Create the pack's characters. Returns ``{"pack", "installed": [ids], "skipped": [{id, reason}]}``.

    ``only`` limits it to some of the pack's character ids; ``connection`` / ``model`` assign them
    to a Connection. Who may use them is each profile's ``household_bots`` (People)."""
    pack = get_pack(pack_id)
    ids = {c.id for c in pack["characters"]}
    if only:
        unknown = sorted(set(only) - ids)
        if unknown:
            raise PackError("starter_pack_not_found", f"The pack has no character {unknown[0]!r}", 404)
    defaults: dict[str, Any] = pack["defaults"]
    done: list[str] = []
    skipped: list[dict] = []
    voices_skipped: list[str] = []
    have = {b.id.lower() for b in config.load_bots()}
    for c in pack["characters"]:
        if only and c.id not in only:
            continue
        if c.id.lower() in have:
            skipped.append({"id": c.id, "reason": "a character with this id already exists"})
            continue
        try:
            characters.create(
                id=c.id, name=c.name, emoji=c.emoji, backend="direct",
                quick_actions=bool(defaults.get("quick_actions", True)),
                tools=bool(defaults.get("tools", True)), connection=connection, model=model,
                description=c.description, pack=pack_id,
                persona=c.persona, color=c.color, model_hint="")
        except characters.CharacterError as e:
            raise PackError(e.code, str(e), e.status) from e
        if c.voice:
            # The voice is the add-on's to validate. Without the add-on the id is kept as the
            # character's wish (it is used when the add-on arrives); an add-on that does not
            # know the voice leaves the character on its default voice instead of failing.
            try:
                characters.check_extension_field("voice", c.id, c.voice)
                accepted = True
            except characters.CharacterError as e:
                accepted = e.code == "voice_addon_missing"
                if not accepted:
                    voices_skipped.append(c.id)
            if accepted:
                config.update_bot(c.id, lambda b, v=c.voice: replace(b, voice=v[:80]))
        done.append(c.id)
        have.add(c.id.lower())
    return {"pack": pack_id, "installed": done, "skipped": skipped, "voices_skipped": voices_skipped}
