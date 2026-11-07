"""Image Studio's backend (``apps/imagestudio/backend.py``).

Mounted by the shell's app loader at ``/api/apps/imagestudio``. Every route needs
the app's permission (``use_images``, ``ctx.require_operator``) on top of the
loader's own gate, so a profile without it, a cookieless caller and a forged
cookie all get the uniform 403 before any of this runs.

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
ordinary inject path and No-Image Mode strips them by the rules it already has.
Each picture gets one row in ``<data>/apps-data/imagestudio/images.jsonl``
(prompt, provider, model, ratio, seed, ..., and ``owner_profile_id``: the profile
that made it). The per-day count is read from that ledger.

OWNERSHIP (4.0)
---------------
A profile's pictures are its own, Admins included: ``/state``, ``/images``,
``/clear`` and a ``result`` reference read only the caller's rows (``_mine``). A
row from before rows had an owner is the protected Admin's (``ownership``'s rule
for an unassigned row). ``prompts-<profile id>.json`` holds a profile's recent
prompts (the protected Admin falls back to the 3.x ``prompts.json``); when neither
exists, an optional ``seed-prompts.json`` (never shipped in the repo) supplies the
first list. Reset / downgrade (``profiles_service``) drop a profile's rows and its
prompts file.

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
that lets a profile delete its conversations), and clears the caller's own
pictures only.

COST
----
One accepted request is one provider call (Civitai: one submit, polled to completion under a
single deadline; ``/estimate`` is a free dry-run price in Buzz and makes no picture). Nothing here retries, warms up or
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
import threading
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app import image_access, image_guard, image_jobs, image_providers

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
#: Civitai's default checkpoints (a datalist, not a lock: any SDXL checkpoint link or AIR works).
CIVITAI_MODEL_NAMES = {
    "urn:air:sdxl:checkpoint:civitai:133005@1759168": "Juggernaut XL (Ragnarok)",
    "urn:air:sdxl:checkpoint:civitai:139562@789646": "RealVisXL V5.0",
    "urn:air:sdxl:checkpoint:civitai:257749@290640": "Pony Diffusion V6 XL",
    "urn:air:sdxl:checkpoint:civitai:827184@2883731": "WAI-illustrious-SDXL v17",
}
KNOWN_MODELS = {"minimax": ["image-01", "image-01-live"], "civitai": list(CIVITAI_MODEL_NAMES)}
NOT_ALLOWED = ("Your profile type can't use {label}. Ask the Owner "
               "(Settings → AI & Connections → Image generation).")

#: Guards the ledger file (append in _store, rewrite in clear); never held across a provider call.
_lock = threading.Lock()

#: Several generations may run at once. Each profile may have this many in flight (the
#: 429 says so), and the whole install this many, so a household cannot flood a metered
#: provider. Counted in memory: a restart forgets them, as it forgets the requests.
MAX_PER_PROFILE = 4
MAX_TOTAL = 8
_inflight: dict[str, int] = {}
_inflight_lock = threading.Lock()


def _take_slot(owner: str | None) -> str | None:
    """Reserve an in-flight slot for ``owner``; the refusal text, or None when taken."""
    key = owner or ""
    with _inflight_lock:
        if _inflight.get(key, 0) >= MAX_PER_PROFILE:
            return f"{MAX_PER_PROFILE} pictures are already being made; wait for one to finish"
        if sum(_inflight.values()) >= MAX_TOTAL:
            return "The studio is busy making pictures for the household; try again in a moment"
        _inflight[key] = _inflight.get(key, 0) + 1
    return None


def _free_slot(owner: str | None) -> None:
    key = owner or ""
    with _inflight_lock:
        n = _inflight.get(key, 0) - 1
        if n > 0:
            _inflight[key] = n
        else:
            _inflight.pop(key, None)


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


#: A profile id as it may appear in a file name (accounts mint hex/uuid ids).
_PID_RE = re.compile(r"\A[A-Za-z0-9_-]{1,64}\Z")


def _owner_of(request: Request) -> str | None:
    """Whose pictures a request's new rows are (the uploader's profile; a machine's or an
    open install's are the protected Admin's, as for every stamped resource)."""
    from app import ownership
    return ownership.for_request(request)


def _mine(rows: list[dict], request: Request) -> list[dict]:
    """The rows the caller may see: its own (``ownership.may_see_thread`` semantics - a row
    with no owner is the protected Admin's; an open install sees all)."""
    from app import ownership, policy
    pr = policy.of(request).principal
    return [r for r in rows if ownership.may_see_thread(pr, r.get("owner_profile_id"), write=True)]


def _prompts_path(owner: str | None) -> Path:
    if owner and _PID_RE.match(owner):
        return _dir() / f"prompts-{owner}.json"
    return _dir() / "prompts.json"


def _recent_prompts(owner: str | None) -> list[str]:
    from app import ownership
    own = _load_json_list(_prompts_path(owner))
    if own is not None:
        return own
    if owner and owner == ownership.protected_admin_id():
        legacy = _load_json_list(_dir() / "prompts.json")     # the 3.x shared list was theirs
        if legacy is not None:
            return legacy
    return _load_json_list(_dir() / "seed-prompts.json") or []


_PROMPTS_LOCK = threading.Lock()


def _remember_prompt(prompt: str, owner: str | None) -> None:
    """Put ``prompt`` first in the owner's recent list. Parallel jobs run this on worker threads:
    the read-modify-write holds a lock and each write has its own temp name, and a failure is
    logged, never raised (the picture already exists; failing the request would bill a retry)."""
    try:
        with _PROMPTS_LOCK:
            items = [p for p in _recent_prompts(owner) if p != prompt]
            items.insert(0, prompt)
            dest = _prompts_path(owner)
            tmp = dest.with_name(f"{dest.name}.{uuid.uuid4().hex}.tmp")
            try:
                tmp.write_text(json.dumps(items[:RECENT_MAX], ensure_ascii=False), encoding="utf-8")
                os.replace(tmp, dest)
            finally:
                tmp.unlink(missing_ok=True)
    except OSError:
        logging.getLogger("local-chat.imagestudio").warning("could not save the recent prompts", exc_info=True)


def _today() -> str:
    return datetime.now().astimezone().date().isoformat()


def _public_row(row: dict) -> dict:
    return {k: row.get(k) for k in (
        "id", "url", "prompt", "provider", "model", "ratio", "seed", "optimizer",
        "reference", "batch", "created_at", "day", "currency")}


# --------------------------------------------------------------------------- #
# Providers
# --------------------------------------------------------------------------- #


def _cfg_for(preset_id: str, model: str = "") -> dict:
    """The config used for ``preset_id``: the stored one when that is the stored
    provider, else the preset's own environment key (and a chat connection to
    the same host, via ``resolve``'s shared-key rule). The key stays inside
    ``resolve``."""
    stored = image_providers.load_config()
    preset = image_providers.PRESET_BY_ID[preset_id]
    if image_providers.provider_id(stored) == preset_id and not preset.studio_only:
        cfg = dict(stored)
    else:
        # A studio-only preset never reads image.yaml (its key is its own).
        cfg = {"provider": preset_id, "api_key_env": preset.key_env}
    if model:
        cfg["model"] = model
    return cfg


def _resolved(preset_id: str, model: str = "") -> image_providers.Resolved | None:
    try:
        return image_providers.resolve(_cfg_for(preset_id, model), studio=True)
    except image_jobs.ImageJobError:
        return None


def _provider_rows(principal=None) -> tuple[list[dict], str]:
    """The providers ``principal`` may use (the access matrix), and the default among them."""
    rows: list[dict] = []
    for preset in image_providers.PRESETS:
        if principal is not None and not image_access.allows(principal, preset.access):
            continue
        res = _resolved(preset.id)
        row: dict[str, Any] = {
            "id": preset.id, "label": preset.label, "shape": preset.shape,
            "configured": res is not None, "price_note": preset.price_note,
            "model": res.model if res else preset.model,
            "models": KNOWN_MODELS.get(preset.id, []),
        }
        if preset.shape == "civitai":
            row["estimate"] = True
            row["currencies"] = list(image_providers.CIVITAI_CURRENCIES)
            row["browse"] = True
            row["model_names"] = dict(CIVITAI_MODEL_NAMES)
        row["supports"] = (image_providers.supports(res) if res else
                           {"reference": preset.shape == "minimax",
                            "optimizer": preset.shape == "minimax",
                            "seed": preset.shape in ("minimax", "civitai"),
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


def _reference_data_url(ref: dict, request: Request) -> str:
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
        row = (next((r for r in _mine(_read_ledger(), request) if r["id"] == iid), None)
               if IMAGE_ID_RE.match(iid) else None)
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
    # Civitai only: which Buzz pays ("blue" | "yellow"); "" = let Civitai choose.
    currency: str = Field(default="", max_length=16)


#: "Delete by age": how far back from NOW (the server's clock) each scope reaches.
SINCE_SECONDS = {"all": None, "hour": 3600, "day": 86400}


class ClearIn(BaseModel):
    # Either "this picture and everything after it" (id + order) or "everything from the
    # last hour/day/all" (since) - never both, never neither.
    id: str | None = Field(default=None, min_length=32, max_length=32)
    order: str | None = Field(default=None, pattern=r"^(newest|oldest)$")   # with id: the page's order, never a guess
    since: str | None = Field(default=None, pattern=r"^(all|hour|day)$")
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


def _created(row: dict) -> datetime | None:
    try:
        d = datetime.fromisoformat(str(row.get("created_at") or ""))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=UTC)


def _clear_since(rows: list[dict], since: str, now: datetime | None = None) -> list[dict]:
    """The rows made within ``since`` of now (``all`` = every row). A row whose time cannot
    be read is only removed by ``all``."""
    secs = SINCE_SECONDS[since]
    if secs is None:
        return list(rows)
    cutoff = (now or datetime.now(UTC)).timestamp() - secs
    return [r for r in rows if (d := _created(r)) is not None and d.timestamp() >= cutoff]


def _rewrite_without(ids: set[str]) -> None:
    """Drop ``ids`` from the ledger as it is NOW (a picture stored since the caller looked stays)."""
    with _lock:
        _rewrite_ledger([r for r in _read_ledger() if r.get("id") not in ids])


def _rewrite_ledger(rows: list[dict]) -> None:
    tmp = _ledger().with_name(_ledger().name + ".tmp")
    tmp.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    os.replace(tmp, _ledger())


def _check_prompt(raw: str) -> str:
    """The trimmed prompt, or an HTTPException: empty / too long / a hard-line refusal.
    Runs before any provider or key lookup so a refusal never reaches a metered API
    (adult-only; no sexual imagery of real people), with the chat path's own message."""
    prompt = raw.strip()
    if not prompt:
        raise HTTPException(422, "Write a prompt first")
    if len(prompt) > PROMPT_MAX:
        raise HTTPException(
            422, f"The prompt is {len(prompt)} characters; the limit is {PROMPT_MAX}")
    try:
        image_guard.check_prompt(prompt)
    except image_guard.ImageGuardError as e:
        raise HTTPException(422, e.message) from None
    return prompt


async def _pick(body, request: Request) -> tuple[str, image_providers.Resolved, str]:
    """Provider, access check, model and key for a Generate / Estimate request:
    ``(provider id, resolved call, ratio)`` or an HTTPException."""
    from app import policy
    principal = policy.of(request).principal
    providers, default = _provider_rows(principal)
    pid = (body.provider or default).strip().lower()
    if not pid:
        if not providers:
            raise HTTPException(403, "Your profile type can't use the image generators here. Ask the Owner.")
        raise HTTPException(
            409, "No image provider has an API key yet (Settings → AI & Connections → Image generation)")
    preset = image_providers.PRESET_BY_ID.get(pid)
    if preset is None:
        raise HTTPException(422, "Unknown image provider")
    if not image_access.allows(principal, preset.access):
        raise HTTPException(403, NOT_ALLOWED.format(label=preset.label))
    model = body.model.strip()
    if preset.shape == "civitai":
        try:
            model = await image_providers.civitai_model(model or preset.model)
        except ValueError as e:
            raise HTTPException(422, str(e)) from None
    elif model and not MODEL_RE.match(model):
        raise HTTPException(422, "That model name is not valid")
    ratio = body.ratio.strip()
    if ratio and not image_jobs._RATIO_RE.match(ratio):
        # ImageSpec would raise ValueError later -- a 500, not a 422.
        raise HTTPException(422, "The aspect ratio must look like 16:9")
    res = _resolved(pid, model)
    if res is None:
        raise HTTPException(
            409, "That provider has no API key yet (Settings → AI & Connections → Image generation)")
    return pid, res, ratio


def _currency(body) -> str | None:
    """The Buzz type a Civitai request asks for: blue, yellow, or None (Civitai chooses).
    Other providers ignore it, but a value that is neither is refused for everyone."""
    value = (body.currency or "").strip().lower()
    if value and value not in image_providers.CIVITAI_CURRENCIES:
        raise HTTPException(422, "Pick blue or yellow Buzz")
    return value or None


#: A balance check hits Civitai with the Owner's key: at most one per owner every few seconds.
BALANCE_MIN_GAP_S = 5.0
_balance_last: dict[str, float] = {}
_balance_lock = threading.Lock()


def _balance_gap_ok(owner: str | None) -> bool:
    now = time.monotonic()
    key = owner or ""
    with _balance_lock:
        last = _balance_last.get(key)
        if last is not None and now - last < BALANCE_MIN_GAP_S:
            return False
        _balance_last[key] = now
        if len(_balance_last) > 256:
            for k in [k for k, t in _balance_last.items() if now - t > 60]:
                del _balance_last[k]
    return True


def _civitai_gate(request: Request) -> None:
    """403 unless the caller's profile type may use Civitai."""
    from app import policy
    preset = image_providers.PRESET_BY_ID["civitai"]
    if not image_access.allows(policy.of(request).principal, preset.access):
        raise HTTPException(403, NOT_ALLOWED.format(label=preset.label))



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
        providers, default = _provider_rows(policy.of(request).principal)
        rows = _mine(await asyncio.to_thread(_read_ledger), request)
        days: dict[str, int] = {}
        for r in rows:
            days[r.get("day", "")] = days.get(r.get("day", ""), 0) + 1
        recent_days = [{"day": d, "count": c} for d, c in sorted(days.items(), reverse=True)[:7]]
        return {
            "providers": providers, "default": default,
            # True only when the who-may-make-pictures matrix removed every provider
            # (not when keys are merely missing).
            "blocked": not providers,
            "ratios": list(image_providers.MINIMAX_RATIOS),
            "prompt_max": PROMPT_MAX,
            "today": _today(), "today_count": days.get(_today(), 0),
            "days": recent_days, "total": len(rows),
            "prompts": await asyncio.to_thread(_recent_prompts, _owner_of(request)),
            "busy": bool(_inflight),
            "in_flight": _inflight.get(_owner_of(request) or "", 0), "max_in_flight": MAX_PER_PROFILE,
            # "Clear from here down" is offered only to a profile that may delete.
            "can_clear": policy.of(request).can("manage_history"),
            # The empty state's "Set up image generation" opens Settings -> AI & Connections;
            # offered only to a profile that may change it.
            "can_configure": policy.of(request).can("manage_connections"),
            # "Send to chat" posts through /api/inject (manage_agents): offered only to a
            # profile that may (anyone else would only ever meet a 403).
            "can_send": policy.of(request).can("manage_agents"),
        }

    @router.get("/images", dependencies=op)
    async def images(request: Request, limit: int = Query(60, ge=1, le=200)):
        rows = _mine(await asyncio.to_thread(_read_ledger), request)
        return {"images": [_public_row(r) for r in rows[-limit:]]}   # newest last

    @router.post("/generate", dependencies=op)
    async def generate(body: GenerateIn, request: Request):
        prompt = _check_prompt(body.prompt)
        pid, res, ratio = await _pick(body, request)
        currency = _currency(body) if res.preset.shape == "civitai" else None
        caps = image_providers.supports(res)
        n = min(body.n, caps["max_n"])
        wants_ref = bool(body.reference)
        if wants_ref and not caps["reference"]:
            raise HTTPException(422, "This provider does not take a reference image")
        reference = _reference_data_url(body.reference, request) if wants_ref else None
        owner = _owner_of(request)
        refusal = _take_slot(owner)
        if refusal:
            raise HTTPException(429, refusal)
        try:
            spec = image_jobs.ImageSpec(prompt=prompt, ratio=ratio, backend="api")
            try:
                blobs = await image_providers.generate_batch(
                    res, spec, n=n,
                    seed=body.seed if caps["seed"] else None,
                    optimizer=bool(body.optimizer and caps["optimizer"]),
                    reference=reference, currency=currency)
            except image_jobs.ImageJobError as e:
                raise HTTPException(502, e.message) from None
            rows = await asyncio.to_thread(
                _store, blobs, prompt, pid, res.model, ratio,
                body.seed if caps["seed"] else None,
                bool(body.optimizer and caps["optimizer"]), wants_ref, owner, currency)
        finally:
            _free_slot(owner)
        return {"images": [_public_row(r) for r in rows], "asked": n}

    @router.post("/estimate", dependencies=op)
    async def estimate(body: GenerateIn, request: Request):
        """Civitai's own Buzz price for a Generate body. Information only: no slot, no
        ledger row, no file; the same guard and access checks as /generate."""
        prompt = _check_prompt(body.prompt)
        pid, res, ratio = await _pick(body, request)
        if res.preset.shape != "civitai":
            raise HTTPException(422, "No estimate for this provider")
        currency = _currency(body)
        n = min(body.n, image_providers.supports(res)["max_n"])
        spec = image_jobs.ImageSpec(prompt=prompt, ratio=ratio, backend="api")
        try:
            buzz = await image_providers.estimate_civitai(res, spec, n=n, currency=currency)
        except image_jobs.ImageJobError as e:
            raise HTTPException(502, e.message) from None
        return {"buzz": buzz, "n": n}

    @router.get("/civitai/balance", dependencies=op)
    async def civitai_balance(request: Request):
        """The Civitai account's Buzz by type. The button is the refresh, so nothing is
        cached here; a per-owner minimum gap keeps a held-down button off Civitai."""
        _civitai_gate(request)
        res = _resolved("civitai")
        if res is None:
            raise HTTPException(
                409, "That provider has no API key yet (Settings → AI & Connections → Image generation)")
        if not _balance_gap_ok(_owner_of(request)):
            raise HTTPException(429, "Wait a few seconds before checking the Buzz balance again")
        try:
            balances = await image_providers.civitai_balance(res)
        except image_jobs.ImageJobError as e:
            if e.code == "scope":
                return {"balances": None, "scope_missing": True,
                        "help_url": "https://civitai.com/user/account"}
            raise HTTPException(502, e.message) from None
        return {"balances": balances, "checked": datetime.now(UTC).isoformat(timespec="seconds")}

    @router.get("/civitai/models", dependencies=op)
    async def civitai_models(request: Request, sort: str = "week", q: str = "",
                             fresh: bool = False):
        """Popular Civitai checkpoints (public list, no key needed) to pick a model from.
        ``q`` keeps only models whose name holds every word of it."""
        _civitai_gate(request)
        if sort not in image_providers.CIVITAI_SORTS:
            raise HTTPException(422, "Pick top this week, top this month, highest rated or newest")
        try:
            rows = await image_providers.civitai_top_models(sort, query=q, fresh=fresh)
        except image_jobs.ImageJobError as e:
            raise HTTPException(502, e.message) from None
        return {"models": rows, "sort": sort, "q": image_providers.browse_query(q)}

    @router.post("/clear", dependencies=op)
    async def clear(body: ClearIn, request: Request):
        from app import policy
        if not policy.of(request).can("manage_history"):
            raise HTTPException(403, "Your profile cannot delete pictures")
        by_age = body.since is not None
        if by_age == (body.id is not None or body.order is not None):
            raise HTTPException(422, "Give either a picture id with its order, or a time range")
        if not by_age and (body.id is None or body.order is None):
            raise HTTPException(422, "A picture id needs the page's order")
        if body.id is not None and not IMAGE_ID_RE.match(body.id):
            raise HTTPException(422, "That is not a picture id")
        rows = await asyncio.to_thread(_read_ledger)
        # The caller's own pictures only: "this one and everything after it" in the
        # order the caller's page shows them. Everyone else's rows are kept untouched.
        mine = _mine(rows, request)
        if by_age:
            removed = _clear_since(mine, body.since)
        else:
            split = _clear_rows(mine, body.id, body.order)
            if split is None:
                raise HTTPException(404, "That picture is already gone")
            _kept_mine, removed = split
        gone = {id(r) for r in removed}
        kept = [r for r in rows if id(r) not in gone]
        urls = [str(r.get("url") or "") for r in removed]
        in_chat = await ctx.db.media_urls_in_messages(urls)
        still_used = in_chat | {str(r.get("url") or "") for r in kept}
        if body.dry_run:
            return {"count": len(removed), "in_chat": len(in_chat), "dry_run": True}
        if body.expect is not None and body.expect != len(removed):
            raise HTTPException(409, "The pictures changed since you confirmed; count again")
        await asyncio.to_thread(_rewrite_without, {str(r.get('id')) for r in removed})
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
    async def chat_images(request: Request, limit: int = Query(48, ge=1, le=120)):
        from app import policy
        # 4.0: the caller's OWN chats only (before 4.0 every thread in the house).
        threads = await ctx.db.all_threads(include_archived=False,
                                           principal=policy.of(request).principal)
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
    async def targets(request: Request):
        from app import characters, policy
        pr = policy.of(request).principal
        # 4.0: the caller's OWN chats, with characters the caller may use (before 4.0 every
        # thread title and character in the house reached any Member who may make pictures).
        threads = await ctx.db.all_threads(include_archived=False, principal=pr)
        threads = [t for t in threads if characters.may_use_id(t.bot_id, pr)]
        threads.sort(key=lambda t: t.updated_at, reverse=True)
        bots = [b for b in ctx.config.load_bots() if characters.may_use(b, pr)]
        names = {b.id: b.name for b in bots}
        # "Set as bot avatar" lists only the bots whose picture the caller may change: the
        # caller's own (through /api/me/characters/{id}/avatar) and, for a character manager,
        # the household ones (/api/bots/{id}/avatar).
        manage = policy.of(request).can("manage_characters")
        own = policy.of(request).can("create_characters")
        pickable = [b for b in bots if (b.scope == "personal" and b.owner_profile == pr.profile_id and own)
                    or (b.scope != "personal" and manage)]
        return {
            "threads": [{"id": t.id, "title": t.title or "", "bot_id": t.bot_id,
                         "bot": names.get(t.bot_id, t.bot_id)} for t in threads[:60]],
            "bots": [{"id": b.id, "name": b.name, "personal": b.scope == "personal"} for b in pickable],
        }

    return router


def _store(blobs: list[bytes], prompt: str, provider: str, model: str, ratio: str,
           seed: int | None, optimizer: bool, reference: bool, owner: str | None,
           currency: str | None = None) -> list[dict]:
    """Write the pictures into the media store and the ledger (one row each, stamped with
    ``owner_profile_id``)."""
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
            "created_at": now.isoformat(timespec="seconds"), "day": _today(),
            "owner_profile_id": owner, **({"currency": currency} if currency else {})})
    with _lock, _ledger().open("a", encoding="utf-8") as f:
        f.write("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
    _remember_prompt(prompt, owner)
    log.info("image studio: %d picture(s) from %s/%s", len(rows), provider, model)
    return rows
