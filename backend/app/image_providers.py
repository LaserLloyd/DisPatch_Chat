"""Image generation through a standard API: OpenAI-shape and MiniMax.

WHY THIS EXISTS
---------------
``image_jobs`` drives an MCP image server (a ComfyUI-style rig). Most people
who install DisPatch do not have one; they have an API key. This module is the
second backend for the same worker: it answers the five calls the worker makes
on the MCP client (``readiness``, ``enqueue``, ``poll``, ``fetch``, ``cancel``,
plus ``status`` for /api/health), so the placeholder → picture rewrite, the
deadline, coalescing, rate limits and Safe-Mode redaction all apply unchanged.
See docs/design/integrations-2-3.md.

An API generation is ONE synchronous HTTP request that takes 5-60 s. ``enqueue``
starts it as an in-process task and returns at once; ``poll`` reports the task;
``fetch`` hands back the validated bytes. A restart loses the task, and the
next poll says so (``job_lost``) exactly the way a restarted rig does.

WIRE SHAPES
-----------
``openai-images``  ``POST {base}/images/generations`` ``{model, prompt, n: 1,
                   size?}`` → ``data[0].b64_json`` or ``data[0].url``. OpenAI,
                   xAI Grok, and any compatible host.
``minimax``        ``POST {base}/image_generation`` ``{model: "image-01",
                   prompt, aspect_ratio, response_format: "base64", n: 1}`` →
                   ``data.image_base64[0]`` (or ``data.image_urls[0]``), with
                   ``base_resp.status_code`` 0 on success.

CONFIGURATION
-------------
``<data>/image.yaml`` (0600): ``provider`` (``""`` automatic = the MCP image
server if one is configured, ``none``, ``clawforge``, or a preset id),
``base_url``, ``model``, ``api_key`` and/or ``api_key_env``. The key is never
returned by any route. With no key of its own, a preset borrows the key of a
chat connection to the SAME host, so a MiniMax chat key also draws pictures.

COST
----
API pictures are metered. Exactly one generation is made per accepted request;
nothing here retries, warms up, or refills a pool.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
import os
import re
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
import yaml

from . import config, image_jobs, llm_api

log = logging.getLogger("local-chat.image_providers")

CONFIG_NAME = "image.yaml"

#: One generation: generous, because a busy provider takes a minute. The job's
#: own deadline (image_jobs.DEADLINE_S) still governs.
GENERATE_TIMEOUT_S = 180.0
CONNECT_TIMEOUT_S = 10.0
#: A finished result nobody fetched is dropped after this long.
RESULT_TTL_S = 900.0
#: MiniMax documents a 1500-character prompt cap.
MINIMAX_PROMPT_MAX = 1500

#: Provider ids that are not presets.
AUTO, NONE, CLAWFORGE = "", "none", "clawforge"

#: Test seam: an httpx transport used instead of the network.
transport: Any = None


@dataclass(frozen=True)
class ImagePreset:
    id: str
    label: str
    shape: str                 # "openai-images" | "minimax"
    base_url: str = ""
    model: str = ""
    key_env: str = ""
    docs: str = ""
    #: "openai" maps a ratio to OpenAI's three sizes; "" sends no size at all
    #: (the provider's default, square) -- for hosts whose size vocabulary is
    #: not documented.
    size_mode: str = ""
    editable_base_url: bool = False
    key_required: bool = True
    price_note: str = ""

    def public(self) -> dict:
        return {"id": self.id, "label": self.label, "shape": self.shape,
                "base_url": self.base_url, "model": self.model,
                "key_env": self.key_env, "docs": self.docs,
                "editable_base_url": self.editable_base_url,
                "key_required": self.key_required, "price_note": self.price_note}


PRESETS: tuple[ImagePreset, ...] = (
    ImagePreset(
        id="minimax", label="MiniMax", shape="minimax",
        base_url="https://api.minimax.io/v1", model="image-01",
        key_env="MINIMAX_API_KEY", docs="https://platform.minimax.io/",
        price_note="Billed per image, or from a Token Plan's image quota."),
    ImagePreset(
        id="openai", label="OpenAI", shape="openai-images",
        base_url="https://api.openai.com/v1", model="gpt-image-1",
        key_env="OPENAI_API_KEY", docs="https://platform.openai.com/api-keys",
        size_mode="openai", price_note="Billed per image."),
    ImagePreset(
        id="xai", label="xAI Grok", shape="openai-images",
        base_url="https://api.x.ai/v1", model="grok-imagine-image",
        key_env="XAI_API_KEY", docs="https://console.x.ai/",
        price_note="Billed per image."),
    ImagePreset(
        id="custom", label="Custom (OpenAI-compatible)", shape="openai-images",
        editable_base_url=True, key_required=False, size_mode="openai",
        price_note="Depends on the host."),
)
PRESET_BY_ID = {p.id: p for p in PRESETS}

#: MiniMax image-01's documented aspect ratios.
MINIMAX_RATIOS = ("1:1", "16:9", "4:3", "3:2", "2:3", "3:4", "9:16", "21:9")


# --------------------------------------------------------------------------- #
# Config file
# --------------------------------------------------------------------------- #


def config_path() -> Path:
    return config.DATA_DIR / CONFIG_NAME


def load_config() -> dict:
    """The stored config, or {} (automatic). A broken file reads as {} with a
    log line: the MCP path then behaves exactly as it did before this file."""
    try:
        doc = yaml.safe_load(config_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, yaml.YAMLError) as e:
        log.warning("%s is unreadable (%s); image provider = automatic",
                    CONFIG_NAME, e)
        return {}
    if not isinstance(doc, dict):
        return {}
    out = {k: str(doc.get(k) or "").strip()
           for k in ("provider", "base_url", "model", "api_key", "api_key_env")}
    return out


def save_config(cfg: dict) -> None:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = {k: v for k, v in cfg.items() if v}
    text = yaml.safe_dump(doc, sort_keys=False) if doc else "{}\n"
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{CONFIG_NAME}.")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def provider_id(cfg: dict | None = None) -> str:
    cfg = load_config() if cfg is None else cfg
    return str(cfg.get("provider") or "").strip().lower()


def api_selected(cfg: dict | None = None) -> bool:
    """Is the default provider one of the API presets?"""
    return provider_id(cfg) in PRESET_BY_ID


def choose_backend(workflow: str, *, clawforge: bool,
                   cfg: dict | None = None) -> str:
    """Which backend a NEW job uses: "clawforge", "api", or "" (none).

    A bot naming a workflow keeps the MCP image server whenever one is
    configured. Everything else follows the default provider; automatic (no
    image.yaml) is the MCP server if configured -- the pre-2.3.7 behaviour.
    """
    pid = provider_id(cfg)
    if pid in PRESET_BY_ID:
        return CLAWFORGE if (workflow and clawforge) else "api"
    if pid == NONE:
        return CLAWFORGE if (workflow and clawforge) else ""
    return CLAWFORGE if clawforge else ""


# --------------------------------------------------------------------------- #
# Resolving a config into a call
# --------------------------------------------------------------------------- #


@dataclass
class Resolved:
    preset: ImagePreset
    base_url: str
    model: str
    api_key: str
    key_source: str            # "stored" | "env" | "shared" | ""


def _host(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""


def shared_chat_key(base_url: str) -> str:
    """The key of a chat connection to the same host, or "".

    "Same host" is the whole rule: a key is only ever sent where its owner
    already sends it. Direct-API bots (advisor.providers) and legacy api: bots
    both count."""
    host = _host(base_url)
    if not host:
        return ""
    for bot in config.load_bots():
        entries = [p for p in ((bot.advisor or {}).get("providers") or [])
                   if isinstance(p, dict)]
        if bot.api:
            entries.append(bot.api)
        for e in entries:
            prov = llm_api.PROVIDER_BY_ID.get(str(e.get("provider") or ""))
            url = str(e.get("base_url") or "") or (prov.base_url if prov else "")
            if _host(url) == host:
                key = llm_api.resolve_key(e)
                if key:
                    return key
    return ""


def resolve(cfg: dict | None = None) -> Resolved:
    """Turn a config dict into everything a call needs, or raise ImageJobError
    with a sentence the operator (or the chat bubble) can act on."""
    cfg = load_config() if cfg is None else cfg
    pid = provider_id(cfg)
    preset = PRESET_BY_ID.get(pid)
    if preset is None:
        raise image_jobs.ImageJobError("no image API provider is selected")
    try:
        base = llm_api.normalize_base_url(cfg.get("base_url") or preset.base_url)
    except llm_api.ApiError as e:
        raise image_jobs.ImageJobError(f"{e.message}") from None
    model = (cfg.get("model") or preset.model).strip()
    if not model:
        raise image_jobs.ImageJobError(f"{preset.label} images need a model name")
    key, source = "", ""
    env_name = (cfg.get("api_key_env") or "").strip()
    if env_name and os.environ.get(env_name, "").strip():
        key, source = os.environ[env_name].strip(), "env"
    elif cfg.get("api_key"):
        key, source = str(cfg["api_key"]).strip(), "stored"
    else:
        shared = shared_chat_key(base)
        if shared:
            key, source = shared, "shared"
    if preset.key_required and not key:
        raise image_jobs.ImageJobError(
            f"{preset.label} images need an API key (Settings → AI models → "
            "Image generation)")
    return Resolved(preset=preset, base_url=base, model=model, api_key=key,
                    key_source=source)


def public_config(cfg: dict | None = None) -> dict:
    """The stored config with the key replaced by facts about it."""
    cfg = load_config() if cfg is None else cfg
    out = {"provider": provider_id(cfg), "base_url": cfg.get("base_url", ""),
           "model": cfg.get("model", ""), "api_key_env": cfg.get("api_key_env", ""),
           "has_key": bool(cfg.get("api_key")), "key_source": ""}
    if api_selected(cfg):
        with contextlib.suppress(image_jobs.ImageJobError):
            out["key_source"] = resolve(cfg).key_source
    return out


def merge_submission(payload: dict, stored: dict | None = None) -> dict:
    """A form submission over the stored config. A blank key keeps the stored
    one (it never comes back out to the form) unless the provider changed."""
    stored = load_config() if stored is None else stored
    pid = str(payload.get("provider") or "").strip().lower()
    if pid not in PRESET_BY_ID and pid not in (AUTO, NONE, CLAWFORGE):
        raise ValueError(f"unknown image provider {pid!r}")
    same = provider_id(stored) == pid
    cfg = {"provider": pid,
           "base_url": str(payload.get("base_url") or "").strip(),
           "model": str(payload.get("model") or "").strip(),
           "api_key": str(payload.get("api_key") or "").strip()
           or (stored.get("api_key", "") if same else ""),
           "api_key_env": str(payload.get("api_key_env") or "").strip()}
    if cfg["api_key_env"] and not env_name_ok(cfg["api_key_env"]):
        raise ValueError("api_key_env must be an environment variable name "
                         "such as MINIMAX_API_KEY")
    if pid not in PRESET_BY_ID:
        cfg.update(base_url="", model="", api_key="", api_key_env="")
    return cfg


def env_name_ok(name: str) -> bool:
    """A credential-shaped variable name, never one of DisPatch's own."""
    return bool(_ENV_NAME_RE.fullmatch(name)) and not name.startswith("DISPATCH_")


_ENV_NAME_RE = re.compile(r"[A-Z][A-Z0-9_]{2,63}")


# --------------------------------------------------------------------------- #
# One generation
# --------------------------------------------------------------------------- #


def _client(timeout: float = GENERATE_TIMEOUT_S) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=transport, follow_redirects=False,
        timeout=httpx.Timeout(timeout, connect=CONNECT_TIMEOUT_S))


def openai_size(ratio: str, width: int | None, height: int | None,
                model: str) -> str:
    """OpenAI's size vocabulary is three shapes; pick the nearest."""
    w, h = 1, 1
    if width and height:
        w, h = width, height
    elif ratio and ":" in ratio:
        a, b = ratio.split(":", 1)
        with contextlib.suppress(ValueError):
            w, h = int(a), int(b)
    wide = "1792x1024" if model.startswith("dall-e-3") else "1536x1024"
    tall = "1024x1792" if model.startswith("dall-e-3") else "1024x1536"
    if w * 10 > h * 11:
        return wide
    if h * 10 > w * 11:
        return tall
    return "1024x1024"


def minimax_ratio(ratio: str) -> str:
    if ratio in MINIMAX_RATIOS:
        return ratio
    try:
        a, b = (int(x) for x in ratio.split(":", 1))
        want = a / b
    except (ValueError, ZeroDivisionError):
        return "1:1"
    return min(MINIMAX_RATIOS,
               key=lambda r: abs(int(r.split(":")[0]) / int(r.split(":")[1]) - want))


def _error_from_status(r: httpx.Response, label: str) -> image_jobs.ImageJobError:
    msg = ""
    with contextlib.suppress(Exception):
        body = r.json()
        err = body.get("error") if isinstance(body, dict) else None
        if isinstance(err, dict):
            msg = str(err.get("message") or "")
        elif isinstance(err, str):
            msg = err
        if not msg and isinstance(body, dict):
            msg = str((body.get("base_resp") or {}).get("status_msg") or "")
    if r.status_code in (401, 403):
        return image_jobs.ImageJobError(
            f"{label} rejected the API key (HTTP {r.status_code})")
    if r.status_code == 429:
        return image_jobs.ImageJobError(
            f"{label} is rate-limiting or out of quota (HTTP 429)"
            + (f": {msg}" if msg else ""))
    return image_jobs.ImageJobError(
        f"{label} returned HTTP {r.status_code}" + (f": {msg}" if msg else ""))


async def _download(client: httpx.AsyncClient, url: str) -> bytes:
    if urlparse(url).scheme not in ("http", "https"):
        raise image_jobs.ImageJobError("the provider named a non-web image URL")
    chunks: list[bytes] = []
    total = 0
    async with client.stream("GET", url) as resp:
        if resp.status_code >= 400:
            raise image_jobs.ImageJobError(
                f"could not download the picture (HTTP {resp.status_code})")
        async for chunk in resp.aiter_bytes():
            total += len(chunk)
            if total > image_jobs.FETCH_MAX_BYTES:
                raise image_jobs.ImageJobError("image is implausibly large")
            chunks.append(chunk)
    return b"".join(chunks)


def _b64(s: str) -> bytes:
    if s.startswith("data:") and "," in s:
        s = s.split(",", 1)[1]
    try:
        return base64.b64decode(s, validate=False)
    except (ValueError, TypeError):
        raise image_jobs.ImageJobError("the provider sent unreadable image data") from None


async def generate(res: Resolved, spec: image_jobs.ImageSpec) -> bytes:
    """One picture's bytes, validated. Raises ImageJobError."""
    return (await generate_batch(res, spec))[0]


#: The most pictures one request may ask for, by wire shape. MiniMax documents
#: 1-9; OpenAI-shape hosts differ per model, so the Studio keeps to a modest 4
#: (and 1 for dall-e-3, which refuses more).
MAX_BATCH = {"minimax": 9, "openai-images": 4}


def max_batch(res: Resolved) -> int:
    if res.preset.shape == "openai-images" and res.model.startswith("dall-e-3"):
        return 1
    return MAX_BATCH.get(res.preset.shape, 1)


def supports(res: Resolved) -> dict:
    """What a provider can do beyond a prompt, for UIs that hide the rest."""
    mm = res.preset.shape == "minimax"
    return {"reference": mm, "optimizer": mm, "seed": mm, "max_n": max_batch(res)}


async def generate_batch(res: Resolved, spec: image_jobs.ImageSpec, *, n: int = 1,
                         seed: int | None = None, optimizer: bool = False,
                         reference: str | None = None) -> list[bytes]:
    """``n`` pictures' bytes, validated, in one provider request.

    The same client and error mapping as :func:`generate` (which is the n=1
    case). ``seed``, ``optimizer`` and ``reference`` (a ``data:image/...``
    URL: MiniMax's ``subject_reference`` character mode) apply to the
    ``minimax`` shape only and are ignored elsewhere. Raises ImageJobError.
    """
    label = res.preset.label
    n = max(1, min(int(n), max_batch(res)))
    headers = {"Content-Type": "application/json"}
    if res.api_key:
        headers["Authorization"] = f"Bearer {res.api_key}"
    blobs: list[bytes] = []
    async with _client() as client:
        try:
            if res.preset.shape == "minimax":
                body: dict[str, Any] = {
                    "model": res.model, "prompt": spec.prompt[:MINIMAX_PROMPT_MAX],
                    "aspect_ratio": minimax_ratio(spec.ratio or "1:1"),
                    "response_format": "base64", "n": n,
                    "prompt_optimizer": bool(optimizer)}
                if seed is not None:
                    body["seed"] = int(seed)
                if reference:
                    body["subject_reference"] = [
                        {"type": "character", "image_file": reference}]
                r = await client.post(f"{res.base_url}/image_generation",
                                      json=body, headers=headers)
                if r.status_code >= 400:
                    raise _error_from_status(r, label)
                items = _minimax_items(r.json(), label)
            else:
                body = {"model": res.model, "prompt": spec.prompt, "n": n}
                if res.preset.size_mode == "openai":
                    body["size"] = openai_size(spec.ratio, spec.width, spec.height,
                                               res.model)
                r = await client.post(f"{res.base_url}/images/generations",
                                      json=body, headers=headers)
                if r.status_code >= 400:
                    raise _error_from_status(r, label)
                items = _openai_items(r.json(), label)
            for item in items:
                blobs.append(await _download(client, item) if isinstance(item, str)
                             else item)
        except image_jobs.ImageJobError:
            raise
        except httpx.TimeoutException:
            raise image_jobs.ImageJobError(f"{label} took too long to answer") from None
        except httpx.HTTPError as e:
            raise image_jobs.ImageJobError(
                f"{label} could not be reached ({type(e).__name__})") from None
        except ValueError:
            raise image_jobs.ImageJobError(f"{label} sent a reply that is not JSON") from None
    for data in blobs:
        ok, why = image_jobs.validate_image(data)
        if not ok:
            raise image_jobs.ImageJobError(
                f"{label} returned something that is not an image ({why})")
    return blobs


def _openai_items(payload: Any, label: str) -> list[bytes | str]:
    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list) or not rows:
        raise image_jobs.ImageJobError(f"{label} returned no picture")
    out: list[bytes | str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if row.get("b64_json"):
            out.append(_b64(str(row["b64_json"])))
        elif row.get("url"):
            out.append(str(row["url"]))
    if not out:
        raise image_jobs.ImageJobError(f"{label} returned no picture")
    return out


def _openai_bytes(payload: Any, label: str) -> bytes | str:
    return _openai_items(payload, label)[0]


_MINIMAX_CODES = {
    1002: "MiniMax is rate-limiting this key",
    1004: "MiniMax rejected the API key",
    1008: "the MiniMax account balance is too low",
    1026: "MiniMax refused the prompt (sensitive content)",
    1027: "MiniMax refused the picture it made (sensitive content)",
    2013: "MiniMax rejected the request parameters",
    2049: "MiniMax rejected the API key",
}


def _minimax_items(payload: Any, label: str) -> list[bytes | str]:
    if not isinstance(payload, dict):
        raise image_jobs.ImageJobError(f"{label} returned no picture")
    base = payload.get("base_resp") or {}
    code = base.get("status_code", 0)
    try:
        code = int(code)
    except (TypeError, ValueError):
        code = -1
    if code != 0:
        why = _MINIMAX_CODES.get(code) or f"{label} error {code}"
        msg = str(base.get("status_msg") or "").strip()
        raise image_jobs.ImageJobError(f"{why}" + (f" ({msg})" if msg and code not in _MINIMAX_CODES else ""))
    data = payload.get("data") or {}
    out: list[bytes | str] = []
    b64s = data.get("image_base64") if isinstance(data, dict) else None
    if isinstance(b64s, list):
        out.extend(_b64(str(x)) for x in b64s if x)
    urls = data.get("image_urls") if isinstance(data, dict) else None
    if not out and isinstance(urls, list):
        out.extend(str(u) for u in urls if u)
    if not out:
        raise image_jobs.ImageJobError(f"{label} returned no picture (it may have "
                                       "blocked the prompt)")
    return out


def _minimax_bytes(payload: Any, label: str) -> bytes | str:
    return _minimax_items(payload, label)[0]


# --------------------------------------------------------------------------- #
# The worker-facing backend
# --------------------------------------------------------------------------- #


@dataclass
class _Run:
    task: asyncio.Task | None
    started: float
    data: bytes | None = None
    error: str = ""
    finished: float = 0.0


class ApiImages:
    """The image-job backend for an API provider. Same calls as the MCP client."""

    def __init__(self) -> None:
        self._runs: dict[str, _Run] = {}
        self._last_error = ""
        self._last_ok = 0.0
        self.generations = 0

    # -- the worker's calls ------------------------------------------------ #

    async def readiness(self, **_: Any) -> image_jobs.RigReadiness:
        # An API is either there or it says why on the call itself.
        return image_jobs.RigReadiness(checked_at=time.monotonic())

    async def enqueue(self, spec: image_jobs.ImageSpec, *, callback_url: str = "",
                      callback_token: str = "") -> image_jobs.EnqueueResult:
        res = resolve()            # a config mistake fails the job, visibly
        self._gc()
        rid = f"api-{uuid.uuid4().hex[:16]}"
        run = _Run(task=None, started=time.monotonic())
        self._runs[rid] = run
        run.task = asyncio.ensure_future(self._run(rid, res, spec))
        return image_jobs.EnqueueResult(job_id=rid)

    async def _run(self, rid: str, res: Resolved, spec: image_jobs.ImageSpec) -> None:
        run = self._runs.get(rid)
        try:
            self.generations += 1
            data = await generate(res, spec)
            if run is not None:
                run.data = data
            self._last_ok, self._last_error = time.monotonic(), ""
        except asyncio.CancelledError:
            if run is not None:
                run.error = "cancelled"
            raise
        except image_jobs.ImageJobError as e:
            if run is not None:
                run.error = e.message
            self._last_error = e.message
            log.info("image API generation failed: %s", e.message)
        except Exception as e:  # never let a task die silently
            log.exception("image API generation crashed")
            if run is not None:
                run.error = f"internal error ({type(e).__name__})"
        finally:
            if run is not None:
                run.finished = time.monotonic()

    async def poll(self, job_id: str) -> image_jobs.PollResult:
        run = self._runs.get(job_id)
        if run is None:
            raise image_jobs.ImageJobError(
                "DisPatch restarted and lost this picture", code="job_lost")
        if not run.finished:
            return image_jobs.PollResult(state="running", done=False)
        if run.data is not None:
            return image_jobs.PollResult(state="done", done=True, files_rel=job_id)
        if run.error == "cancelled":
            return image_jobs.PollResult(state=image_jobs.CANCELLED, done=True,
                                         error="cancelled")
        return image_jobs.PollResult(state="failed", done=True,
                                     error=run.error or "the provider returned no picture")

    async def fetch(self, files_rel: str) -> bytes:
        run = self._runs.pop(files_rel, None)
        if run is None or run.data is None:
            raise image_jobs.ImageJobError("the picture is no longer held")
        return run.data

    async def cancel(self, job_id: str) -> dict:
        run = self._runs.get(job_id)
        if run is not None and run.task is not None and not run.task.done():
            run.task.cancel()
        return {"cancelled": run is not None}

    async def comfy_restart(self, **_: Any) -> tuple[bool, str]:
        return False, "not applicable to an API provider"

    def status(self) -> dict:
        cfg = load_config()
        return {"provider": provider_id(cfg) if api_selected(cfg) else "",
                "in_flight": sum(1 for r in self._runs.values() if not r.finished),
                "last_error": self._last_error,
                "last_ok_age_s": (int(time.monotonic() - self._last_ok)
                                  if self._last_ok else None)}

    def _gc(self) -> None:
        now = time.monotonic()
        for rid, run in list(self._runs.items()):
            if run.finished and now - run.finished > RESULT_TTL_S:
                self._runs.pop(rid, None)

    async def aclose(self) -> None:
        for run in self._runs.values():
            if run.task is not None and not run.task.done():
                run.task.cancel()
        self._runs.clear()


backend = ApiImages()
