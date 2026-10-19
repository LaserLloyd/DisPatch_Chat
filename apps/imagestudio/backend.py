"""Image Studio's backend (``apps/imagestudio/backend.py``).

Mounted by the shell's app loader at ``/api/apps/imagestudio``. Every route is
operator-only (``ctx.require_operator``) on top of the loader's own gate, so a
locked (Safe-Mode) device, a cookieless caller and a forged cookie all get the
uniform decoy 403 before any of this runs.

WHAT IT IS
----------
A thin, deliberate front for the image providers DisPatch already has
(``app/image_providers.py``): it does NOT contain a second MiniMax/OpenAI
client and never reads a key itself -- ``image_providers.resolve()`` turns the
stored image config into a call, and the key never leaves the server. This
module adds only what a studio needs on top of one provider request:
several pictures per request, a seed, the prompt optimizer, and MiniMax's
``subject_reference`` character mode.

STORAGE
-------
Pictures are written into the same media store every generated chat picture
uses (``config.MEDIA_DIR``, ``/media/<uuid>.<ext>``), so "send to chat" is the
ordinary inject path and No-Image Mode / Safe Mode strip them by the rules they
already have. Each picture gets one row in ``<data>/apps-data/imagestudio/
images.jsonl`` (prompt, provider, model, ratio, seed, ...). The per-day count is
read from that ledger. ``prompts.json`` holds the recent prompts; when it does
not exist yet, an optional ``seed-prompts.json`` (never shipped in the repo)
supplies the first list.

CLEARING
--------
``POST /clear`` is the studio's "Clear from here down": the picture named and
every picture after it in the order the page shows (``order`` is required:
``oldest`` first -- the page's default -- or ``newest`` first) leave the ledger,
and their files leave the media store -- except a file a chat message still
shows (sent to chat), which stays so the chat keeps its picture. ``dry_run``
answers the count for the confirmation, and the real request carries that count
back as ``expect``: if the set changed in between (a picture made in another
tab), nothing is deleted and the answer is 409 ``changed`` so the page counts
again and asks again.
It needs ``manage_history`` on top of the app's ``use_images`` (the permission
that lets a profile delete its conversations); Restricted never holds either.

COST
----
One accepted request is one provider call. Nothing here retries, warms up or
runs on a schedule; a second Generate while one is running is refused (409).
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import io
import json
import logging
import os
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app import image_jobs, image_providers
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

log = logging.getLogger("local-chat.app.imagestudio")

APP_ID = "imagestudio"
CTX = None
router = APIRouter()

PROMPT_MAX = image_providers.MINIMAX_PROMPT_MAX
RECENT_MAX = 50
REFERENCE_MAX_BYTES = 10 * 1024 * 1024
MODEL_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._:/-]{0,79}\Z")
MEDIA_URL_RE = re.compile(r"\A/media/([A-Za-z0-9][A-Za-z0-9._-]{0,127})\Z")
_MEDIA_IN_TEXT = re.compile(r"/media/[A-Za-z0-9][A-Za-z0-9._-]{0,127}\.(?:png|jpe?g|webp|gif)", re.IGNORECASE)
IMAGE_ID_RE = re.compile(r"\A[0-9a-f]{32}\Z")
KNOWN_MODELS = {"minimax": ["image-01", "image-01-live"]}

_lock = asyncio.Lock()


# --------------------------------------------------------------------------- #
# Files
# --------------------------------------------------------------------------- #


def _dir() -> Path:
    return CTX.data_dir


def _ledger() -> Path:
    return _dir() / "images.jsonl"


def _read_ledger() -> list[dict]:
    rows: list[dict] = []
    try:
        with _ledger().open(encoding="utf-8") as f:
            for line in f:
                with contextlib.suppress(ValueError):
                    row = json.loads(line)
                    if isinstance(row, dict) and row.get("id"):
                        rows.append(row)
    except OSError:
        pass
    return rows


def _load_json_list(path: Path) -> list[str] | None:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(doc, list):
        return None
    return [s.strip() for s in doc if isinstance(s, str) and s.strip()][:RECENT_MAX]


def _recent_prompts() -> list[str]:
    own = _load_json_list(_dir() / "prompts.json")
    if own is not None:
        return own
    return _load_json_list(_dir() / "seed-prompts.json") or []


def _remember_prompt(prompt: str) -> None:
    items = [p for p in _recent_prompts() if p != prompt]
    items.insert(0, prompt)
    tmp = _dir() / "prompts.json.tmp"
    tmp.write_text(json.dumps(items[:RECENT_MAX], ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, _dir() / "prompts.json")


def _today() -> str:
    return datetime.now().astimezone().date().isoformat()


def _public_row(row: dict) -> dict:
    return {k: row.get(k) for k in (
        "id", "url", "prompt", "provider", "model", "ratio", "seed", "optimizer",
        "reference", "batch", "created_at", "day")}


# --------------------------------------------------------------------------- #
# Providers
# --------------------------------------------------------------------------- #


def _cfg_for(preset_id: str, model: str = "") -> dict:
    """The config used for ``preset_id``: the stored one when that is the stored
    provider, else the preset's own environment key (and a chat connection to
    the same host, via ``resolve``'s shared-key rule). The key stays inside
    ``resolve``."""
    stored = image_providers.load_config()
    if image_providers.provider_id(stored) == preset_id:
        cfg = dict(stored)
    else:
        preset = image_providers.PRESET_BY_ID[preset_id]
        cfg = {"provider": preset_id, "api_key_env": preset.key_env}
    if model:
        cfg["model"] = model
    return cfg


def _resolved(preset_id: str, model: str = "") -> image_providers.Resolved | None:
    try:
        return image_providers.resolve(_cfg_for(preset_id, model))
    except image_jobs.ImageJobError:
        return None


def _provider_rows() -> tuple[list[dict], str]:
    rows: list[dict] = []
    for preset in image_providers.PRESETS:
        res = _resolved(preset.id)
        row: dict[str, Any] = {
            "id": preset.id, "label": preset.label, "shape": preset.shape,
            "configured": res is not None, "price_note": preset.price_note,
            "model": res.model if res else preset.model,
            "models": KNOWN_MODELS.get(preset.id, []),
        }
        row["supports"] = (image_providers.supports(res) if res else
                           {"reference": preset.shape == "minimax",
                            "optimizer": preset.shape == "minimax",
                            "seed": preset.shape == "minimax",
                            "max_n": image_providers.MAX_BATCH.get(preset.shape, 1)})
        rows.append(row)
    stored = image_providers.provider_id(image_providers.load_config())
    default = stored if any(r["id"] == stored and r["configured"] for r in rows) else ""
    if not default:
        default = next((r["id"] for r in rows if r["configured"]), "")
    return rows, default


# --------------------------------------------------------------------------- #
# Reference images
# --------------------------------------------------------------------------- #


def _to_reference(raw: bytes) -> str:
    """JPEG/PNG bytes -> the data URL MiniMax wants, converting anything else
    Pillow can read, and refusing what is over 10 MB."""
    from PIL import Image, UnidentifiedImageError

    try:
        im = Image.open(io.BytesIO(raw))
        fmt = (im.format or "").upper()
        im.load()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError):
        raise HTTPException(422, "The reference is not a readable image") from None
    if fmt in ("JPEG", "PNG") and len(raw) < REFERENCE_MAX_BYTES:
        mime, out = ("image/jpeg" if fmt == "JPEG" else "image/png"), raw
    else:
        buf = io.BytesIO()
        flat = im.convert("RGBA" if im.mode in ("RGBA", "LA", "P") else "RGB")
        flat.save(buf, "PNG")
        mime, out = "image/png", buf.getvalue()
        if len(out) >= REFERENCE_MAX_BYTES:
            buf = io.BytesIO()
            im.convert("RGB").save(buf, "JPEG", quality=90)
            mime, out = "image/jpeg", buf.getvalue()
    if len(out) >= REFERENCE_MAX_BYTES:
        raise HTTPException(422, "The reference image must be under 10 MB")
    return f"data:{mime};base64,{base64.b64encode(out).decode()}"


def _media_file(name: str) -> Path | None:
    from app import config

    path = config.MEDIA_DIR / name
    return path if path.is_file() else None


def _reference_data_url(ref: dict) -> str:
    kind = str(ref.get("kind") or "")
    if kind == "data_url":
        s = str(ref.get("data") or "")
        if not s.startswith("data:image/") or "," not in s:
            raise HTTPException(422, "The reference is not an image")
        try:
            raw = base64.b64decode(s.split(",", 1)[1], validate=False)
        except ValueError:
            raise HTTPException(422, "The reference could not be read") from None
        return _to_reference(raw)
    if kind == "media":
        m = MEDIA_URL_RE.match(str(ref.get("url") or ""))
        path = _media_file(m.group(1)) if m else None
        if path is None:
            raise HTTPException(404, "That chat image is not in the media store")
        return _to_reference(path.read_bytes())
    if kind == "result":
        iid = str(ref.get("id") or "")
        row = next((r for r in _read_ledger() if r["id"] == iid), None) if IMAGE_ID_RE.match(iid) else None
        m = MEDIA_URL_RE.match(str((row or {}).get("url") or ""))
        path = _media_file(m.group(1)) if m else None
        if path is None:
            raise HTTPException(404, "That result is gone")
        return _to_reference(path.read_bytes())
    raise HTTPException(422, "Unknown reference kind")


# --------------------------------------------------------------------------- #
# Request bodies
# --------------------------------------------------------------------------- #


class GenerateIn(BaseModel):
    prompt: str = Field(min_length=1, max_length=PROMPT_MAX + 400)
    provider: str = ""
    model: str = ""
    ratio: str = Field(default="1:1", max_length=8)
    n: int = Field(default=1, ge=1, le=9)
    optimizer: bool = False
    seed: int | None = Field(default=None, ge=0, le=2**31 - 1)
    reference: dict | None = None


class ClearIn(BaseModel):
    id: str = Field(min_length=32, max_length=32)
    order: str = Field(pattern=r"^(newest|oldest)$")   # required: the page's order, never a guess
    dry_run: bool = False
    expect: int | None = Field(default=None, ge=0)   # the dry run's count the person confirmed


def _clear_rows(rows: list[dict], iid: str, order: str) -> tuple[list[dict], list[dict]] | None:
    """(kept, removed) for "this picture and everything after it" in ``order``.
    ``rows`` is the ledger, oldest first. None when ``iid`` is not in it."""
    idx = next((i for i, r in enumerate(rows) if r.get("id") == iid), None)
    if idx is None:
        return None
    if order == "oldest":                 # shown oldest first: "down" is newer
        return rows[:idx], rows[idx:]
    return rows[idx + 1:], rows[:idx + 1]  # shown newest first: "down" is older


def _rewrite_ledger(rows: list[dict]) -> None:
    tmp = _ledger().with_name(_ledger().name + ".tmp")
    tmp.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    os.replace(tmp, _ledger())


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #


def build(ctx):
    global CTX
    CTX = ctx
    op = [Depends(ctx.require_operator)]

    @router.get("/state", dependencies=op)
    async def state(request: Request):
        from app import policy
        providers, default = _provider_rows()
        rows = await asyncio.to_thread(_read_ledger)
        days: dict[str, int] = {}
        for r in rows:
            days[r.get("day", "")] = days.get(r.get("day", ""), 0) + 1
        recent_days = [{"day": d, "count": c} for d, c in sorted(days.items(), reverse=True)[:7]]
        return {
            "providers": providers, "default": default,
            "ratios": list(image_providers.MINIMAX_RATIOS),
            "prompt_max": PROMPT_MAX,
            "today": _today(), "today_count": days.get(_today(), 0),
            "days": recent_days, "total": len(rows),
            "prompts": await asyncio.to_thread(_recent_prompts),
            "busy": _lock.locked(),
            # "Clear from here down" is offered only to a profile that may delete.
            "can_clear": policy.of(request).can("manage_history"),
            # The empty state's "Set up image generation" opens Settings -> AI & Connections;
            # offered only to a profile that may change it.
            "can_configure": policy.of(request).can("manage_connections"),
        }

    @router.get("/images", dependencies=op)
    async def images(limit: int = Query(60, ge=1, le=200)):
        rows = await asyncio.to_thread(_read_ledger)
        return {"images": [_public_row(r) for r in rows[-limit:]]}   # newest last

    @router.post("/generate", dependencies=op)
    async def generate(body: GenerateIn):
        prompt = body.prompt.strip()
        if not prompt:
            raise HTTPException(422, "Write a prompt first")
        if len(prompt) > PROMPT_MAX:
            raise HTTPException(
                422, f"The prompt is {len(prompt)} characters; the limit is {PROMPT_MAX}")
        _providers, default = _provider_rows()
        pid = (body.provider or default).strip().lower()
        if not pid:
            raise HTTPException(
                409, "No image provider has an API key yet (Settings → AI models → Image generation)")
        if pid not in image_providers.PRESET_BY_ID:
            raise HTTPException(422, "Unknown image provider")
        model = body.model.strip()
        if model and not MODEL_RE.match(model):
            raise HTTPException(422, "That model name is not valid")
        ratio = body.ratio.strip()
        if ratio and not image_jobs._RATIO_RE.match(ratio):
            # ImageSpec would raise ValueError below -- a 500, not a 422.
            raise HTTPException(422, "The aspect ratio must look like 16:9")
        res = _resolved(pid, model)
        if res is None:
            raise HTTPException(
                409, "That provider has no API key yet (Settings → AI models → Image generation)")
        caps = image_providers.supports(res)
        n = min(body.n, caps["max_n"])
        wants_ref = bool(body.reference)
        if wants_ref and not caps["reference"]:
            raise HTTPException(422, "This provider does not take a reference image")
        reference = _reference_data_url(body.reference) if wants_ref else None
        if _lock.locked():
            raise HTTPException(409, "A generation is already running")
        async with _lock:
            spec = image_jobs.ImageSpec(prompt=prompt, ratio=ratio, backend="api")
            try:
                blobs = await image_providers.generate_batch(
                    res, spec, n=n,
                    seed=body.seed if caps["seed"] else None,
                    optimizer=bool(body.optimizer and caps["optimizer"]),
                    reference=reference)
            except image_jobs.ImageJobError as e:
                raise HTTPException(502, e.message) from None
            rows = await asyncio.to_thread(
                _store, blobs, prompt, pid, res.model, ratio,
                body.seed if caps["seed"] else None,
                bool(body.optimizer and caps["optimizer"]), wants_ref)
        return {"images": [_public_row(r) for r in rows], "asked": n}

    @router.post("/clear", dependencies=op)
    async def clear(body: ClearIn, request: Request):
        from app import policy
        if not policy.of(request).can("manage_history"):
            raise HTTPException(403, "Your profile cannot delete pictures")
        if not IMAGE_ID_RE.match(body.id):
            raise HTTPException(422, "That is not a picture id")
        if _lock.locked():
            raise HTTPException(409, "A generation is running; try again when it is done")
        async with _lock:
            rows = await asyncio.to_thread(_read_ledger)
            split = _clear_rows(rows, body.id, body.order)
            if split is None:
                raise HTTPException(404, "That picture is already gone")
            kept, removed = split
            urls = [str(r.get("url") or "") for r in removed]
            in_chat = await ctx.db.media_urls_in_messages(urls)
            still_used = in_chat | {str(r.get("url") or "") for r in kept}
            if body.dry_run:
                return {"count": len(removed), "in_chat": len(in_chat), "dry_run": True}
            if body.expect is not None and body.expect != len(removed):
                raise HTTPException(409, "The pictures changed since you confirmed; count again")
            await asyncio.to_thread(_rewrite_ledger, kept)
            files = 0
            for url in dict.fromkeys(urls):
                m = MEDIA_URL_RE.match(url)
                path = _media_file(m.group(1)) if m and url not in still_used else None
                if path is not None:
                    with contextlib.suppress(OSError):
                        path.unlink()
                        files += 1
        log.info("image studio: cleared %d picture(s) (%d file(s); %d kept for chats)",
                 len(removed), files, len(in_chat))
        return {"deleted": len(removed), "files": files, "in_chat": len(in_chat)}

    @router.get("/chat-images", dependencies=op)
    async def chat_images(limit: int = Query(48, ge=1, le=120)):
        threads = await ctx.db.all_threads(include_archived=False)
        threads.sort(key=lambda t: t.updated_at, reverse=True)
        out: list[dict] = []
        seen: set[str] = set()
        for th in threads[:15]:
            msgs, _more = await ctx.db.list_messages(th.id, limit=40)
            for m in reversed(msgs):                       # newest first
                cands = [m.media_url or "", *_MEDIA_IN_TEXT.findall(m.content or "")]
                for url in cands:
                    mm = MEDIA_URL_RE.match(url)
                    if not mm or url in seen or not _MEDIA_IN_TEXT.fullmatch(url):
                        continue
                    seen.add(url)
                    out.append({"url": url, "thread_id": th.id,
                                "thread": th.title or "", "created_at": m.created_at})
        out.sort(key=lambda r: r["created_at"], reverse=True)
        return {"images": out[:limit]}

    @router.get("/targets", dependencies=op)
    async def targets():
        threads = await ctx.db.all_threads(include_archived=False)
        threads.sort(key=lambda t: t.updated_at, reverse=True)
        names = {b.id: b.name for b in ctx.config.load_bots()}
        return {
            "threads": [{"id": t.id, "title": t.title or "", "bot_id": t.bot_id,
                         "bot": names.get(t.bot_id, t.bot_id)} for t in threads[:60]],
            "bots": [{"id": b.id, "name": b.name} for b in ctx.config.load_bots()],
        }

    return router


def _store(blobs: list[bytes], prompt: str, provider: str, model: str, ratio: str,
           seed: int | None, optimizer: bool, reference: bool) -> list[dict]:
    """Write the pictures into the media store and the ledger (one row each)."""
    from app import config

    config.MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    now = datetime.now(UTC)
    batch = uuid.uuid4().hex[:12]
    rows: list[dict] = []
    for blob in blobs:
        iid = uuid.uuid4().hex
        name = f"{iid}{image_jobs.image_suffix(blob)}"
        dest = config.MEDIA_DIR / name
        tmp = dest.with_name(dest.name + ".part")
        tmp.write_bytes(blob)
        os.replace(tmp, dest)
        rows.append({
            "id": iid, "url": f"/media/{name}", "prompt": prompt, "provider": provider,
            "model": model, "ratio": ratio, "seed": seed, "optimizer": optimizer,
            "reference": reference, "batch": batch,
            "created_at": now.isoformat(timespec="seconds"), "day": _today()})
    with _ledger().open("a", encoding="utf-8") as f:
        f.write("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
    _remember_prompt(prompt)
    log.info("image studio: %d picture(s) from %s/%s", len(rows), provider, model)
    return rows
