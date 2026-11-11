"""The catalog of models each connection / image provider offers (refreshable, never a key).

Stored in ``<data>/model-catalog.json``::

    {"version": 1, "connections": {<conn_id>: Entry}, "image": {<preset_id>: Entry}}
    Entry = {"models": [{"id", "kind"}], "source": "endpoint|documented|mixed",
             "checked_at": iso|None, "error": str|None, "note": str}

Chat connections go through ``connections.test_and_record`` (one code path for the SSRF guard
and the key; it also updates ``connections.models``). Image providers resolve exactly as Image
Studio does (``image_providers.resolve(..., studio=True)``) and the request goes only to the
resolved base URL. Nothing here logs or returns a key.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import threading
import time
from datetime import UTC, datetime
from typing import Any

import httpx

from . import config, connections, image_jobs, image_providers

MAX_IDS = 500
MAX_BYTES = 2 * 1024 * 1024
TIMEOUT_S = 15.0
COOLDOWN_S = 20.0
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@-]{0,199}$")
_IMAGE_RE = re.compile(r"image|dall-e|imagine|flux|sdxl|diffusion", re.I)
_EMBED_RE = re.compile(r"embed", re.I)

_lock = threading.Lock()
#: target key ("conn:<id>" / "image:<preset>") -> monotonic time of the last refresh.
_last: dict[str, float] = {}

_MINIMAX_NOTE = ("MiniMax's model list (GET /v1/models) has no image models; this list is from "
                 "MiniMax's docs (checked 2026-11-11)")

DOCUMENTED_IMAGE_MODELS: dict[str, dict[str, Any]] = {
    "minimax": {
        "models": ["MiniMax-H3-Image", "MiniMax-H3-Image-Highspeed", "image-01", "image-01-live"],
        "names": {"MiniMax-H3-Image": "MiniMax H3 Image (newest)",
                  "MiniMax-H3-Image-Highspeed": "MiniMax H3 Image Highspeed (about 2x the price)",
                  "image-01": "image-01 (cheapest)", "image-01-live": "image-01-live"},
        "source_url": "https://platform.minimax.io/", "checked": "2026-11-11",
        "note": _MINIMAX_NOTE},
    "openai": {
        "models": ["gpt-image-1", "dall-e-3"], "names": {},
        "source_url": "https://platform.openai.com/docs/models", "checked": "2026-11-11",
        "note": "From OpenAI's docs (checked 2026-11-11); used when the provider's list can't be read"},
    "xai": {
        "models": ["grok-imagine-image", "grok-imagine-image-2.0"], "names": {},
        "source_url": "https://docs.x.ai/", "checked": "2026-11-11",
        "note": "From xAI's docs (checked 2026-11-11); used when the provider's list can't be read"},
    "civitai": {
        "models": ["urn:air:sdxl:checkpoint:civitai:133005@1759168"], "names": {
            "urn:air:sdxl:checkpoint:civitai:133005@1759168": "Juggernaut XL"},
        "source_url": "https://civitai.com/", "checked": "2026-11-11",
        "note": "Civitai has thousands of checkpoints - use Browse"},
}


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def classify(model_id: str) -> str:
    s = str(model_id or "")
    if _IMAGE_RE.search(s):
        return "image"
    if _EMBED_RE.search(s):
        return "embedding"
    return "chat"


def _entry(models=(), source="endpoint", checked_at=None, error=None, note="") -> dict:
    return {"models": [{"id": i, "kind": k} for i, k in models], "source": source,
            "checked_at": checked_at, "error": error, "note": note}


def _clean_ids(raw) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for i in raw:
        if not isinstance(i, str) or not _ID_RE.match(i) or i in seen:
            continue
        seen.add(i)
        out.append(i)
        if len(out) >= MAX_IDS:
            break
    return out


# --------------------------------------------------------------------------- #
# Storage
# --------------------------------------------------------------------------- #


def _path():
    return config.DATA_DIR / "model-catalog.json"


def _read() -> dict:
    try:
        doc = json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        doc = {}
    if not isinstance(doc, dict):
        doc = {}
    for k in ("connections", "image"):
        if not isinstance(doc.get(k), dict):
            doc[k] = {}
    doc["version"] = 1
    return doc


def _store(section: str, key: str, entry: dict) -> None:
    with _lock:
        doc = _read()
        doc[section][key] = entry
        p = _path()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + f".{os.getpid()}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with contextlib.suppress(OSError):
            os.fchmod(fd, 0o600)            # a stale tmp keeps its old mode otherwise
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(doc, f, indent=1, sort_keys=True)
        os.replace(tmp, p)


def catalog() -> dict:
    """The whole stored catalog (no keys, no URLs)."""
    with _lock:
        return _read()


def _release(key: str) -> None:
    """A failed refresh must not start the cooldown: the next press may retry at once."""
    with _lock:
        _last.pop(key, None)


def _claim(key: str) -> bool:
    """Check-and-set the cooldown under the lock: True = this caller may refresh now."""
    with _lock:
        now = time.monotonic()
        t = _last.get(key)
        if t is not None and now - t < COOLDOWN_S:
            return False
        _last[key] = now
        return True


# --------------------------------------------------------------------------- #
# Chat connections
# --------------------------------------------------------------------------- #


async def refresh_connection(conn: connections.Connection) -> dict:
    """Re-list a saved connection's models. Never raises."""
    key = f"conn:{conn.id}"
    if not _claim(key):
        stored = (catalog()["connections"].get(conn.id)) or _entry()
        return {**stored, "cached": True}
    try:
        # A refresh re-lists models; it never picks a default model for the connection.
        res = await connections.test_and_record(conn, fill_default=False)
    except Exception as e:                                    # never raises
        res = {"ok": False, "models": [], "message": f"{type(e).__name__}"}
    if res.get("ok") and res.get("models"):
        ids = _clean_ids(res["models"])
        entry = _entry([(i, classify(i)) for i in ids], "endpoint", _now())
    else:
        prev = catalog()["connections"].get(conn.id) or _entry()
        msg = str(res.get("message") or res.get("problem") or "the server did not list models")
        entry = {**prev, "error": msg[:300]}
        entry["checked_at"] = prev.get("checked_at")
    if entry.get("error"):
        _release(key)
    await asyncio.to_thread(_store, "connections", conn.id, entry)
    return entry


# --------------------------------------------------------------------------- #
# Image providers
# --------------------------------------------------------------------------- #


def _cfg_for(preset_id: str) -> dict:
    """The same config Image Studio resolves a preset with (key stays inside ``resolve``)."""
    stored = image_providers.load_config()
    preset = image_providers.PRESET_BY_ID[preset_id]
    if image_providers.provider_id(stored) == preset_id and not preset.studio_only:
        return dict(stored)
    return {"provider": preset_id, "api_key_env": preset.key_env}


async def _get_json(url: str, key: str) -> tuple[int, Any]:
    """GET with bounds: 15 s in total, 2 MB, no redirects. -> (status, parsed json or None)."""
    return await asyncio.wait_for(_get_json_inner(url, key), TIMEOUT_S)


async def _get_json_inner(url: str, key: str) -> tuple[int, Any]:
    headers = {"Accept": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    async with httpx.AsyncClient(transport=image_providers.transport, follow_redirects=False,
                                 timeout=httpx.Timeout(TIMEOUT_S)) as client:
        async with client.stream("GET", url, headers=headers) as r:
            buf = bytearray()
            async for chunk in r.aiter_bytes():
                buf.extend(chunk)
                if len(buf) > MAX_BYTES:
                    raise ValueError("the model list is larger than 2 MB")
            status = r.status_code
    try:
        return status, json.loads(bytes(buf)) if buf else None
    except ValueError:
        return status, None


def _ids_from(doc: Any) -> list[str]:
    rows = doc.get("data") if isinstance(doc, dict) else None
    if rows is None and isinstance(doc, dict):
        rows = doc.get("models")
    if rows is None and isinstance(doc, list):
        rows = doc
    out = []
    for r in rows or []:
        if isinstance(r, str):
            out.append(r)
        elif isinstance(r, dict):
            out.append(r.get("id") or r.get("model") or r.get("name"))
    return _clean_ids(out)


def _documented_entry(preset_id: str, error: str | None = None, checked_at=None) -> dict:
    d = DOCUMENTED_IMAGE_MODELS.get(preset_id) or {}
    return _entry([(i, "image") for i in d.get("models", [])], "documented", checked_at, error,
                  d.get("note", ""))


async def refresh_image(preset_id: str) -> dict:
    """Re-list an image provider's models (see the module docstring). Never raises."""
    if preset_id not in image_providers.PRESET_BY_ID:
        return _entry(error="unknown image provider", source="documented")
    key = f"image:{preset_id}"
    if not _claim(key):
        return {**(catalog()["image"].get(preset_id) or _documented_entry(preset_id)), "cached": True}
    entry = await _refresh_image(preset_id)
    if entry.get("error"):
        _release(key)
    await asyncio.to_thread(_store, "image", preset_id, entry)
    return entry


async def _refresh_image(preset_id: str) -> dict:
    if preset_id == "civitai":                      # no list endpoint: documented, Browse does the rest
        return _documented_entry(preset_id, checked_at=_now())
    try:
        res = image_providers.resolve(_cfg_for(preset_id), studio=True)
    except image_jobs.ImageJobError as e:
        return _documented_entry(preset_id, error=str(e)[:300])
    base = res.base_url.rstrip("/")
    try:                                   # same target policy as a chat connection's Test
        await asyncio.to_thread(connections.check_target, base)
    except connections.ConnectionError_ as e:
        return _documented_entry(preset_id, error=str(e)[:300])
    try:
        if preset_id == "minimax":
            status, _doc = await _get_json(f"{base}/models", res.api_key)
            err = None if status == 200 else f"MiniMax answered HTTP {status} to the key check"
            return _documented_entry(preset_id, error=err, checked_at=_now())
        ids: list[str] = []
        if preset_id == "xai":
            status, doc = await _get_json(f"{base}/image-generation-models", res.api_key)
            if status == 200:
                ids = _ids_from(doc)
            if not ids:
                status, doc = await _get_json(f"{base}/models", res.api_key)
                if status != 200:
                    return _documented_entry(preset_id, error=f"HTTP {status}")
                ids = [i for i in _ids_from(doc) if classify(i) == "image"]
        else:
            status, doc = await _get_json(f"{base}/models", res.api_key)
            if status != 200:
                return _documented_entry(preset_id, error=f"HTTP {status}")
            ids = _ids_from(doc)
            if preset_id == "openai":
                ids = [i for i in ids if classify(i) == "image"]
    except (httpx.HTTPError, ValueError, TimeoutError) as e:
        return _documented_entry(preset_id, error=f"{type(e).__name__}: {str(e)[:120]}")
    if not ids and preset_id != "custom":
        return _documented_entry(preset_id, error="the provider listed no image models",
                                 checked_at=_now())
    return _entry([(i, "image" if preset_id != "custom" else classify(i)) for i in ids],
                  "endpoint", _now())


def image_models(preset_id: str, cat: dict | None = None) -> dict:
    """What an image provider offers: the stored entry plus the documented list. Pure read.
    ``cat`` = an already-read catalog (``catalog()``), so a caller listing many presets reads the file once."""
    stored = (cat if cat is not None else catalog())["image"].get(preset_id) or {}
    doc = DOCUMENTED_IMAGE_MODELS.get(preset_id) or {}
    ids = [m["id"] for m in stored.get("models", []) if isinstance(m, dict) and m.get("id")]
    source = stored.get("source") or ("documented" if doc else "endpoint")
    if not ids:                       # a live list is authoritative; the docs fill in only when it is empty
        ids = list(doc.get("models", []))
        source = "documented" if doc else source
    note = stored.get("note") or (doc.get("note", "") if source == "documented" else "")
    return {"models": ids, "names": dict(doc.get("names", {})), "source": source,
            "checked_at": stored.get("checked_at"), "checked": doc.get("checked", ""),
            "note": note}


async def refresh_all(concurrency: int = 4, *, scope: str = "all") -> dict:
    """Refresh every connection and image preset, or (``scope="image"``) only the image presets."""
    sem = asyncio.Semaphore(concurrency)

    async def run(coro):
        async with sem:
            return await coro

    conns = [] if scope == "image" else await asyncio.to_thread(connections.list_all)
    presets = list(image_providers.PRESET_BY_ID)
    cres = await asyncio.gather(*(run(refresh_connection(c)) for c in conns))
    ires = await asyncio.gather(*(run(refresh_image(p)) for p in presets))
    return {"connections": {c.id: e for c, e in zip(conns, cres, strict=True)},
            "image": dict(zip(presets, ires, strict=True))}
