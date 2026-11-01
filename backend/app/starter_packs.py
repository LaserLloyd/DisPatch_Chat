"""Starter packs: ready-made sets of characters you can install in one step.

A pack is a YAML file in ``app/starter_packs/`` (``schema: 1``, ``id``, ``name``, ``description``,
optional ``defaults``, and ``characters``, each of which is a *Character manifest*
- ``docs/contracts/manifests/character.schema.json``). One ships with DisPatch:

``assistants``      four general helpers (explain, write, code, plan)
``animal_friends``  eight gentle animal characters for small children (science, one fact at a time)

A pack may ship pictures: ``app/starter_packs/pictures/<character id>-{face,full,thumb}.png``.
Install copies them into the avatar folder under the same names the avatar upload route writes
(``POST /api/bots/<id>/avatar``) and points the character at its face.

``defaults`` may set ``tools`` / ``quick_actions`` (booleans, default on), ``advisor`` (only the
keys in :data:`_ADVISOR_KEYS`: how the persona names the person, how much chat history a turn
sends) and ``max_tokens`` (the reply cap on the connection entry, set when the pack is installed
with a connection).

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
#: A pack character's shipped pictures: ``<id>-face.png`` (the avatar), ``-full`` and ``-thumb``.
AVATAR_SRC_DIR = PACK_DIR / "pictures"
_AVATAR_PARTS = ("face", "full", "thumb")
#: The ``defaults.advisor`` keys a pack may set on its characters (nothing that names a provider,
#: a key, a knowledge folder or an agent: a pack never reaches beyond its own characters).
_ADVISOR_KEYS = {"owner": str, "history_chars": int}

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
                            "defaults": _defaults(doc.get("defaults")), "characters": chars}
        except (OSError, yaml.YAMLError, KeyError, ValueError, ValidationError) as e:
            log.error("starter pack %s is unusable: %s", f.name, e)
    return out


def _defaults(raw: Any) -> dict[str, Any]:
    """A pack's ``defaults``, checked: unknown advisor keys and wrong types are refused (the pack
    is then unusable and logged, like any other bad pack file)."""
    d = dict(raw or {})
    for k in ("tools", "quick_actions"):
        if k in d and not isinstance(d[k], bool):
            raise ValueError(f"defaults.{k} must be true or false")
    adv = dict(d.get("advisor") or {})
    for k, v in adv.items():
        want = _ADVISOR_KEYS.get(k)
        if want is None:
            raise ValueError(f"defaults.advisor.{k} is not a key a pack may set")
        if not isinstance(v, want) or isinstance(v, bool):
            raise ValueError(f"defaults.advisor.{k} must be a {want.__name__}")
    if "history_chars" in adv and not 1000 <= adv["history_chars"] <= 200_000:
        raise ValueError("defaults.advisor.history_chars must be between 1000 and 200000")
    d["advisor"] = adv
    if "max_tokens" in d and (not isinstance(d["max_tokens"], int) or isinstance(d["max_tokens"], bool)
                              or not 1 <= d["max_tokens"] <= 100_000):
        raise ValueError("defaults.max_tokens must be a whole number between 1 and 100000")
    return d


def avatar_files(char_id: str) -> dict[str, Path]:
    """The pictures a pack ships for ``char_id`` (``{"face"|"full"|"thumb": path}``; empty when it
    ships none - the face is required for any of them to count)."""
    found = {part: AVATAR_SRC_DIR / f"{char_id}-{part}.png" for part in _AVATAR_PARTS}
    found = {k: v for k, v in found.items() if v.is_file()}
    return found if "face" in found else {}


def _install_avatar(char_id: str) -> str:
    """Copy the shipped pictures of ``char_id`` into the avatar folder (atomically, under the
    upload route's names) and point the character at its face. Returns the face file name, or
    "" when the pack ships none or the copy failed (the character keeps its emoji)."""
    src = avatar_files(char_id)
    if not src:
        return ""
    dest_dir = Path(config.AVATAR_DIR)
    written: list[Path] = []
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
        for part, path in src.items():
            dest = dest_dir / f"{char_id}-{part}.png"
            tmp = dest.with_name(f".{dest.name}.partial")
            try:
                tmp.write_bytes(path.read_bytes())
                tmp.replace(dest)
            finally:
                tmp.unlink(missing_ok=True)
            written.append(dest)
    except OSError:
        log.warning("starter pack: could not install the picture of %s", char_id, exc_info=True)
        for p in written:
            p.unlink(missing_ok=True)
        return ""
    face = f"{char_id}-face.png"
    return face if config.save_bot_avatar(char_id, face) else ""


def list_packs() -> list[dict]:
    """Every pack with its characters (persona text omitted - it is long) and what is installed."""
    have = {b.id for b in config.load_bots()}
    out = []
    for p in _load_all().values():
        chars = [{"id": c.id, "name": c.name, "emoji": c.emoji, "description": c.description,
                  "voice": c.voice, "installed": c.id in have, "color": c.color,
                  "picture": f"/api/starter-packs/{p['id']}/pictures/{c.id}"
                             if avatar_files(c.id) else ""}
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
    """Create the pack's characters. Returns ``{"pack", "installed": [ids], "skipped": [{id, reason}],
    "voices_skipped": [ids], "pictures": [ids]}``.

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
    pictures: list[str] = []
    advisor_extra: dict[str, Any] = dict(defaults.get("advisor") or {})
    max_tokens = defaults.get("max_tokens")
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
                description=c.description, pack=pack_id, give_to=[],
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
        if advisor_extra or (max_tokens and connection):
            config.update_bot(c.id, lambda b: _with_pack_settings(b, advisor_extra, max_tokens))
        if _install_avatar(c.id):
            pictures.append(c.id)
        done.append(c.id)
        have.add(c.id.lower())
    return {"pack": pack_id, "installed": done, "skipped": skipped, "voices_skipped": voices_skipped,
            "pictures": pictures}


def _with_pack_settings(b: config.Bot, advisor_extra: dict[str, Any], max_tokens: int | None) -> config.Bot:
    adv = {**(b.advisor or {}), **advisor_extra}
    provs = [dict(p) if isinstance(p, dict) else p for p in (adv.get("providers") or [])]
    if max_tokens and provs and isinstance(provs[0], dict):
        provs[0]["max_tokens"] = int(max_tokens)
        adv["providers"] = provs
    return replace(b, advisor=adv)
