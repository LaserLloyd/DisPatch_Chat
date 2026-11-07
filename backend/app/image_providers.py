"""Image generation through a standard API: OpenAI-shape, MiniMax and Civitai.

WHY THIS EXISTS
---------------
``image_jobs`` drives an MCP image server (a ComfyUI-style rig). Most people
who install DisPatch do not have one; they have an API key. This module is the
second backend for the same worker: it answers the five calls the worker makes
on the MCP client (``readiness``, ``enqueue``, ``poll``, ``fetch``, ``cancel``,
plus ``status`` for /api/health), so the placeholder → picture rewrite, the
deadline, coalescing, rate limits and the image hard-line guard all apply unchanged.
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
``civitai``        ASYNC. ``POST {base}/v2/consumer/workflows?wait=55`` with
                   one ``imageGen`` step, then ``GET .../workflows/{id}?wait=30``
                   until a terminal status; the pictures are signed blob URLs in
                   ``steps[0].output.images[]``. Billed in Buzz. Civitai is
                   STUDIO-ONLY: it is never the saved default provider, so
                   ``[[pic:]]``, ``/api/image-jobs``, the provider Test and
                   Quick setup can never reach it (``DEFAULT_PRESETS``,
                   ``resolve(studio=False)``).

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
from urllib.parse import parse_qs, urlparse

import httpx
import yaml

from . import config, image_jobs, llm_api, secret_store

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
#: Per-shape prompt caps; any other shape takes image_jobs.MAX_API_PROMPT_CHARS.
PROMPT_MAX_BY_SHAPE = {"minimax": MINIMAX_PROMPT_MAX}

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
    #: Which access-matrix column governs this preset: "standard" | "civitai".
    access: str = "standard"
    #: Usable from Image Studio only -- never the saved default provider.
    studio_only: bool = False

    def public(self) -> dict:
        return {"id": self.id, "label": self.label, "shape": self.shape,
                "base_url": self.base_url, "model": self.model,
                "key_env": self.key_env, "docs": self.docs,
                "editable_base_url": self.editable_base_url,
                "key_required": self.key_required, "price_note": self.price_note,
                "access": self.access, "studio_only": self.studio_only}


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
        # xAI takes `aspect_ratio` (not `size`); its list (1:1 3:4 4:3 9:16 16:9 2:3 3:2 21:9
        # 9:19.5 19.5:9 9:20 20:9 1:2 2:1 5:2 auto, checked 2026-11-07) holds every ratio
        # minimax_ratio() can return, so the same snapping serves both.
        size_mode="aspect",
        price_note="Billed per image; Grok Imagine 2.0 costs about twice as much as Grok Imagine."),
    ImagePreset(
        id="custom", label="Custom (OpenAI-compatible)", shape="openai-images",
        editable_base_url=True, key_required=False, size_mode="openai",
        # No price note: a custom host is often a free local server, and Image
        # Studio reads any note as "this provider is metered".
        price_note=""),
    ImagePreset(
        id="civitai", label="Civitai", shape="civitai",
        base_url="https://orchestration.civitai.com",
        model="urn:air:sdxl:checkpoint:civitai:133005@1759168",   # Juggernaut XL
        key_env="CIVITAI_API_KEY", docs="https://civitai.com/user/account",
        price_note="Billed in Buzz; the estimate below is Civitai's own.",
        access="civitai", studio_only=True),
)
PRESET_BY_ID = {p.id: p for p in PRESETS}
#: What may be the SAVED default provider (the ``[[pic:]]`` path). Studio-only
#: presets are not here, so they are unrepresentable as a default.
DEFAULT_PRESETS: tuple[ImagePreset, ...] = tuple(p for p in PRESETS if not p.studio_only)
_DEFAULT_IDS = frozenset(p.id for p in DEFAULT_PRESETS)

#: Setting that holds the Civitai key (write-only secret; env CIVITAI_API_KEY wins).
CIVITAI_KEY_REF = "setting.connections.image_civitai_key"

# Civitai wire constants (see _civitai_batch).
CIVITAI_SUBMIT_WAIT_S = 55
CIVITAI_POLL_WAIT_S = 30
CIVITAI_DEADLINE_S = 300      # a less common checkpoint can wait minutes in Civitai's queue
CIVITAI_MIN_POLL_GAP_S = 2.0
CIVITAI_CANCEL_TIMEOUT_S = 10.0
CIVITAI_MAX_N = 4
CIVITAI_STEPS = 25
CIVITAI_CFG = 7
CIVITAI_NEGATIVE = "worst quality, low quality, blurry"
#: The recipe page says "model"; the OpenAPI schema says "checkpointModel".
#: ONE constant so a live probe (scripts/civitai_smoke.py) can flip it.
CIVITAI_MODEL_FIELD = "model"
CIVITAI_SIZES: tuple[tuple[int, int], ...] = (
    (1024, 1024), (1152, 896), (896, 1152), (1216, 832), (832, 1216),
    (1344, 768), (768, 1344), (1536, 640), (640, 1536))
#: A workflow id goes into a URL path that carries the key, so only plain id characters.
_WORKFLOW_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,128}")
_AIR_RE = re.compile(r"urn:air:sdxl:checkpoint:civitai:\d+@\d+")
_CIVITAI_HOSTS = frozenset({"civitai.com", "www.civitai.com",
                            "civitai.red", "www.civitai.red"})
CIVITAI_SITE_API = "https://civitai.com/api/v1"
#: Fixed host for the Buzz balance (tRPC); no override, so the key goes nowhere else.
CIVITAI_TRPC_BALANCE = "https://civitai.com/api/trpc/buzz.getBuzzAccount"
CIVITAI_SITE_URL = "https://civitai.com"
#: Which Buzz pays for a picture. "" (not in this tuple) = let Civitai choose.
CIVITAI_CURRENCIES = ("blue", "yellow")

#: Test seams for the polling loop.
_sleep = asyncio.sleep
_monotonic = time.monotonic

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
        with os.fdopen(fd, "w", encoding="utf-8") as f:
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
    return provider_id(cfg) in _DEFAULT_IDS


def choose_backend(workflow: str, *, clawforge: bool,
                   cfg: dict | None = None) -> str:
    """Which backend a NEW job uses: "clawforge", "api", or "" (none).

    A bot naming a workflow keeps the MCP image server whenever one is
    configured. Everything else follows the default provider; automatic (no
    image.yaml) is the MCP server if configured -- the pre-2.3.7 behaviour.
    """
    pid = provider_id(cfg)
    if pid in _DEFAULT_IDS:
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


def _origin(url: str) -> tuple[str, str, int | None] | None:
    """(scheme, host, port) -- the tuple a borrowed key is scoped to, the
    same one llm_api scopes a chat credential to on redirects. None for a
    URL with no host (nothing can match it)."""
    try:
        scheme, host, port = llm_api._origin_of(url)
    except (ValueError, TypeError):
        return None
    if scheme not in ("http", "https") or not host:
        return None
    return scheme, host, port


def shared_chat_key(base_url: str) -> str:
    """The key of a chat connection to the same origin, or "".

    "Same origin" -- scheme, host AND port -- is the whole rule: a key is only
    ever sent where its owner already sends it. Matching the hostname alone
    let `http://api.minimax.io/v1` (plaintext) or `https://api.minimax.io:8443`
    borrow the MiniMax chat key. Direct-API bots (advisor.providers) and
    legacy api: bots both count."""
    want = _origin(base_url)
    if want is None:
        return ""
    for bot in config.load_bots():
        entries = [p for p in ((bot.advisor or {}).get("providers") or [])
                   if isinstance(p, dict)]
        if bot.api:
            entries.append(bot.api)
        for e in entries:
            prov = llm_api.PROVIDER_BY_ID.get(str(e.get("provider") or ""))
            url = str(e.get("base_url") or "") or (prov.base_url if prov else "")
            if _origin(url) == want:
                key = llm_api.resolve_key(e)
                if key:
                    return key
    return ""


def resolve(cfg: dict | None = None, *, studio: bool = False) -> Resolved:
    """Turn a config dict into everything a call needs, or raise ImageJobError
    with a sentence the operator (or the chat bubble) can act on.

    ``studio=True`` is Image Studio only; it is the one caller allowed to
    resolve a studio-only preset (Civitai)."""
    cfg = load_config() if cfg is None else cfg
    pid = provider_id(cfg)
    preset = PRESET_BY_ID.get(pid)
    if preset is None:
        raise image_jobs.ImageJobError("no image API provider is selected")
    if preset.studio_only and not studio:
        raise image_jobs.ImageJobError(
            f"{preset.label} is used from Image Studio only")
    if preset.access == "civitai":
        # Own key, own host: never image.yaml's api_key, never a chat key, and
        # no base_url override (the key goes to Civitai only: the orchestration
        # host and, for the Buzz balance, civitai.com).
        key, source = os.environ.get(preset.key_env, "").strip(), "env"
        if not key:
            with contextlib.suppress(Exception):
                key, source = (secret_store.get(CIVITAI_KEY_REF) or "").strip(), "stored"
        if not key:
            raise image_jobs.ImageJobError(
                "Civitai needs an API key (Settings → AI & Connections → Image generation)")
        model = (cfg.get("model") or preset.model).strip() or preset.model
        return Resolved(preset=preset, base_url=preset.base_url, model=model,
                        api_key=key, key_source=source)
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
            f"{preset.label} images need an API key (Settings → AI & Connections → "
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
    if PRESET_BY_ID.get(pid) is not None and PRESET_BY_ID[pid].studio_only:
        raise ValueError("Civitai is used from Image Studio only; it cannot be "
                         "the default picture maker")
    if pid not in _DEFAULT_IDS and pid not in (AUTO, NONE, CLAWFORGE):
        raise ValueError(f"unknown image provider {pid!r}")
    same = provider_id(stored) == pid
    cfg = {"provider": pid,
           "base_url": str(payload.get("base_url") or "").strip(),
           "model": str(payload.get("model") or "").strip(),
           "api_key": str(payload.get("api_key") or "").strip()
           or (stored.get("api_key", "") if same else ""),
           "api_key_env": str(payload.get("api_key_env") or "").strip()}
    if cfg["api_key_env"] and not env_name_ok(cfg["api_key_env"]):
        raise ValueError("api_key_env must be the name of a credential variable "
                         "such as MINIMAX_API_KEY (ending in _KEY, _TOKEN, "
                         "_SECRET, _PASSWORD or _CREDENTIALS)")
    if pid not in _DEFAULT_IDS:
        cfg.update(base_url="", model="", api_key="", api_key_env="")
    return cfg


def require_key_rules(principal, payload: dict, cfg: dict, stored: dict) -> None:
    """The 4.3 #31 rules for the image provider form (``PUT /api/llm/image`` and its Test):
    (1) naming a server environment variable that the stored config does not already name for
    the same provider and server needs the protected Admin (``env_key_owner_only``); (2) a form
    that sends no key of its own but points at ANOTHER server would carry the stored key (or the
    variable's value) there, so it needs the protected Admin too (``permission_denied``). A key
    typed into the form may go wherever the form says. Raises ``connections.ConnectionError_``
    (403). Every Admin keeps re-saving the same form (a model change, the same address)."""
    from . import connections
    preset = PRESET_BY_ID.get(cfg.get("provider") or "")
    if preset is None or connections._protected(principal):
        return
    old_preset = PRESET_BY_ID.get(provider_id(stored))
    old_base = (str(stored.get("base_url") or "") or old_preset.base_url) if old_preset else ""
    new_base = cfg.get("base_url") or preset.base_url
    here = old_preset is preset and connections.same_server(old_base, new_base)
    connections.require_env_binding(principal, str(stored.get("api_key_env") or "") if here else "",
                                    cfg.get("api_key_env") or "")
    typed = bool(str(payload.get("api_key") or "").strip())
    carried = bool(cfg.get("api_key") or cfg.get("api_key_env"))
    if carried and not typed and not here:
        raise connections.ConnectionError_("permission_denied", connections.KEY_MOVE_OWNER_ONLY, 403)


def env_name_ok(name: str) -> bool:
    """A credential-shaped variable name, never one of DisPatch's own.

    The value of the named variable is sent as a bearer token to whatever
    base URL the form names, so only a name that plausibly holds a credential
    is accepted (the same suffix rule the chat Test applies): `MINIMAX_API_KEY`
    yes, `PATH` or `HOME` no."""
    return (bool(_ENV_NAME_RE.fullmatch(name)) and not name.startswith("DISPATCH_")
            and bool(_CREDENTIAL_SUFFIX_RE.search(name)))


_ENV_NAME_RE = re.compile(r"[A-Z][A-Z0-9_]{2,63}")
_CREDENTIAL_SUFFIX_RE = re.compile(r"(?:_KEY|_TOKEN|_SECRET|_PASSWORD|_CREDENTIALS?)$")


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


def prompt_max(shape: str) -> int:
    """The longest prompt a provider of ``shape`` takes."""
    return PROMPT_MAX_BY_SHAPE.get(shape, image_jobs.MAX_API_PROMPT_CHARS)


def _error_from_status(r: httpx.Response, label: str) -> image_jobs.ImageJobError:
    msg = ""
    with contextlib.suppress(Exception):
        body = r.json()
        code = body.get("code") if isinstance(body, dict) else None
        if isinstance(code, str) and "moderat" in code.lower():
            # xAI: {"code": "imagine:content-moderated", ...} -- its own filter
            # refused the finished picture. The attempt is billed all the same.
            return image_jobs.ImageJobError(
                f"{label}'s own content moderation refused this picture "
                "(the provider still bills the attempt). Rewording rarely helps; "
                "a provider without that filter, such as Civitai, may make it.")
        err = body.get("error") if isinstance(body, dict) else None
        if isinstance(err, dict):
            msg = str(err.get("message") or "")
        elif isinstance(err, str):
            msg = err
        if not msg and isinstance(body, dict):
            msg = str((body.get("base_resp") or {}).get("status_msg") or "")
        if not msg and isinstance(body, dict):      # problem+json (Civitai)
            msg = str(body.get("detail") or body.get("title") or "")
    if r.status_code in (401, 403):
        return image_jobs.ImageJobError(
            f"{label} rejected the API key (HTTP {r.status_code})")
    if r.status_code == 429:
        return image_jobs.ImageJobError(
            f"{label} is rate-limiting or out of quota (HTTP 429)"
            + (f": {msg}" if msg else ""))
    # Plain words for people: no "HTTP 404". A custom host is just "the picture server".
    who = "The picture server" if label.startswith("Custom") else label
    if not msg:
        msg = ("not found" if r.status_code == 404
               else "it had a problem on its side" if r.status_code >= 500
               else f"error {r.status_code}")
    return image_jobs.ImageJobError(f"{who} answered: {msg}")


DOWNLOAD_MAX_REDIRECTS = 3


async def _download(client: httpx.AsyncClient, url: str) -> bytes:
    """The picture's bytes. Redirects are followed by hand (the client never
    follows them): at most DOWNLOAD_MAX_REDIRECTS hops, relative Locations
    resolve against the current URL, and every hop must be https. Civitai's
    picture links answer 301 to a signed link on the same host; an unfollowed
    3xx used to be read as an empty picture. No hop carries a key."""
    if urlparse(url).scheme not in ("http", "https"):
        raise image_jobs.ImageJobError("the provider named a non-web image URL")
    for _hop in range(DOWNLOAD_MAX_REDIRECTS + 1):
        chunks: list[bytes] = []
        total = 0
        async with client.stream("GET", url) as resp:
            if resp.is_redirect:
                loc = resp.headers.get("location", "")
                if not loc:
                    raise image_jobs.ImageJobError(
                        f"could not download the picture (HTTP {resp.status_code} "
                        "without a location)")
                url = str(resp.url.join(loc))
                if urlparse(url).scheme != "https":
                    raise image_jobs.ImageJobError(
                        "the picture link redirected to a non-https URL")
                continue
            if resp.status_code >= 400:
                raise image_jobs.ImageJobError(
                    f"could not download the picture (HTTP {resp.status_code})",
                    code="blob_gone" if resp.status_code in (403, 404, 410) else "")
            if resp.status_code != 200:
                raise image_jobs.ImageJobError(
                    f"could not download the picture (HTTP {resp.status_code})")
            async for chunk in resp.aiter_bytes():
                total += len(chunk)
                if total > image_jobs.FETCH_MAX_BYTES:
                    raise image_jobs.ImageJobError("image is implausibly large")
                chunks.append(chunk)
        return b"".join(chunks)
    raise image_jobs.ImageJobError("the picture link redirected too many times")


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
MAX_BATCH = {"minimax": 9, "openai-images": 4, "civitai": CIVITAI_MAX_N}


def max_batch(res: Resolved) -> int:
    if res.preset.shape == "openai-images" and res.model.startswith("dall-e-3"):
        return 1
    return MAX_BATCH.get(res.preset.shape, 1)


def supports(res: Resolved) -> dict:
    """What a provider can do beyond a prompt, for UIs that hide the rest."""
    mm = res.preset.shape == "minimax"
    return {"reference": mm, "optimizer": mm,
            "seed": mm or res.preset.shape == "civitai", "max_n": max_batch(res)}


async def generate_batch(res: Resolved, spec: image_jobs.ImageSpec, *, n: int = 1,
                         seed: int | None = None, optimizer: bool = False,
                         reference: str | None = None,
                         currency: str | None = None) -> list[bytes]:
    """``n`` pictures' bytes, validated, in one provider request.

    The same client and error mapping as :func:`generate` (which is the n=1
    case). ``seed``, ``optimizer`` and ``reference`` (a ``data:image/...``
    URL: MiniMax's ``subject_reference`` character mode) apply to the
    ``minimax`` shape only and are ignored elsewhere (``seed`` also applies to
    ``civitai``, and so does ``currency``: "blue" or "yellow" Buzz, None = Civitai
    chooses). Raises ImageJobError.
    """
    label = res.preset.label
    n = max(1, min(int(n), max_batch(res)))
    headers = {"Content-Type": "application/json"}
    if res.api_key:
        headers["Authorization"] = f"Bearer {res.api_key}"
    blobs: list[bytes] = []
    async with _client() as client:
        try:
            if res.preset.shape == "civitai":
                blobs = await _civitai_batch(client, res, spec, n, seed, currency)
                items = []
            elif res.preset.shape == "minimax":
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
                elif res.preset.size_mode == "aspect":
                    body["aspect_ratio"] = minimax_ratio(spec.ratio or "1:1")
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


# --------------------------------------------------------------------------- #
# Civitai (async workflow; studio-only)
# --------------------------------------------------------------------------- #


def civitai_size(ratio: str) -> tuple[int, int]:
    """The recipe size whose shape is nearest the requested ``a:b`` ratio."""
    want = 1.0
    try:
        a, b = (int(x) for x in (ratio or "1:1").split(":", 1))
        want = a / b
    except (ValueError, ZeroDivisionError):
        pass
    return min(CIVITAI_SIZES, key=lambda wh: abs(wh[0] / wh[1] - want))


def civitai_body(res: Resolved, spec: image_jobs.ImageSpec, n: int,
                 seed: int | None, *, external_id: str | None,
                 currency: str | None = None) -> dict:
    """The SubmitWorkflow body. ``external_id`` is Civitai's idempotency key;
    the estimate passes None (an estimate's id would make the real submit
    return the dry run). ``currency`` ("blue" | "yellow") picks which Buzz pays;
    None leaves it to Civitai."""
    if currency is not None and currency not in CIVITAI_CURRENCIES:
        raise ValueError("currency must be blue or yellow")
    ratio = spec.ratio
    if not ratio and spec.width and spec.height:
        ratio = f"{spec.width}:{spec.height}"
    w, h = civitai_size(ratio or "1:1")
    inp: dict[str, Any] = {
        "engine": "sdcpp", "ecosystem": "sdxl", "operation": "createImage",
        CIVITAI_MODEL_FIELD: res.model, "prompt": spec.prompt,
        "negativePrompt": CIVITAI_NEGATIVE, "width": w, "height": h,
        "cfgScale": CIVITAI_CFG, "steps": CIVITAI_STEPS,
        "quantity": max(1, min(int(n), CIVITAI_MAX_N))}
    if seed is not None:
        inp["seed"] = int(seed)
    body: dict[str, Any] = {"steps": [{"$type": "imageGen", "input": inp}],
                            "allowMatureContent": True}
    if currency:
        body["currencies"] = [currency]
    if external_id:
        body["externalId"] = external_id
    return body


def _civitai_headers(res: Resolved) -> dict:
    return {"Content-Type": "application/json",
            "Authorization": f"Bearer {res.api_key}"}


def _civitai_reason(wf: dict) -> str:
    """Plain words for a failed / expired / canceled workflow."""
    status = str(wf.get("status") or "").lower()
    bits: list[str] = []
    blocked = False
    for obj in [wf, *[s for s in (wf.get("steps") or []) if isinstance(s, dict)]]:
        # A present blockedReason is the moderation verdict whatever its value says
        # ("sexual-minor", a code...); a free-text reason that mentions a block counts too.
        blocked = blocked or bool(obj.get("blockedReason"))
        for k in ("reason", "blockedReason", "error", "detail", "message"):
            v = obj.get(k)
            if v:
                bits.append(str(v))
    text = "; ".join(dict.fromkeys(bits))
    if blocked or "block" in text.lower():
        return "Civitai refused this prompt (its moderation)"
    if status == "expired":
        return "Civitai gave up on the picture (expired)"
    if status == "canceled":
        return "Civitai cancelled the picture (the account may be out of Buzz)"
    return "Civitai could not make the picture" + (f": {text[:200]}" if text else "")


def _civitai_images(wf: dict) -> list[dict]:
    steps = wf.get("steps")
    step = steps[0] if isinstance(steps, list) and steps and isinstance(steps[0], dict) else {}
    out = step.get("output")
    imgs = out.get("images") if isinstance(out, dict) else None
    rows = [i for i in imgs if isinstance(i, dict)] if isinstance(imgs, list) else []
    if not rows:
        raise image_jobs.ImageJobError("Civitai returned no picture")
    # Civitai moderates per picture: one blocked picture in a batch of four must not
    # throw away the three the Buzz already paid for. Only an all-blocked batch is a refusal.
    kept = [i for i in rows if not i.get("blockedReason")]
    if not kept:
        raise image_jobs.ImageJobError("Civitai refused this prompt (its moderation)")
    return kept


async def _civitai_post(client: httpx.AsyncClient, url: str, body: dict,
                        headers: dict, label: str) -> httpx.Response:
    """POST; one re-POST of the SAME body (same externalId, so no second
    charge) on a transport error."""
    for attempt in (0, 1):
        try:
            return await client.post(url, json=body, headers=headers)
        except httpx.TransportError:
            if attempt:
                raise image_jobs.ImageJobError(
                    f"{label} could not be reached") from None
    raise AssertionError("unreachable")


async def _civitai_batch(client: httpx.AsyncClient, res: Resolved,
                         spec: image_jobs.ImageSpec, n: int,
                         seed: int | None, currency: str | None = None) -> list[bytes]:
    label = res.preset.label
    headers = _civitai_headers(res)
    base = res.base_url
    body = civitai_body(res, spec, n, seed, external_id="dp-" + uuid.uuid4().hex,
                        currency=currency)
    started = _monotonic()
    r = await _civitai_post(
        client, f"{base}/v2/consumer/workflows?wait={CIVITAI_SUBMIT_WAIT_S}",
        body, headers, label)
    if r.status_code >= 400:
        raise _error_from_status(r, label)
    wf = r.json()
    if not isinstance(wf, dict):
        raise image_jobs.ImageJobError(f"{label} sent a reply that is not JSON")
    wid = str(wf.get("id") or wf.get("workflowId") or "")
    if wid and not _WORKFLOW_ID_RE.fullmatch(wid):
        raise image_jobs.ImageJobError(f"{label} sent a workflow id that is not valid")
    wf_url = f"{base}/v2/consumer/workflows/{wid}"
    last_req = _monotonic()
    terminal = ("succeeded", "failed", "expired", "canceled")
    try:
        while str(wf.get("status") or "").lower() not in terminal:
            if not wid:
                raise image_jobs.ImageJobError(f"{label} returned no picture")
            gap = CIVITAI_MIN_POLL_GAP_S - (_monotonic() - last_req)
            await _sleep(max(0.0, gap))
            if _monotonic() - started >= CIVITAI_DEADLINE_S:
                raise image_jobs.ImageJobError(
                    f"{label} took too long; the request was cancelled")
            last_req = _monotonic()
            try:
                g = await client.get(f"{wf_url}?wait={CIVITAI_POLL_WAIT_S}", headers=headers)
            except httpx.TransportError:
                continue                       # the deadline still bounds the loop
            if g.status_code >= 400:
                raise _error_from_status(g, label)
            wf = g.json()
            if not isinstance(wf, dict):
                raise image_jobs.ImageJobError(f"{label} sent a reply that is not JSON")
    except BaseException:
        # Giving up for ANY reason (deadline, a poll error, the request being cancelled)
        # before a terminal status: ask Civitai to stop, so no Buzz is spent on a picture
        # nobody will receive. Best effort and bounded; the original error still raises.
        if wid:
            with contextlib.suppress(BaseException):
                await asyncio.wait_for(
                    client.put(wf_url, json={"status": "canceled"}, headers=headers),
                    CIVITAI_CANCEL_TIMEOUT_S)
        raise
    if str(wf.get("status")).lower() != "succeeded":
        raise image_jobs.ImageJobError(_civitai_reason(wf))
    rows = _civitai_images(wf)
    blobs: list[bytes] = []
    refreshed: list[dict] | None = None
    for idx, row in enumerate(rows):
        url = str(row.get("url") or "")
        if not row.get("available", True) or not url:
            raise image_jobs.ImageJobError("Civitai did not provide the picture")
        for attempt in (0, 1):
            if urlparse(url).scheme != "https":
                raise image_jobs.ImageJobError("the provider named a non-https image URL")
            try:
                blobs.append(await _download(client, url))
                break
            except image_jobs.ImageJobError as e:
                if e.code != "blob_gone" or attempt:
                    if e.code == "blob_gone":
                        raise image_jobs.ImageJobError(
                            "Civitai's picture link expired") from None
                    raise
                if refreshed is None:
                    g = await client.get(wf_url, headers=headers)
                    if g.status_code >= 400:
                        raise image_jobs.ImageJobError(
                            "Civitai's picture link expired") from None
                    refreshed = _civitai_images(g.json())
                url = str((refreshed[idx] if idx < len(refreshed) else {}).get("url") or "")
                if not url:
                    raise image_jobs.ImageJobError(
                        "Civitai's picture link expired") from None
    return blobs


async def estimate_civitai(res: Resolved, spec: image_jobs.ImageSpec, *,
                           n: int = 1, currency: str | None = None) -> int:
    """Civitai's own Buzz estimate (``whatif``). Never sends ``externalId``,
    never polls, downloads or stores. Raises ImageJobError."""
    label = res.preset.label
    body = civitai_body(res, spec, n, None, external_id=None, currency=currency)
    try:
        async with _client() as client:
            r = await client.post(f"{res.base_url}/v2/consumer/workflows?whatif=true",
                                  json=body, headers=_civitai_headers(res))
            if r.status_code >= 400:
                raise _error_from_status(r, label)
            doc = r.json()
    except image_jobs.ImageJobError:
        raise
    except httpx.TimeoutException:
        raise image_jobs.ImageJobError(f"{label} took too long to answer") from None
    except httpx.HTTPError as e:
        raise image_jobs.ImageJobError(
            f"{label} could not be reached ({type(e).__name__})") from None
    except ValueError:
        raise image_jobs.ImageJobError(f"{label} sent a reply that is not JSON") from None
    cost = doc.get("cost") if isinstance(doc, dict) else None
    total = cost.get("total") if isinstance(cost, dict) else None
    if isinstance(total, bool) or not isinstance(total, (int, float)):
        raise image_jobs.ImageJobError(f"{label} gave no price estimate")
    return int(round(total))


_CIVITAI_ASK = "Paste a Civitai checkpoint link or AIR (urn:air:sdxl:checkpoint:…)"


async def civitai_model(text: str) -> str:
    """An SDXL checkpoint AIR from an AIR, a version id, or a civitai.com /
    civitai.red model link. May call Civitai's public site API (no key).
    Raises ValueError."""
    t = (text or "").strip()
    if _AIR_RE.fullmatch(t):
        return t
    version = ""
    model_id = ""
    if re.fullmatch(r"\d{1,10}", t):
        version = t
    else:
        u = urlparse(t)
        m = re.fullmatch(r"/models/(\d{1,10})(?:/[^/]*)?/?", u.path or "")
        if u.scheme not in ("http", "https") or (u.hostname or "").lower() not in _CIVITAI_HOSTS or not m:
            raise ValueError(_CIVITAI_ASK)
        model_id = m.group(1)
        v = (parse_qs(u.query).get("modelVersionId") or [""])[0]
        if v:
            if not re.fullmatch(r"\d{1,10}", v):
                raise ValueError(_CIVITAI_ASK)
            version = v
    try:
        async with _client(30.0) as client:
            if not version:
                r = await client.get(f"{CIVITAI_SITE_API}/models/{model_id}")
                if r.status_code >= 400:
                    raise ValueError(_CIVITAI_ASK)
                vers = r.json().get("modelVersions")
                vid = vers[0].get("id") if isinstance(vers, list) and vers else None
                if not isinstance(vid, int):
                    raise ValueError(_CIVITAI_ASK)
                version = str(vid)
            r = await client.get(f"{CIVITAI_SITE_API}/model-versions/{version}")
            if r.status_code >= 400:
                raise ValueError(_CIVITAI_ASK)
            air = str((r.json() or {}).get("air") or "")
    except httpx.HTTPError:
        raise ValueError("Could not reach Civitai to look that model up") from None
    except (AttributeError, KeyError, IndexError, TypeError):
        raise ValueError(_CIVITAI_ASK) from None
    if not _AIR_RE.fullmatch(air):
        raise ValueError(_CIVITAI_ASK)
    return air


# --------------------------------------------------------------------------- #
# Civitai: Buzz balance and the model list
# --------------------------------------------------------------------------- #

_BUZZ_TYPE_RE = re.compile(r"[a-z]{1,16}")


def parse_balances(doc: Any) -> dict[str, int]:
    """The ``{type: balance}`` map out of a tRPC reply. Defensive: only plain
    lower-case type names with a real number are kept; the rest is ignored."""
    cur: Any = doc
    for key in ("result", "data", "json"):
        if isinstance(cur, dict) and key in cur:
            cur = cur[key]
    out: dict[str, int] = {}
    if isinstance(cur, dict):
        for k, v in cur.items():
            if (isinstance(k, str) and _BUZZ_TYPE_RE.fullmatch(k)
                    and isinstance(v, (int, float)) and not isinstance(v, bool)
                    and v == v and abs(v) < 1e15):
                out[k] = int(v)
    return out


async def civitai_balance(res: Resolved) -> dict[str, int]:
    """The account's Buzz by type (``{"yellow": 1234, "blue": 56}``). The key goes
    to civitai.com only (fixed host) as a Bearer header. Raises ImageJobError;
    ``code == "scope"`` means the key lacks the Buzz: Read scope."""
    label = res.preset.label
    try:
        async with _client(30.0) as client:
            r = await client.get(CIVITAI_TRPC_BALANCE, headers={
                "Authorization": f"Bearer {res.api_key}"})
            if r.status_code in (401, 403) and "scope" in r.text.lower():
                raise image_jobs.ImageJobError(
                    f"{label} key can't read the Buzz balance", code="scope")
            if r.status_code >= 400:
                raise _error_from_status(r, label)
            doc = r.json()
    except image_jobs.ImageJobError:
        raise
    except httpx.TimeoutException:
        raise image_jobs.ImageJobError(f"{label} took too long to answer") from None
    except httpx.HTTPError as e:
        raise image_jobs.ImageJobError(
            f"{label} could not be reached ({type(e).__name__})") from None
    except ValueError:
        raise image_jobs.ImageJobError(f"{label} sent a reply that is not JSON") from None
    return parse_balances(doc)


#: sort name -> (Civitai sort, period)
CIVITAI_SORTS: dict[str, tuple[str, str]] = {
    "week": ("Most Downloaded", "Week"),
    "month": ("Most Downloaded", "Month"),
    "rated": ("Highest Rated", "Month"),
    "new": ("Newest", "Week"),
}
CIVITAI_BROWSE_BASES = ("SDXL 1.0", "Pony", "Illustrious")
CIVITAI_BROWSE_FETCH = 40
#: A name filter asks for more: Civitai's search also matches tags, so many hits lack the words in their name.
CIVITAI_BROWSE_FETCH_FILTERED = 100
CIVITAI_BROWSE_MAX = 30
CIVITAI_BROWSE_TTL_S = 15 * 60
#: The Browse panel's name filter: at most this many characters are sent to Civitai.
CIVITAI_BROWSE_QUERY_MAX = 60
#: How many (sort, filter) lists the browse cache keeps; the oldest goes first.
CIVITAI_BROWSE_CACHE_MAX = 64
_browse_cache: dict[tuple[str, str], tuple[float, list[dict]]] = {}


def _count(v: Any) -> int:
    return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0 else 0


def browse_query(text: Any) -> str:
    """The Browse filter as sent: whitespace folded, at most CIVITAI_BROWSE_QUERY_MAX chars."""
    return " ".join(str(text or "").split())[:CIVITAI_BROWSE_QUERY_MAX].strip()


def _name_has_all(name: str, words: tuple[str, ...]) -> bool:
    low = name.casefold()
    return all(w in low for w in words)


def _browse_rows(doc: Any, words: tuple[str, ...] = ()) -> list[dict]:
    """``words``: when given, keep only models whose NAME holds every word (case-blind).
    Civitai's own search also matches tags and creators; the person asked for the name."""
    items = doc.get("items") if isinstance(doc, dict) else None
    rows: list[dict] = []
    for it in items if isinstance(items, list) else []:
        try:
            if not isinstance(it, dict) or it.get("supportsGeneration") is not True:
                continue
            mid = it.get("id")
            vers = it.get("modelVersions")
            v0 = vers[0] if isinstance(vers, list) and vers else None
            vid = v0.get("id") if isinstance(v0, dict) else None
            name = it.get("name")
            if (isinstance(mid, bool) or not isinstance(mid, int) or mid < 1
                    or isinstance(vid, bool) or not isinstance(vid, int) or vid < 1
                    or not isinstance(name, str) or not name.strip()):
                continue
            air = f"urn:air:sdxl:checkpoint:civitai:{mid}@{vid}"
            if not _AIR_RE.fullmatch(air):
                continue
            if words and not _name_has_all(name, words):
                continue
            stats = it.get("stats") if isinstance(it.get("stats"), dict) else {}
            base = v0.get("baseModel")
            rows.append({
                "air": air, "name": " ".join(name.split())[:80],
                "base": " ".join(str(base).split())[:24] if isinstance(base, str) else "",
                "downloads": _count(stats.get("downloadCount")),
                "likes": _count(stats.get("thumbsUpCount")),
                "nsfw": it.get("nsfw") is True,
                "url": f"{CIVITAI_SITE_URL}/models/{mid}"})
        except (AttributeError, TypeError):
            continue
        if len(rows) >= CIVITAI_BROWSE_MAX:
            break
    return rows


async def civitai_top_models(sort: str, *, query: str = "",
                             fresh: bool = False) -> list[dict]:
    """Popular SDXL-family checkpoints Civitai can generate with, from the PUBLIC
    model list (no key is sent). ``query`` narrows it to models whose name holds
    every word; a filtered list searches all time (Civitai's week/month window
    leaves almost nothing once a name is given) in the same sort order. Cached
    15 minutes per (sort, filter); ``fresh`` skips the cache. Raises ValueError
    for an unknown sort, ImageJobError when Civitai fails."""
    if sort not in CIVITAI_SORTS:
        raise ValueError("unknown sort")
    q = browse_query(query)
    words = tuple(dict.fromkeys(q.casefold().split()))
    key = (sort, " ".join(words))
    now = _monotonic()
    hit = _browse_cache.get(key)
    if hit and not fresh and now - hit[0] < CIVITAI_BROWSE_TTL_S:
        return [dict(r) for r in hit[1]]
    order, period = CIVITAI_SORTS[sort]
    if q:
        period = "AllTime"
    params: list[tuple[str, str]] = [
        ("types", "Checkpoint"), ("sort", order), ("period", period),
        *(("baseModels", b) for b in CIVITAI_BROWSE_BASES),
        ("limit", str(CIVITAI_BROWSE_FETCH_FILTERED if q else CIVITAI_BROWSE_FETCH)),
        ("nsfw", "true")]
    if q:
        params.append(("query", q))
    try:
        async with _client(30.0) as client:
            r = await client.get(f"{CIVITAI_SITE_API}/models", params=params)
            if r.status_code >= 400:
                raise _error_from_status(r, "Civitai")
            doc = r.json()
    except image_jobs.ImageJobError:
        raise
    except httpx.TimeoutException:
        raise image_jobs.ImageJobError("Civitai took too long to answer") from None
    except httpx.HTTPError as e:
        raise image_jobs.ImageJobError(
            f"Civitai could not be reached ({type(e).__name__})") from None
    except ValueError:
        raise image_jobs.ImageJobError("Civitai sent a reply that is not JSON") from None
    rows = _browse_rows(doc, words)
    _browse_cache.pop(key, None)
    _browse_cache[key] = (now, rows)
    while len(_browse_cache) > CIVITAI_BROWSE_CACHE_MAX:
        _browse_cache.pop(next(iter(_browse_cache)))
    return [dict(x) for x in rows]


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
