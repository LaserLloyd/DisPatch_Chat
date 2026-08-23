"""ComfyUI service control: systemd lifecycle, Tailscale Serve gateway, and the
schema-validated launch-flags editor for ``~/comfy/comfy.env``.

Mirrors openclaw.py's shape: this is the ONLY module that shells out to
systemctl/journalctl/tailscale or calls ComfyUI's HTTP API for infrastructure
control. Routes in main.py compose these primitives and own the WS broadcasts;
this module stays pure/testable (mock subprocess + httpx, no FastAPI here).

Command whitelist (non-negotiable): the only binaries invoked are systemctl,
journalctl, and tailscale, against the fixed UNIT constant. No route parameter
ever reaches argv — flag values are schema-validated before they touch a file,
and the file is sourced by a script, never interpolated into a shell command.

Gateway is Tailscale Serve, never Funnel — grep this module for "funnel" and
expect zero matches. ComfyUI's own --listen stays pinned to 127.0.0.1 in
start-comfyui.sh; that address is not a field this module (or the API) can set.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
import os
import re
import shlex
import shutil
import tempfile
import time
from pathlib import Path

import httpx

from . import config

log = logging.getLogger("local-chat.comfy_service")

UNIT = "comfyui.service"
COMFY_DIR = Path.home() / "comfy"
COMFY_ENV_PATH = COMFY_DIR / "comfy.env"

# Dedicated to ComfyUI. NOT 8443: that port is already DisPatch's own tailnet
# HTTPS URL (tailscale serve :8443 -> 127.0.0.1:8765, set up before this
# feature existed) — reassigning it would silently break any bookmarked
# DisPatch link. Decided with the operator 2026-07-05; see the build directive.
GATEWAY_HTTPS_PORT = 8444

_HEALTH_TIMEOUT = 2.0        # seconds, per health() probe
_OP_TIMEOUT = 30.0           # seconds, default for systemctl/journalctl/tailscale calls
_START_WAIT_TIMEOUT = 60.0   # seconds, health-poll budget after start/restart


class ServiceError(Exception):
    """A control op failed. str(e) is safe to show the caller (no tracebacks)."""


class HealthTimeoutError(ServiceError):
    """start()/restart() issued fine but health never came up in time."""

    def __init__(self, journal_tail: str = ""):
        self.journal_tail = journal_tail
        super().__init__("ComfyUI did not become healthy in time")


class TailscaleUnavailableError(ServiceError):
    """tailscale/serve command failed (not installed, not up, no operator, ...)."""


class ServiceBusyError(ServiceError):
    """Another control op is already in flight (_svc_lock held)."""

    def __init__(self):
        super().__init__("operation in progress")


class FlagValidationError(ServiceError):
    """Incoming flag payload failed schema validation. .errors is key -> message."""

    def __init__(self, errors: dict[str, str]):
        self.errors = errors
        super().__init__("; ".join(f"{k}: {v}" for k, v in errors.items()))


# --------------------------------------------------------------------------- #
# Subprocess plumbing
# --------------------------------------------------------------------------- #

_BIN_CACHE: dict[str, str] = {}


def _bin(name: str) -> str:
    """Resolve a whitelisted binary's absolute path once (falls back to bare
    name so a missing binary fails naturally at exec time, not at import)."""
    if name not in _BIN_CACHE:
        import shutil
        _BIN_CACHE[name] = shutil.which(name) or name
    return _BIN_CACHE[name]


async def _run(
    argv: list[str], timeout: float = _OP_TIMEOUT, env: dict[str, str] | None = None,
) -> tuple[int, str, str]:
    """Run a fixed argv list (never shell=True). Returns (returncode, stdout, stderr)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env,
        )
    except OSError as e:
        raise ServiceError(f"failed to launch {argv[0]}: {e}") from e
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        with contextlib.suppress(Exception):
            await proc.wait()
        raise ServiceError(f"{argv[0]} timed out after {timeout:.0f}s") from None
    return proc.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")


def _tailscale_hint(stderr: str) -> str:
    s = (stderr or "").strip()
    low = s.lower()
    if "operator" in low or "access denied" in low or "permission" in low:
        return "Tailscale operator not set — run: sudo tailscale set --operator=$USER"
    return s or "tailscale command failed"


# --------------------------------------------------------------------------- #
# Exclusive control-op lock — concurrent ops get a 409, not a queue
# --------------------------------------------------------------------------- #

_svc_lock = asyncio.Lock()


@contextlib.asynccontextmanager
async def _exclusive():
    if _svc_lock.locked():
        raise ServiceBusyError()
    async with _svc_lock:
        yield


# --------------------------------------------------------------------------- #
# systemd unit state + health
# --------------------------------------------------------------------------- #

_UNIT_PROPS = "ActiveState,SubState,ExecMainStartTimestamp,NRestarts"


async def unit_state() -> dict:
    rc, out, err = await _run([_bin("systemctl"), "--user", "show", UNIT, "-p", _UNIT_PROPS])
    if rc != 0:
        raise ServiceError(err.strip() or f"systemctl show {UNIT} failed (rc={rc})")
    props: dict[str, str] = {}
    for line in out.splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            props[k] = v
    return {
        "active_state": props.get("ActiveState", "unknown"),
        "sub_state": props.get("SubState", "unknown"),
        "start_timestamp": props.get("ExecMainStartTimestamp") or None,
        "n_restarts": int(props.get("NRestarts") or 0),
    }


async def health(port: int | None = None) -> dict | None:
    """GET /system_stats with a short timeout. None means unreachable/unhealthy."""
    if port is None:
        port = read_flags()["values"]["port"]
    try:
        async with httpx.AsyncClient(timeout=_HEALTH_TIMEOUT) as client:
            r = await client.get(f"http://127.0.0.1:{port}/system_stats")
        if r.status_code == 200:
            return r.json()
    except (httpx.HTTPError, ValueError):
        pass
    return None


async def start_timestamp_epoch() -> float | None:
    """When the unit's main process started, as a UTC epoch float (None if the
    unit has never started or the timestamp can't be parsed). Forces TZ=UTC on
    the subprocess so systemd's human-readable timestamp is locale/tz-stable,
    rather than trying to map arbitrary tz abbreviations back to an offset."""
    rc, out, err = await _run(
        [_bin("systemctl"), "--user", "show", UNIT, "-p", "ExecMainStartTimestamp", "--value"],
        env={**os.environ, "TZ": "UTC"},
    )
    if rc != 0 or not out.strip():
        return None
    ts = re.sub(r"\s+UTC$", "", out.strip())
    try:
        import calendar
        return float(calendar.timegm(time.strptime(ts, "%a %Y-%m-%d %H:%M:%S")))
    except ValueError:
        return None


async def flags_dirty() -> bool:
    """True if comfy.env was written more recently than the unit last started
    — i.e. a saved flag change hasn't taken effect yet (needs a restart)."""
    if not COMFY_ENV_PATH.exists():
        return False
    start_ts = await start_timestamp_epoch()
    if start_ts is None:
        return False
    with contextlib.suppress(OSError):
        return COMFY_ENV_PATH.stat().st_mtime > start_ts
    return False


async def logs(lines: int = 100) -> str:
    lines = max(1, min(int(lines), 500))
    rc, out, err = await _run(
        [_bin("journalctl"), "--user", "-u", UNIT, "-n", str(lines), "--no-pager", "-o", "cat"],
    )
    if rc != 0:
        raise ServiceError(err.strip() or f"journalctl failed (rc={rc})")
    return out


async def _wait_healthy(port: int) -> dict:
    deadline = time.monotonic() + _START_WAIT_TIMEOUT
    while time.monotonic() < deadline:
        stats = await health(port)
        if stats is not None:
            return {"healthy": True, "stats": stats}
        await asyncio.sleep(1.0)
    tail = ""
    with contextlib.suppress(ServiceError):
        tail = await logs(20)
    raise HealthTimeoutError(tail)


async def _do_start() -> dict:
    rc, out, err = await _run([_bin("systemctl"), "--user", "start", UNIT])
    if rc != 0:
        raise ServiceError(err.strip() or f"systemctl start {UNIT} failed (rc={rc})")
    return await _wait_healthy(read_flags()["values"]["port"])


async def _do_stop() -> None:
    rc, out, err = await _run([_bin("systemctl"), "--user", "stop", UNIT])
    if rc != 0:
        raise ServiceError(err.strip() or f"systemctl stop {UNIT} failed (rc={rc})")


async def _do_restart() -> dict:
    rc, out, err = await _run([_bin("systemctl"), "--user", "restart", UNIT])
    if rc != 0:
        raise ServiceError(err.strip() or f"systemctl restart {UNIT} failed (rc={rc})")
    return await _wait_healthy(read_flags()["values"]["port"])


async def start() -> dict:
    async with _exclusive():
        return await _do_start()


async def stop() -> None:
    async with _exclusive():
        await _do_stop()


async def restart() -> dict:
    async with _exclusive():
        return await _do_restart()


# --------------------------------------------------------------------------- #
# Tailscale Serve gateway (never Funnel)
# --------------------------------------------------------------------------- #

_hostname_cache: tuple[float, str] | None = None


async def tailnet_hostname() -> str:
    global _hostname_cache
    now = time.monotonic()
    if _hostname_cache and now - _hostname_cache[0] < 60.0:
        return _hostname_cache[1]
    rc, out, err = await _run([_bin("tailscale"), "status", "--json"])
    if rc != 0:
        raise TailscaleUnavailableError(_tailscale_hint(err))
    name = ""
    with contextlib.suppress(json.JSONDecodeError, AttributeError):
        name = (json.loads(out).get("Self") or {}).get("DNSName") or ""
    name = name.rstrip(".")
    if not name:
        raise TailscaleUnavailableError("tailscale status did not return a DNSName")
    _hostname_cache = (now, name)
    return name


async def _do_gateway_status() -> dict:
    rc, out, err = await _run([_bin("tailscale"), "serve", "status", "--json"])
    if rc != 0:
        raise TailscaleUnavailableError(_tailscale_hint(err))
    try:
        data = json.loads(out or "{}")
    except json.JSONDecodeError:
        data = {}
    on = str(GATEWAY_HTTPS_PORT) in (data.get("TCP") or {})
    url = f"https://{await tailnet_hostname()}:{GATEWAY_HTTPS_PORT}/" if on else None
    return {"on": on, "url": url}


async def gateway_status() -> dict:
    return await _do_gateway_status()


async def _do_gateway_on(comfy_port: int | None = None) -> str:
    if comfy_port is None:
        comfy_port = read_flags()["values"]["port"]
    target = f"http://127.0.0.1:{comfy_port}"
    rc, out, err = await _run(
        [_bin("tailscale"), "serve", "--bg", f"--https={GATEWAY_HTTPS_PORT}", target]
    )
    if rc != 0:
        raise TailscaleUnavailableError(_tailscale_hint(err))
    return f"https://{await tailnet_hostname()}:{GATEWAY_HTTPS_PORT}/"


async def _do_gateway_off() -> None:
    rc, out, err = await _run([_bin("tailscale"), "serve", f"--https={GATEWAY_HTTPS_PORT}", "off"])
    if rc != 0:
        raise TailscaleUnavailableError(_tailscale_hint(err))


async def gateway_on(comfy_port: int | None = None) -> str:
    async with _exclusive():
        return await _do_gateway_on(comfy_port)


async def gateway_off() -> None:
    async with _exclusive():
        await _do_gateway_off()


async def launch() -> dict:
    """The one-button path: ensure running, ensure gateway on, return the URL."""
    async with _exclusive():
        await _do_start()
        url = await _do_gateway_on()
        return {"url": url}


# --------------------------------------------------------------------------- #
# Flag schema (backend holds it; the frontend renders it, doesn't invent it)
# --------------------------------------------------------------------------- #

FLAG_SCHEMA = [
    {"key": "port", "label": "Port", "kind": "int", "min": 1024, "max": 65535, "default": 8188,
     "note": "Changing this re-points the gateway on save + restart."},
    {"key": "vram_mode", "label": "VRAM mode", "kind": "enum",
     "choices": ["none", "lowvram", "novram", "highvram", "gpu-only"], "default": "none",
     "note": "Unified memory usually wants none or highvram."},
    {"key": "reserve_vram", "label": "Reserve VRAM (GB)", "kind": "float", "min": 0, "max": None,
     "default": None, "note": "Headroom for the co-resident local LLMs."},
    {"key": "disable_mmap", "label": "Disable mmap", "kind": "bool", "default": True,
     "note": "Community-validated critical on gfx1151 — mmap of >64GB weights is "
             "pathologically slow under current ROCm."},
    {"key": "cache_mode", "label": "Model cache", "kind": "enum",
     "choices": ["default", "none", "lru"], "default": "default",
     "note": "'none' frees unified RAM aggressively between runs (good when LLMs share the box)."},
    {"key": "cache_lru_n", "label": "Cache LRU size (N)", "kind": "int", "min": 0, "max": None,
     "default": 0, "note": "Only used when Model cache = lru."},
    {"key": "bf16_vae", "label": "bf16 VAE", "kind": "bool", "default": False,
     "note": "Used in Strix Halo community builds; test for output parity."},
    {"key": "fast_mode", "label": "Fast mode", "kind": "bool", "default": False,
     "note": "Experimental optimizations; verify on ROCm before defaulting on."},
    {"key": "attention", "label": "Attention", "kind": "enum",
     "choices": ["", "split", "quad", "pytorch", "sage", "flash"], "default": "",
     "note": "Empty = ComfyUI's own default selection."},
    {"key": "preview_method", "label": "Preview", "kind": "enum",
     "choices": ["", "none", "auto", "latent2rgb", "taesd"], "default": "auto"},
    {"key": "gfx_override", "label": "GFX override", "kind": "regex",
     # \A..\Z, not ^..$: `$` also matches just before a trailing newline, so
     # "11.5.1\n" passed validation and went into the unit's environment.
     "pattern": r"\A\d+\.\d+\.\d+\Z", "default": "11.5.1"},
    {"key": "hip_alloc_conf", "label": "HIP alloc", "kind": "enum",
     "choices": ["expandable_segments:True", "expandable_segments:False", ""],
     "default": "expandable_segments:True"},
    {"key": "aotriton_fa", "label": "AOTriton FA (experimental)", "kind": "bool", "default": False,
     "note": "Experimental Triton flash-attention path used by Strix Halo toolboxes."},
    {"key": "tunableop", "label": "TunableOp", "kind": "bool", "default": False,
     "note": "Kernel autotuning; slow first runs."},
]
_SCHEMA_BY_KEY = {f["key"]: f for f in FLAG_SCHEMA}

# The known-working recipe for gfx1151 — "Reset to known-good" PUTs this.
# Deliberately omits "port": resetting perf flags shouldn't relocate the server.
KNOWN_GOOD = {
    "vram_mode": "none", "reserve_vram": None, "disable_mmap": True,
    "cache_mode": "default", "cache_lru_n": 0, "bf16_vae": False, "fast_mode": False,
    "attention": "", "preview_method": "auto",
    "gfx_override": "11.5.1", "hip_alloc_conf": "expandable_segments:True",
    "aotriton_fa": False, "tunableop": False,
}

# Logical key -> the raw env var it's stored as directly (not assembled into
# COMFY_ARGS). HIP_VISIBLE_DEVICES is deliberately absent here: it ships in the
# file but has no UI control, so it always falls into "unmanaged" below and is
# preserved verbatim rather than silently dropped.
_ENV_KEYS = {
    "port": "COMFY_PORT",
    "gfx_override": "HSA_OVERRIDE_GFX_VERSION",
    "hip_alloc_conf": "PYTORCH_HIP_ALLOC_CONF",
    "aotriton_fa": "TORCH_ROCM_AOTRITON_ENABLE_EXPERIMENTAL",
    "tunableop": "PYTORCH_TUNABLEOP_ENABLED",
}
_MANAGED_RAW_KEYS = set(_ENV_KEYS.values()) | {"COMFY_ARGS"}

_VRAM_TOKENS = {"--lowvram": "lowvram", "--novram": "novram",
                "--highvram": "highvram", "--gpu-only": "gpu-only"}
_ATTN_TOKENS = {
    "--use-split-cross-attention": "split", "--use-quad-cross-attention": "quad",
    "--use-pytorch-cross-attention": "pytorch", "--use-sage-attention": "sage",
    "--use-flash-attention": "flash",
}
_ATTN_TOKENS_REV = {v: k for k, v in _ATTN_TOKENS.items()}


def _parse_comfy_args(args_str: str) -> tuple[dict, list[str]]:
    """Parse COMFY_ARGS into our logical toggle values. Tokens we don't
    recognise (hand-added flags) are returned as `leftover` and re-appended
    verbatim on the next write — never silently dropped."""
    values = {
        "vram_mode": "none", "reserve_vram": None, "disable_mmap": False,
        "cache_mode": "default", "cache_lru_n": 0, "bf16_vae": False,
        "fast_mode": False, "attention": "", "preview_method": "",
    }
    try:
        tokens = shlex.split(args_str or "")
    except ValueError:
        # A hand-edited unbalanced quote must not brick every route that
        # transitively calls read_flags() (status poll, start, launch, ...).
        # Bash sources the file fine either way (quotes inside double quotes
        # are literal there); degrade to whitespace tokens and keep serving.
        log.warning("COMFY_ARGS is not shlex-parseable; falling back to whitespace split")
        tokens = (args_str or "").split()
    leftover: list[str] = []
    i = 0
    while i < len(tokens):
        t = tokens[i]
        if t in _VRAM_TOKENS:
            values["vram_mode"] = _VRAM_TOKENS[t]
        elif t in _ATTN_TOKENS:
            values["attention"] = _ATTN_TOKENS[t]
        elif t == "--disable-mmap":
            values["disable_mmap"] = True
        elif t == "--cache-none":
            values["cache_mode"] = "none"
        elif t == "--bf16-vae":
            values["bf16_vae"] = True
        elif t == "--fast":
            values["fast_mode"] = True
        elif t == "--reserve-vram" and i + 1 < len(tokens):
            with contextlib.suppress(ValueError):
                values["reserve_vram"] = float(tokens[i + 1])
            i += 1
        elif t == "--cache-lru" and i + 1 < len(tokens):
            values["cache_mode"] = "lru"
            with contextlib.suppress(ValueError):
                values["cache_lru_n"] = int(tokens[i + 1])
            i += 1
        elif t == "--preview-method" and i + 1 < len(tokens):
            values["preview_method"] = tokens[i + 1]
            i += 1
        elif t == "--enable-cors-header":
            # Bare form is the baseline invariant (see _build_comfy_args) —
            # consumed here so the always-re-added copy doesn't duplicate.
            # A hand-edited EXPLICIT origin ("--enable-cors-header https://x")
            # is a deliberate tightening: preserve flag+origin verbatim as
            # leftover, and _build_comfy_args skips the bare baseline then —
            # a save must never silently widen the origin back to '*'.
            if i + 1 < len(tokens) and not tokens[i + 1].startswith("-"):
                leftover += [t, tokens[i + 1]]
                i += 1
        else:
            leftover.append(t)
        i += 1
    return values, leftover


def _build_comfy_args(values: dict, leftover: list[str]) -> str:
    # Always-on baseline invariant, not a user-facing toggle. Discovered during
    # real E2E testing of the tailnet gateway: ComfyUI's own
    # create_origin_only_middleware() (server.py) unconditionally 403s any
    # request carrying `Sec-Fetch-Site: cross-site` — and EVERY click of
    # DisPatch's "Open ComfyUI" chip is exactly that (a top-level navigation
    # from DisPatch's origin into the tailnet-proxied ComfyUI origin). Passing
    # --enable-cors-header swaps that middleware for the permissive CORS one,
    # which does not do the Sec-Fetch-Site check. The wildcard origin is fine
    # here: CORS only governs cross-origin fetch()/XHR reads (nothing in this
    # design does that — DisPatch talks to ComfyUI server-to-server), and the
    # real access boundary is Tailscale network membership, not this header.
    # If a hand-edited explicit-origin form survives in leftover, honor it
    # instead of the wildcard baseline (see _parse_comfy_args).
    parts: list[str] = [] if "--enable-cors-header" in leftover else ["--enable-cors-header"]
    vm = values.get("vram_mode", "none")
    if vm in ("lowvram", "novram", "highvram", "gpu-only"):
        parts.append(f"--{vm}")
    rv = values.get("reserve_vram")
    if rv is not None:
        parts += ["--reserve-vram", str(rv)]
    if values.get("disable_mmap"):
        parts.append("--disable-mmap")
    cm = values.get("cache_mode", "default")
    if cm == "none":
        parts.append("--cache-none")
    elif cm == "lru":
        parts += ["--cache-lru", str(int(values.get("cache_lru_n") or 0))]
    if values.get("bf16_vae"):
        parts.append("--bf16-vae")
    if values.get("fast_mode"):
        parts.append("--fast")
    attn = values.get("attention", "")
    if attn in _ATTN_TOKENS_REV:
        parts.append(_ATTN_TOKENS_REV[attn])
    pm = values.get("preview_method", "")
    if pm:
        parts += ["--preview-method", pm]
    parts.extend(leftover)
    return shlex.join(parts)


def _unquote(v: str) -> str:
    """Exact inverse of _quote (bash-compatible for the forms we emit):
    double-quoted values get their \\" and \\\\ escapes undone; single-quoted
    values are literal; bare values pass through."""
    if len(v) >= 2 and v[0] == v[-1] == '"':
        return v[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    if len(v) >= 2 and v[0] == v[-1] == "'":
        return v[1:-1]
    return v


def _parse_env_file(text: str) -> tuple[dict[str, str], dict[str, str]]:
    """Returns (values, raw_lines): parsed values per key, plus each key's
    original line VERBATIM — unmanaged (hand-edited) lines are re-emitted
    from raw_lines on rewrite, never re-encoded, so a save can't mutate
    someone's quoting or introduce bash expansion they didn't write."""
    raw: dict[str, str] = {}
    raw_lines: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        k, _, v = stripped.partition("=")
        k = k.strip()
        raw[k] = _unquote(v.strip())
        raw_lines[k] = stripped
    return raw, raw_lines


def _quote(v) -> str:
    s = str(v).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{s}"'


def _safe_int(v, default: int) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _read_full() -> tuple[dict, dict, list[str], list[str]]:
    text = COMFY_ENV_PATH.read_text() if COMFY_ENV_PATH.exists() else ""
    raw, raw_lines = _parse_env_file(text)
    values = {
        "port": _safe_int(raw.get("COMFY_PORT"), 8188),
        "gfx_override": raw.get("HSA_OVERRIDE_GFX_VERSION", "11.5.1"),
        "hip_alloc_conf": raw.get("PYTORCH_HIP_ALLOC_CONF", "expandable_segments:True"),
        "aotriton_fa": raw.get("TORCH_ROCM_AOTRITON_ENABLE_EXPERIMENTAL", "") == "1",
        "tunableop": raw.get("PYTORCH_TUNABLEOP_ENABLED", "") == "1",
    }
    arg_values, leftover = _parse_comfy_args(raw.get("COMFY_ARGS", ""))
    values.update(arg_values)
    unmanaged = {k: v for k, v in raw.items() if k not in _MANAGED_RAW_KEYS}
    unmanaged_lines = [raw_lines[k] for k in raw if k not in _MANAGED_RAW_KEYS]
    return values, unmanaged, leftover, unmanaged_lines


def read_flags() -> dict:
    values, unmanaged, _leftover, _lines = _read_full()
    return {"schema": FLAG_SCHEMA, "values": values, "unmanaged": unmanaged, "known_good": KNOWN_GOOD}


def _validate_one(spec: dict, value) -> str | None:
    kind = spec["kind"]
    if kind == "bool":
        if not isinstance(value, bool):
            return "must be a boolean"
    elif kind == "int":
        if not isinstance(value, int) or isinstance(value, bool):
            return "must be an integer"
        if spec.get("min") is not None and value < spec["min"]:
            return f"must be >= {spec['min']}"
        if spec.get("max") is not None and value > spec["max"]:
            return f"must be <= {spec['max']}"
    elif kind == "float":
        if value is not None:
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                return "must be a number or null"
            # JSON parsing accepts NaN/Infinity, and `nan < min` is False —
            # without this, NaN sails through the range check into argv.
            if not math.isfinite(value):
                return "must be a finite number"
            if spec.get("min") is not None and value < spec["min"]:
                return f"must be >= {spec['min']}"
            if spec.get("max") is not None and value > spec["max"]:
                return f"must be <= {spec['max']}"
    elif kind == "enum":
        if value not in spec["choices"]:
            return f"must be one of {spec['choices']}"
    elif kind == "regex":
        if not isinstance(value, str) or not re.match(spec["pattern"], value):
            return f"must match {spec['pattern']}"
    return None


def _write_atomic(content: str) -> None:
    if COMFY_ENV_PATH.exists() or COMFY_ENV_PATH.is_symlink():
        resolved_parent = COMFY_ENV_PATH.resolve().parent
        if resolved_parent != COMFY_DIR.resolve():
            raise ServiceError(f"refusing to write: {COMFY_ENV_PATH} resolves outside {COMFY_DIR}")
    COMFY_DIR.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(COMFY_DIR), prefix=".comfy.env.")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(content)
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, COMFY_ENV_PATH)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


def write_flags(incoming: dict) -> dict:
    """Validate `incoming` (a partial values dict) against the schema, merge it
    over the current file, and atomically rewrite comfy.env. Never restarts —
    the caller (route) decides, so the UI can batch edits into one restart."""
    if not isinstance(incoming, dict):
        raise FlagValidationError({"_": "payload must be an object"})
    unknown = [k for k in incoming if k not in _SCHEMA_BY_KEY]
    if unknown:
        raise FlagValidationError(dict.fromkeys(unknown, "unknown flag"))

    errors = {}
    for k, v in incoming.items():
        err = _validate_one(_SCHEMA_BY_KEY[k], v)
        if err:
            errors[k] = err
    if errors:
        raise FlagValidationError(errors)

    values, unmanaged, leftover, unmanaged_lines = _read_full()
    values.update(incoming)

    lines = [
        "# Managed by DisPatch Chat — edited via the ComfyUI panel. One KEY=VALUE per line.",
        "# COMFY_LISTEN is intentionally absent and not editable: the listen address",
        "# is pinned to 127.0.0.1 in start-comfyui.sh.",
        f'COMFY_PORT={_quote(values["port"])}',
        f'COMFY_ARGS={_quote(_build_comfy_args(values, leftover))}',
        "",
        "# GPU / ROCm environment (Strix Halo gfx1151)",
        f'HSA_OVERRIDE_GFX_VERSION={_quote(values["gfx_override"])}',
        f'PYTORCH_HIP_ALLOC_CONF={_quote(values["hip_alloc_conf"])}',
    ]
    if values.get("aotriton_fa"):
        lines.append('TORCH_ROCM_AOTRITON_ENABLE_EXPERIMENTAL="1"')
    if values.get("tunableop"):
        lines.append('PYTORCH_TUNABLEOP_ENABLED="1"')
    if unmanaged_lines:
        lines.append("")
        lines.append("# --- unmanaged (hand-edited) lines, preserved verbatim ---")
        lines.extend(unmanaged_lines)
    _write_atomic("\n".join(lines) + "\n")
    return read_flags()


# --------------------------------------------------------------------------- #
# Workflow manager — save / list / trash / snapshot of ComfyUI workflow JSONs
#
# Source of truth: config.COMFY_WORKFLOWS_DIR (where ComfyUI's own "Save" menu
# writes). Snapshots go to config.COMFY_WORKFLOW_BACKUP_DIR. Pure file logic —
# routes in main.py own HTTP mapping, exactly like the rest of this module.
# Path safety is non-negotiable: names are basenames only, must end .json, and
# every resolved path must stay INSIDE the workflows dir.
# --------------------------------------------------------------------------- #

WORKFLOW_MAX_BYTES = 5 * 1024 * 1024      # per-workflow size cap
WORKFLOW_TRASH_KEEP = 20                  # max files kept in .trash/
WORKFLOW_SNAPSHOT_KEEP = 20               # max snapshot dirs kept


class WorkflowError(ServiceError):
    """A workflow file op failed. str(e) is caller-safe (no paths/tracebacks);
    .status is the HTTP status the route should return."""

    def __init__(self, message: str, status: int = 400):
        self.status = status
        super().__init__(message)


def _wf_dir() -> Path:
    return config.COMFY_WORKFLOWS_DIR


def _wf_backup_dir() -> Path:
    return config.COMFY_WORKFLOW_BACKUP_DIR


def sanitize_workflow_name(name: str) -> str:
    """Return a safe basename, or raise. Rejects anything that isn't already a
    plain `<basename>.json` (path separators, .., absolute paths, dotfiles)."""
    raw = str(name or "")
    base = Path(raw).name.strip()
    if not base or base != raw or base.startswith(".") or ".." in base:
        raise WorkflowError("Invalid workflow name", 400)
    if not base.lower().endswith(".json") or len(base) <= len(".json"):
        raise WorkflowError("Workflow name must end in .json", 400)
    return base


def workflow_path(name: str) -> Path:
    """Sanitized name → absolute path, provably confined to the workflows dir
    (resolve() also unmasks a symlink pointing outside)."""
    base = sanitize_workflow_name(name)
    d = _wf_dir().resolve()
    p = (d / base).resolve()
    if p.parent != d or p.name != base:
        raise WorkflowError("Invalid workflow name", 400)
    return p


def last_workflow_backup_epoch() -> int | None:
    """Epoch of the newest snapshot dir (its creation mtime), or None."""
    try:
        dirs = [p for p in _wf_backup_dir().iterdir() if p.is_dir()]
        if not dirs:
            return None
        return int(max(p.stat().st_mtime for p in dirs))
    except OSError:
        return None


def list_workflows() -> dict:
    d = _wf_dir()
    items = []
    with contextlib.suppress(OSError):
        for p in d.glob("*.json"):
            if not p.is_file():
                continue
            st = p.stat()
            items.append({"name": p.name, "size": st.st_size,
                          "modified_epoch": int(st.st_mtime)})
    items.sort(key=lambda x: x["modified_epoch"], reverse=True)
    return {"dir": str(d), "workflows": items,
            "last_backup_epoch": last_workflow_backup_epoch()}


def workflow_file(name: str) -> Path:
    """Existing workflow's path (for download), or 404."""
    p = workflow_path(name)
    if not p.is_file():
        raise WorkflowError("Workflow not found", 404)
    return p


def save_workflow(name: str, body: bytes) -> dict:
    """Save/import a workflow: validated JSON, size-capped, atomic write, and a
    single rolling `<stem>.bak.json` copy of any file being overwritten."""
    if len(body) > WORKFLOW_MAX_BYTES:
        raise WorkflowError("Workflow too large (max 5MB)", 413)
    try:
        json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise WorkflowError("Not valid workflow JSON", 422)
    p = workflow_path(name)
    d = _wf_dir()
    try:
        d.mkdir(parents=True, exist_ok=True)
        if p.is_file():
            # Rolling safety net: keep exactly one previous version.
            shutil.copy2(p, p.with_name(f"{p.stem}.bak.json"))
        fd, tmp = tempfile.mkstemp(dir=str(d), prefix=".wf-")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(body)
            os.replace(tmp, p)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise
    except OSError as e:
        log.error("workflow save failed (%s): %s", p.name, e)
        raise WorkflowError("Disk write failed", 507)
    return {"ok": True, "name": p.name}


def trash_workflow(name: str) -> dict:
    """Soft delete: move into .trash/ (epoch-suffixed on collision), keeping at
    most WORKFLOW_TRASH_KEEP files (oldest pruned by trash time)."""
    p = workflow_file(name)
    trash = _wf_dir() / ".trash"
    try:
        trash.mkdir(parents=True, exist_ok=True)
        dest = trash / p.name
        if dest.exists():
            dest = trash / f"{p.stem}.{int(time.time())}{p.suffix}"
        os.replace(p, dest)
        # st_ctime updates on the rename, so it orders by WHEN it was trashed
        # (st_mtime survives the move and would order by content age instead).
        files = sorted((f for f in trash.iterdir() if f.is_file()),
                       key=lambda f: f.stat().st_ctime)
        for f in files[:-WORKFLOW_TRASH_KEEP]:
            with contextlib.suppress(OSError):
                f.unlink()
    except OSError as e:
        log.error("workflow trash failed (%s): %s", p.name, e)
        raise WorkflowError("Could not move workflow to trash", 507)
    return {"ok": True, "trashed": True}


def snapshot_workflows() -> dict:
    """Copy every top-level *.json to a new UTC-stamped snapshot dir, then
    rotate to the WORKFLOW_SNAPSHOT_KEEP newest dirs."""
    files = []
    with contextlib.suppress(OSError):
        files = [p for p in _wf_dir().glob("*.json") if p.is_file()]
    bdir = _wf_backup_dir()
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    try:
        bdir.mkdir(parents=True, exist_ok=True)
        dest = bdir / stamp
        n = 1
        while dest.exists():                # same-second snapshots still unique
            dest = bdir / f"{stamp}-{n:02d}"
            n += 1
        dest.mkdir()
        for p in files:
            shutil.copy2(p, dest / p.name)
        dirs = sorted((p for p in bdir.iterdir() if p.is_dir()),
                      key=lambda p: p.name)
        for old in dirs[:-WORKFLOW_SNAPSHOT_KEEP]:
            shutil.rmtree(old, ignore_errors=True)
    except OSError as e:
        log.error("workflow snapshot failed: %s", e)
        raise WorkflowError("Snapshot failed", 507)
    return {"ok": True, "snapshot": dest.name, "count": len(files)}


def maybe_snapshot_workflows() -> dict | None:
    """Auto-backup hook (DisPatch's periodic backup loop): snapshot only when
    some workflow is newer than the newest snapshot. Returns the snapshot
    result, or None when there is nothing new to back up."""
    try:
        files = [p for p in _wf_dir().glob("*.json") if p.is_file()]
        if not files:
            return None
        newest_wf = max(int(p.stat().st_mtime) for p in files)
    except OSError:
        return None
    last = last_workflow_backup_epoch()
    if last is not None and newest_wf <= last:
        return None
    return snapshot_workflows()
