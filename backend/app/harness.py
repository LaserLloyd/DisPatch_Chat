"""DeepSeek Harness (`dsh`) integration: the second coding-agent engine next to
the coding terminal.

Unlike a TUI-first CLI, dsh has no TUI in the official package — its two shipped
profiles are `web` (a browser UI on 127.0.0.1:3080) and `headless` (one task
in, one answer out). So the DisPatch pane is built from three pieces:

  1. **Service control** of the `dsh web` systemd --user unit (start / stop /
     restart / health). The pane embeds the Web UI
     in an iframe when the browser can reach loopback.
  2. **Default-model switch**: reads/writes `agent-default-model` in
     `$DSH_HOME/settings.yaml`, which dsh hot-reloads. The catalog offered to the
     picker is the union of the DeepSeek route (`llm-deepseek.models`, falling
     back to dsh's shipped defaults) and every `llm-pi-ai.providers.*` route.
     A pi-ai route with no `models:` list is NOT model-less: per the adapter's
     own contract ("Omission serves the installed catalog for the route
     unchanged") it serves whatever pi-ai's bundled catalog ships for that
     route, so the picker reads that catalog off disk rather than showing an
     empty provider. That is what makes a two-line `providers: {minimax: {…}}`
     route selectable without naming a single model id here.
  3. **Headless jobs**: one task at a time, spawned as the fixed argv
     `[dsh, --profile, headless, <task>]` — no shell, the task is a single argv
     element, and the working directory is validated to be an existing directory
     under $HOME. Output (the final answer) is captured and broadcast when the
     job ends; a bounded history is kept in memory only.

Command whitelist (non-negotiable): the only binaries ever invoked are
`systemctl` (against the fixed UNIT) and the resolved `dsh` binary. This module
stays pure/testable (no FastAPI); routes in main.py compose these primitives
and own the HTTP error mapping + WS broadcasts. Every surface is full-session
only — a headless job is arbitrary code execution.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import json
import logging
import os
import re
import shutil
import signal
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

import httpx
import yaml

log = logging.getLogger("local-chat.harness")


def _group_members(pgid: int) -> list[int]:
    """Pids currently in process group ``pgid`` (Linux /proc; [] elsewhere).

    Used instead of a blanket ``killpg`` once the group LEADER has been
    reaped: at that moment its pid can be recycled, and killpg on a recycled
    pid would signal a stranger's whole process group.
    """
    out: list[int] = []
    try:
        entries = os.listdir("/proc")
    except OSError:
        return out
    me = os.getpid()
    for name in entries:
        if not name.isdigit():
            continue
        pid = int(name)
        if pid == me:
            continue
        try:
            if os.getpgid(pid) == pgid:
                out.append(pid)
        except (ProcessLookupError, PermissionError, OSError):
            continue
    return out


# ---- deployment facts (env-overridable in config.py; these are the defaults) --
UNIT = "dsh-web.service"
DEFAULT_PORT = 3080
# Optional explicit path (DISPATCH_DSH_BIN) for installs where dsh lives outside
# the service's PATH — a user-local npm prefix, say.
DSH_FALLBACK = os.environ.get("DISPATCH_DSH_BIN", "")

# dsh's own catalog defaults when `llm-deepseek.models` is not configured
# (see @deepseek-ai/dsh-llm-deepseek README: "Omitting `models` advertises
# deepseek-v4-flash and deepseek-v4-pro").
DEEPSEEK_PROVIDER = "deepseek-official"
DEEPSEEK_DEFAULT_MODELS = [
    {"id": "deepseek-v4-flash", "name": "DeepSeek-V4-Flash"},
    {"id": "deepseek-v4-pro", "name": "DeepSeek-V4-Pro"},
]

_HEALTH_TIMEOUT = 2.0
_OP_TIMEOUT = 30.0
_START_WAIT_TIMEOUT = 45.0
_STOP_GRACE = 5.0
JOB_TIMEOUT_DEFAULT = 900.0        # seconds a headless job may run
JOB_HISTORY_MAX = 20
JOB_OUTPUT_MAX = 64 * 1024         # bytes of stdout kept per job
# Hard ceiling on how much a job may WRITE before we stop listening and kill
# it. Only the last JOB_OUTPUT_MAX bytes are ever kept, but the read itself
# used to be `communicate()` — which buffers everything the child produces in
# this process's memory before the truncation happens, so a runaway job (a
# loop printing, a binary dumped to stdout) is an OOM, not a big log.
JOB_OUTPUT_HARD_MAX = 8 * 1024 * 1024
_READ_CHUNK = 64 * 1024
TASK_MAX_CHARS = 8000

# Provider / model ids land in a YAML file and (via dsh) in API requests —
# keep them tight tokens. \A..\Z so a trailing newline can't slip through.
_ID_RE = re.compile(r"\A(?!-)[A-Za-z0-9._:/-]{1,128}\Z")


class HarnessError(Exception):
    """A harness op failed. str(e) is safe to show the caller (no tracebacks)."""


class HarnessBusyError(HarnessError):
    """Another control op / job is already in flight."""

    def __init__(self, what: str = "operation in progress"):
        super().__init__(what)


class HealthTimeoutError(HarnessError):
    """start()/restart() issued fine but the UI never came up in time."""


class ValidationError(HarnessError):
    """A caller-supplied value failed validation (→ 422)."""


# --------------------------------------------------------------------------- #
# Binary + home resolution
# --------------------------------------------------------------------------- #

def dsh_home() -> Path:
    return Path(os.environ.get("DSH_HOME") or (Path.home() / ".dsh"))


def resolve_binary() -> str | None:
    """Absolute path of `dsh`, or None when it is not installed. Checked live
    (not cached) so an install/uninstall is reflected without a restart."""
    found = shutil.which("dsh")
    if found:
        return found
    return DSH_FALLBACK if os.access(DSH_FALLBACK, os.X_OK) else None


def installed() -> bool:
    return resolve_binary() is not None


# --------------------------------------------------------------------------- #
# Fixed-argv subprocess helper (never shell=True)
# --------------------------------------------------------------------------- #

_BIN_CACHE: dict[str, str] = {}


def _bin(name: str) -> str:
    if name not in _BIN_CACHE:
        _BIN_CACHE[name] = shutil.which(name) or name
    return _BIN_CACHE[name]


async def _run(argv: list[str], timeout: float = _OP_TIMEOUT) -> tuple[int, str, str]:
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
    except OSError as e:
        raise HarnessError(f"failed to launch {argv[0]}: {e}") from e
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        with contextlib.suppress(Exception):
            await proc.wait()
        raise HarnessError(f"{argv[0]} timed out after {timeout:.0f}s") from None
    return proc.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")


# --------------------------------------------------------------------------- #
# systemd unit state + health (the `dsh web` service)
# --------------------------------------------------------------------------- #

_svc_lock = asyncio.Lock()


@contextlib.asynccontextmanager
async def _exclusive():
    if _svc_lock.locked():
        raise HarnessBusyError()
    async with _svc_lock:
        yield


_UNIT_PROPS = "ActiveState,SubState,ExecMainStartTimestamp,NRestarts,UnitFileState"


async def unit_state(unit: str = UNIT) -> dict:
    """systemd view of the unit. A missing/unknown unit is reported, not raised:
    the pane must render "not installed" rather than 502."""
    rc, out, err = await _run([_bin("systemctl"), "--user", "show", unit, "-p", _UNIT_PROPS])
    props: dict[str, str] = {}
    for line in out.splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            props[k] = v
    return {
        "unit": unit,
        "active_state": props.get("ActiveState", "unknown") if rc == 0 else "unknown",
        "sub_state": props.get("SubState", "unknown") if rc == 0 else "unknown",
        "start_timestamp": props.get("ExecMainStartTimestamp") or None,
        "n_restarts": int(props.get("NRestarts") or 0),
        "unit_file_state": props.get("UnitFileState") or None,
    }


async def health(port: int = DEFAULT_PORT) -> bool:
    """True when the Web UI answers on loopback. It serves plain HTML with no
    dedicated health route, so a 200 on `/` is the probe."""
    try:
        async with httpx.AsyncClient(timeout=_HEALTH_TIMEOUT) as client:
            r = await client.get(f"http://127.0.0.1:{port}/")
        return r.status_code == 200
    except httpx.HTTPError:
        return False


async def _wait_healthy(port: int) -> None:
    deadline = time.monotonic() + _START_WAIT_TIMEOUT
    while time.monotonic() < deadline:
        if await health(port):
            return
        await asyncio.sleep(1.0)
    raise HealthTimeoutError(f"{UNIT} started but 127.0.0.1:{port} never answered")


async def start(unit: str = UNIT, port: int = DEFAULT_PORT) -> dict:
    async with _exclusive():
        rc, _, err = await _run([_bin("systemctl"), "--user", "start", unit])
        if rc != 0:
            raise HarnessError(err.strip() or f"systemctl start {unit} failed (rc={rc})")
        await _wait_healthy(port)
        return await unit_state(unit)


async def stop(unit: str = UNIT) -> dict:
    async with _exclusive():
        rc, _, err = await _run([_bin("systemctl"), "--user", "stop", unit])
        if rc != 0:
            raise HarnessError(err.strip() or f"systemctl stop {unit} failed (rc={rc})")
        return await unit_state(unit)


async def restart(unit: str = UNIT, port: int = DEFAULT_PORT) -> dict:
    async with _exclusive():
        rc, _, err = await _run([_bin("systemctl"), "--user", "restart", unit])
        if rc != 0:
            raise HarnessError(err.strip() or f"systemctl restart {unit} failed (rc={rc})")
        await _wait_healthy(port)
        return await unit_state(unit)


# --------------------------------------------------------------------------- #
# settings.yaml: default model + provider catalog
# --------------------------------------------------------------------------- #

def settings_path() -> Path:
    return dsh_home() / "settings.yaml"


# ---- pi-ai's bundled provider catalog ---------------------------------------
# dsh's `llm-pi-ai` adapter ships @earendil-works/pi-ai, whose provider data
# lives as one JSON file per route under
# `<pkg>/dist/providers/data/<route>.json`, shaped
# `{<api>: {<model-id>: {id, name, …}}}`. A settings route that omits `models:`
# serves that file's models unchanged, so the picker has to read it to know what
# the route can actually run. Read-only, best-effort: every failure degrades to
# "no catalog models for this route", never to an exception.
PI_AI_CATALOG_DIR_ENV = "DISPATCH_PI_AI_CATALOG"
_PI_AI_REL = ("node_modules", "@earendil-works", "pi-ai", "dist", "providers")
# Route keys are file names — refuse anything that could escape the data dir.
_ROUTE_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
_CATALOG_MAX_BYTES = 4 * 1024 * 1024


def pi_ai_catalog_dir() -> Path | None:
    """`…/pi-ai/dist/providers`, or None when dsh/pi-ai is not installed.

    Resolved from the dsh binary (realpath → the package's own lib/), walking up
    so a hoisted node_modules layout is found too. Not cached: an install or an
    upgrade must be visible without restarting DisPatch.
    """
    override = os.environ.get(PI_AI_CATALOG_DIR_ENV, "").strip()
    if override:
        p = Path(override)
        return p if p.is_dir() else None
    binary = resolve_binary()
    if not binary:
        return None
    try:
        start = Path(binary).resolve()
    except (OSError, RuntimeError):
        return None
    for anc in [start, *start.parents]:
        cand = anc.joinpath(*_PI_AI_REL)
        if cand.is_dir():
            return cand
    return None


def _catalog_file(route: str) -> Path | None:
    if not (isinstance(route, str) and _ROUTE_RE.match(route)):
        return None
    base = pi_ai_catalog_dir()
    if base is None:
        return None
    f = base / "data" / f"{route}.json"
    return f if f.is_file() else None


def catalog_models(route: str) -> list[dict]:
    """[{id, name}] pi-ai ships for `route`, in catalog order ([] when unknown)."""
    f = _catalog_file(route)
    if f is None:
        return []
    try:
        if f.stat().st_size > _CATALOG_MAX_BYTES:
            log.warning("pi-ai catalog %s is implausibly large — ignored", f.name)
            return []
        data = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        log.warning("cannot read pi-ai catalog %s: %s", f.name, e)
        return []
    out: list[dict] = []
    seen: set[str] = set()
    if not isinstance(data, dict):
        return out
    for by_id in data.values():                 # one group per wire protocol
        if not isinstance(by_id, dict):
            continue
        for mid, spec in by_id.items():
            entry = _model_entry(spec if isinstance(spec, dict) else mid)
            if entry is None and isinstance(mid, str):
                entry = {"id": mid, "name": mid}
            if entry is None or entry["id"] in seen:
                continue
            seen.add(entry["id"])
            out.append(entry)
    return out


def catalog_display_name(route: str) -> str | None:
    """pi-ai's own display name for `route` (e.g. minimax → "MiniMax").

    The name lives in the provider module, not the data JSON, so this is a
    deliberately narrow read of `<route>.js` and returns None on any surprise —
    the caller falls back to the route key.
    """
    base = pi_ai_catalog_dir()
    if base is None or not (isinstance(route, str) and _ROUTE_RE.match(route)):
        return None
    f = base / f"{route}.js"
    try:
        if not f.is_file() or f.stat().st_size > _CATALOG_MAX_BYTES:
            return None
        head = f.read_text(encoding="utf-8", errors="replace")[:8192]
    except OSError:
        return None
    m = re.search(r'id:\s*"' + re.escape(route) + r'"\s*,\s*name:\s*"([^"\\\n]{1,64})"', head)
    return m.group(1) if m else None


def _load_settings(path: Path | None = None) -> dict:
    p = path or settings_path()
    try:
        with open(p, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except FileNotFoundError:
        return {}
    except (OSError, yaml.YAMLError) as e:
        raise HarnessError(f"cannot read {p.name}: {e}") from e
    if not isinstance(data, dict):
        raise HarnessError(f"{p.name}: top level must be a mapping")
    return data


# ---- Live model state ------------------------------------------------------
# A local llama.cpp-family server (StudioForge, LM Studio) decorates its
# OpenAI /models listing with `state` ("loaded" | "loading" | "not-loaded")
# and, when resident, `loaded_context_length`. Surfacing that in the picker is
# the difference between "instant reply" and "30s cold load" being a surprise.
#
# Probed ONLY for plain-http base URLs — those are LAN/tailnet servers the
# operator configured; https means a cloud API where an unauthenticated
# /models GET is at best noise. Best-effort with a short timeout and a small
# TTL cache so the picker stays snappy; a dead rig degrades to no badges, not
# an error.
_STATE_TTL_S = 10.0
_state_cache: dict[str, tuple[float, dict]] = {}


def _probe_model_states(base_url: str) -> dict:
    """{model_id: {"state": str, "ctx": int|None}} for servers that report it."""
    now = time.monotonic()
    hit = _state_cache.get(base_url)
    if hit and now - hit[0] < _STATE_TTL_S:
        return hit[1]
    states: dict[str, dict] = {}
    try:
        resp = httpx.get(base_url.rstrip("/") + "/models", timeout=2.0,
                         headers={"Accept": "application/json"})
        resp.raise_for_status()
        data = resp.json()
        for m in (data.get("data") or []):
            if isinstance(m, dict) and isinstance(m.get("id"), str) and isinstance(m.get("state"), str):
                states[m["id"]] = {"state": m["state"],
                                   "ctx": m.get("loaded_context_length")}
    except Exception as e:                       # any failure = no badges
        log.debug("model-state probe %s failed: %s", base_url, e)
    _state_cache[base_url] = (now, states)
    return states


def _model_entry(m) -> dict | None:
    if isinstance(m, str):
        return {"id": m, "name": m}
    if isinstance(m, dict) and isinstance(m.get("id"), str):
        return {"id": m["id"], "name": m.get("name") if isinstance(m.get("name"), str) else m["id"]}
    return None


def discover_models(path: Path | None = None) -> dict:
    """{providers: [{id, name, models: [{id, name}]}], current: {provider, model}|None}.
    Best-effort: an unreadable/malformed file yields dsh's shipped defaults and
    current=None; it never raises."""
    try:
        data = _load_settings(path)
    except HarnessError:
        data = {}
    providers: list[dict] = []

    ds = data.get("llm-deepseek") if isinstance(data.get("llm-deepseek"), dict) else {}
    ds_models = [e for e in (_model_entry(m) for m in (ds.get("models") or [])) if e] \
        if isinstance(ds.get("models"), list) else []
    if not ds_models and ds.get("models") != []:
        ds_models = [dict(m) for m in DEEPSEEK_DEFAULT_MODELS]
    providers.append({"id": DEEPSEEK_PROVIDER, "name": "DeepSeek", "models": ds_models})

    pi = data.get("llm-pi-ai") if isinstance(data.get("llm-pi-ai"), dict) else {}
    provs = pi.get("providers") if isinstance(pi.get("providers"), dict) else {}
    for pid, spec in provs.items():
        if not isinstance(pid, str) or not isinstance(spec, dict):
            continue
        if isinstance(spec.get("models"), list):
            # An explicit list REPLACES the installed catalog for this route.
            models = [e for e in (_model_entry(m) for m in spec["models"]) if e]
            catalogued = False
        else:
            # Omitted (or malformed) `models`: the route serves pi-ai's bundled
            # catalog, so that is what the picker must offer. `modelOverrides`
            # is only meaningful here and may rename catalog entries.
            models = catalog_models(pid)
            catalogued = bool(models)
            overrides = spec.get("modelOverrides")
            if isinstance(overrides, dict):
                for m in models:
                    ov = overrides.get(m["id"])
                    if isinstance(ov, dict) and isinstance(ov.get("name"), str):
                        m["name"] = ov["name"]
        if isinstance(spec.get("displayName"), str):
            name = spec["displayName"]
        else:
            name = catalog_display_name(pid) or pid
        base = spec.get("baseURL") if isinstance(spec.get("baseURL"), str) else ""
        if base.startswith("http://"):
            states = _probe_model_states(base)
            for m in models:
                st = states.get(m["id"])
                if st:
                    m["state"] = st["state"]
                    if st.get("ctx"):
                        m["ctx"] = st["ctx"]
        providers.append({"id": pid, "name": name, "models": models,
                          "from_catalog": catalogued})

    current = None
    adm = data.get("agent-default-model")
    if isinstance(adm, dict) and isinstance(adm.get("provider"), str) and isinstance(adm.get("model"), str):
        current = {"provider": adm["provider"], "model": adm["model"]}
    return {"providers": providers, "current": current}


# `agent-default-model:` at column 0, through to the next top-level key. Used to
# rewrite ONLY that block in place, so every other line of the file — including
# every comment — survives byte-for-byte.
_ADM_BLOCK_RE = re.compile(
    r"^agent-default-model:[ \t]*\n(?:(?:[ \t]+[^\n]*|[ \t]*#[^\n]*|[ \t]*)\n)*",
    re.M)


def _rewrite_adm_block(text: str, provider: str, model: str) -> str | None:
    """Replace provider/model inside the existing `agent-default-model:` block.

    Returns the new file text, or None if the block is not there in a shape we
    recognise (caller then appends one). Sibling keys in the block, such as
    `reasoningEffort`, are preserved along with their comments.
    """
    m = _ADM_BLOCK_RE.search(text)
    if not m:
        return None
    block = m.group(0)
    seen = {"provider": False, "model": False}
    out = []
    for line in block.splitlines(keepends=True):
        for key, value in (("provider", provider), ("model", model)):
            km = re.match(rf"^([ \t]+){key}:[ \t]*\S.*$", line.rstrip("\n"))
            if km:
                line = f"{km.group(1)}{key}: {value}\n"
                seen[key] = True
                break
        out.append(line)
    if not (seen["provider"] and seen["model"]):
        return None
    return text[:m.start()] + "".join(out) + text[m.end():]


def set_default_model(provider: str, model: str, path: Path | None = None) -> dict:
    """Rewrite `agent-default-model` in settings.yaml, preserving the file.

    Two properties this needs and did not used to have:

    * **Comments survive.** The old implementation round-tripped the whole file
      through PyYAML, which drops every comment. One model switch from the
      DisPatch pane erased the curated buildpc catalog note and the "JoyFox is
      deliberately absent: 0/5 on tool calls" warning — knowledge written down
      precisely so nobody repeats a known mistake. Now only the two scalars
      inside the `agent-default-model:` block are rewritten, in place.
    * **Concurrent writers cannot interleave.** The file is shared with every
      other dsh caller, and the old read-modify-write took no lock and used a
      fixed `.tmp` path, so simultaneous writes silently last-writer-wins (and
      two writers could fight over the same temp file). Now an exclusive flock
      on a sidecar covers read-through-replace, and the temp file is unique.

    dsh re-reads the file, so the change applies to the next new session in both
    the Web UI and headless runs. Agents should NOT call this: give a job its own
    scratch `DSH_HOME` instead (see the `dsh` skill). It exists for the
    human-facing picker, where the operator is deliberately choosing a shared
    default for everything on the machine.
    """
    if not (isinstance(provider, str) and _ID_RE.match(provider)):
        raise ValidationError("provider must match ^[A-Za-z0-9._:/-]{1,128}$")
    if not (isinstance(model, str) and _ID_RE.match(model)):
        raise ValidationError("model must match ^[A-Za-z0-9._:/-]{1,128}$")
    p = path or settings_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    header = ("# DeepSeek Harness (dsh) user settings — $DSH_HOME/settings.yaml\n"
              "# Hot-reloads: model/provider changes apply on the NEXT request.\n"
              "# Secrets never live here: apiKeyEnv is a reference resolved from\n"
              "# .credentials.yaml (0600) or the environment.\n"
              "# `agent-default-model` is managed by DisPatch's DeepSeek Harness pane too.\n")

    lock_path = p.with_name(p.name + ".lock")
    try:
        lock_fd = os.open(lock_path, os.O_WRONLY | os.O_CREAT, 0o600)
    except OSError as e:
        raise HarnessError(f"cannot lock {p.name}: {e}") from e
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        try:
            original = p.read_text(encoding="utf-8")
        except FileNotFoundError:
            original = ""
        except OSError as e:
            raise HarnessError(f"cannot read {p.name}: {e}") from e

        body = _rewrite_adm_block(original, provider, model) if original.strip() else None
        if body is None:
            # No recognisable block: keep every existing line and append one.
            # Falling back to a PyYAML round-trip here would reintroduce exactly
            # the comment loss this function exists to avoid.
            prefix = original
            if prefix and not prefix.endswith("\n"):
                prefix += "\n"
            if not prefix:
                prefix = header
            body = (f"{prefix}\nagent-default-model:\n"
                    f"  provider: {provider}\n  model: {model}\n")

        # Never write something dsh cannot read back.
        try:
            check = yaml.safe_load(body)
        except yaml.YAMLError as e:
            raise HarnessError(f"refusing to write unparseable {p.name}: {e}") from e
        adm = (check or {}).get("agent-default-model")
        if not (isinstance(adm, dict) and adm.get("provider") == provider
                and adm.get("model") == model):
            raise HarnessError(
                f"refusing to write {p.name}: the rewritten file does not read back "
                f"as {provider}/{model}")

        fd, tmp_name = tempfile.mkstemp(dir=str(p.parent), prefix=p.name + ".", suffix=".tmp")
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(body)
                f.flush()
                os.fsync(f.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, p)
        except OSError as e:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise HarnessError(f"cannot write {p.name}: {e}") from e
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)
    return {"provider": provider, "model": model}


# --------------------------------------------------------------------------- #
# Headless jobs
# --------------------------------------------------------------------------- #

def validate_task(task) -> str:
    if not isinstance(task, str) or not task.strip():
        raise ValidationError("task must be a non-empty string")
    if len(task) > TASK_MAX_CHARS:
        raise ValidationError(f"task must be at most {TASK_MAX_CHARS} characters")
    if "\x00" in task:
        raise ValidationError("task must not contain NUL")
    task = task.strip()
    # The task is a POSITIONAL argument to `dsh --profile headless <task>`.
    # A task beginning with a dash is parsed as a dsh OPTION instead of as
    # work (`--dump-default-config` really did run), so it is refused here.
    # We do not rely on a `--` separator: whether dsh's parser honours one is
    # its business, and this validation holds either way.
    if task.startswith("-"):
        raise ValidationError(
            "task must not start with '-' — it would be read as a dsh option")
    return task


def validate_cwd(cwd, home: Path | None = None) -> Path:
    """The job's working directory: an existing directory at or under $HOME
    (symlinks resolved, so `~/x/../../etc` and a link out of home both fail).
    None/'' means $HOME itself."""
    base = (home or Path.home()).resolve()
    if cwd in (None, "", "~"):
        return base
    if not isinstance(cwd, str) or "\x00" in cwd or len(cwd) > 1024:
        raise ValidationError("cwd must be a path string")
    raw = Path(os.path.expanduser(cwd))
    if not raw.is_absolute():
        raw = base / raw
    try:
        real = raw.resolve(strict=True)
    except (OSError, RuntimeError):
        raise ValidationError("cwd does not exist") from None
    if real != base and base not in real.parents:
        raise ValidationError("cwd must be inside the home directory")
    if not real.is_dir():
        raise ValidationError("cwd is not a directory")
    return real


def _job_env() -> dict[str, str]:
    env = dict(os.environ)
    env.pop("TMPDIR", None)              # see terminal._child_env: keep tool byproducts out of the data dir
    # Extra PATH entries for the job, colon-separated — for an install whose
    # node/npm prefix is outside the service's PATH. Unset = inherit unchanged.
    extra = [e for e in os.environ.get("DISPATCH_HARNESS_PATH", "").split(":") if e.strip()]
    if extra:
        path = env.get("PATH", "")
        seen = path.split(":") if path else []
        for entry in extra:
            if entry not in seen:
                seen.append(entry)
        env["PATH"] = ":".join(x for x in seen if x)
    env.setdefault("DSH_HOME", str(dsh_home()))
    return env


class HeadlessJob:
    """One `dsh --profile headless` run. Plain data + the process handle."""

    _seq = 0

    def __init__(self, task: str, cwd: Path):
        HeadlessJob._seq += 1
        self.id = HeadlessJob._seq
        self.task = task
        self.cwd = str(cwd)
        self.state = "queued"           # queued | running | done | failed | cancelled | timeout
        self.started_at: float | None = None
        self.ended_at: float | None = None
        self.exit_code: int | None = None
        self.output = ""
        self.error = ""
        self.cancel_requested = False
        self.overflowed = False         # hit JOB_OUTPUT_HARD_MAX and was killed
        self.proc: asyncio.subprocess.Process | None = None

    @property
    def active(self) -> bool:
        return self.state in ("queued", "running")

    def summary(self, with_output: bool = True) -> dict:
        d = {
            "id": self.id, "task": self.task, "cwd": self.cwd, "state": self.state,
            "started_at": self.started_at, "ended_at": self.ended_at,
            "exit_code": self.exit_code,
            "duration_s": (round((self.ended_at or time.time()) - self.started_at, 1)
                           if self.started_at else None),
        }
        if with_output:
            d["output"] = self.output
            d["error"] = self.error
        return d


class JobRunner:
    """Runs at most one headless job at a time; keeps a bounded history.
    State hooks fire on every start/end with a status() dict (the route layer
    broadcasts them). In-memory only — the history dies with the process."""

    def __init__(self, binary: str | None = None, timeout: float = JOB_TIMEOUT_DEFAULT):
        self._binary = binary                    # injectable for tests; None = resolve dsh
        self._timeout = timeout
        self._current: HeadlessJob | None = None
        self._history: list[HeadlessJob] = []
        self._hooks: list[Callable[[dict], None]] = []
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()

    # -- hooks -------------------------------------------------------------
    def add_state_hook(self, hook: Callable[[dict], None]) -> None:
        if hook not in self._hooks:
            self._hooks.append(hook)

    def remove_state_hook(self, hook: Callable[[dict], None]) -> None:
        with contextlib.suppress(ValueError):
            self._hooks.remove(hook)

    def _notify(self) -> None:
        st = self.status()
        for h in list(self._hooks):
            try:
                h(st)
            except Exception:
                log.exception("harness job hook failed")

    # -- status ------------------------------------------------------------
    def status(self) -> dict:
        cur = self._current
        return {
            "running": cur is not None and cur.active,
            "current": cur.summary(with_output=False) if cur else None,
            "history": [j.summary(with_output=False) for j in reversed(self._history)],
        }

    def jobs(self) -> list[dict]:
        out = [j.summary() for j in reversed(self._history)]
        if self._current is not None and self._current not in self._history:
            out.insert(0, self._current.summary())
        return out

    def job(self, job_id: int) -> dict | None:
        if self._current is not None and self._current.id == job_id:
            return self._current.summary()
        for j in self._history:
            if j.id == job_id:
                return j.summary()
        return None

    # -- lifecycle ---------------------------------------------------------
    async def submit(self, task: str, cwd: Path) -> dict:
        binary = self._binary or resolve_binary()
        if binary is None:
            raise HarnessError("dsh is not installed (npm i -g @deepseek-ai/dsh)")
        async with self._lock:
            if self._current is not None and self._current.active:
                raise HarnessBusyError("a job is already running")
            job = HeadlessJob(task, cwd)
            self._current = job
            self._history.append(job)
            del self._history[:-JOB_HISTORY_MAX]
            self._task = asyncio.get_running_loop().create_task(self._run(job, binary))
        return job.summary()

    async def cancel(self) -> dict:
        job = self._current
        if job is None or not job.active:
            raise HarnessError("no job is running")
        # The final state is written by _run when the process actually ends
        # (SIGTERM, then SIGKILL after the grace period) — cancel only asks.
        job.cancel_requested = True
        if job.proc is not None:
            self._signal_group(job.proc, signal.SIGTERM)
            self._kill_later(job.proc)
        return job.summary(with_output=False)

    def _kill_later(self, proc: asyncio.subprocess.Process) -> None:
        async def _escalate():
            try:
                await asyncio.wait_for(proc.wait(), timeout=_STOP_GRACE)
            except TimeoutError:
                self._signal_group(proc, signal.SIGKILL)
        asyncio.get_running_loop().create_task(_escalate())

    async def shutdown(self) -> None:
        self._hooks.clear()
        job = self._current
        if job is not None and job.active:
            job.cancel_requested = True
            if job.proc is not None:
                self._signal_group(job.proc, signal.SIGKILL)
        if self._task is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._task, timeout=5)

    @staticmethod
    async def _drain(stream, on_overflow) -> bytes:
        """Read a pipe to EOF keeping only its last JOB_OUTPUT_MAX bytes.

        Memory is bounded by the tail, not by what the child chooses to write.
        Crossing JOB_OUTPUT_HARD_MAX calls ``on_overflow`` (which kills the
        process group) and stops reading — the other pipe then reaches EOF
        instead of deadlocking behind a full buffer.
        """
        buf = bytearray()
        total = 0
        while True:
            try:
                chunk = await stream.read(_READ_CHUNK)
            except (ValueError, OSError):        # pipe torn down under us
                break
            if not chunk:
                break
            total += len(chunk)
            buf.extend(chunk)
            if len(buf) > JOB_OUTPUT_MAX:
                del buf[:len(buf) - JOB_OUTPUT_MAX]
            if total > JOB_OUTPUT_HARD_MAX:
                on_overflow()
                break
        return bytes(buf)

    async def _collect(self, proc: asyncio.subprocess.Process,
                       job: HeadlessJob) -> tuple[bytes, bytes]:
        """Both pipes, tail-only, then reap. Never buffers the whole output."""
        def _overflow() -> None:
            if not job.overflowed:
                job.overflowed = True
                log.warning("harness job %s exceeded %d bytes of output — killed",
                            job.id, JOB_OUTPUT_HARD_MAX)
            self._signal_group(proc, signal.SIGKILL)

        out, err = await asyncio.gather(
            self._drain(proc.stdout, _overflow),
            self._drain(proc.stderr, _overflow),
        )
        # A native child may outlive the wrapper, so the group still needs a
        # sweep — but a blanket killpg is only safe while the LEADER is
        # unreaped (after that its pid, and so the group id, can be recycled
        # onto a stranger). _sweep_group picks whichever is correct.
        self._sweep_group(proc, signal.SIGKILL)
        await proc.wait()
        return out, err

    async def _run(self, job: HeadlessJob, binary: str) -> None:
        argv = [binary, "--profile", "headless", job.task]
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv, cwd=job.cwd, env=_job_env(),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
        except OSError as e:
            job.state = "failed"
            job.error = f"failed to launch dsh: {e}"
            job.started_at = job.ended_at = time.time()
            self._notify()
            return
        job.proc = proc
        job.state = "running"
        job.started_at = time.time()
        if job.cancel_requested:            # cancelled while still queued
            self._signal_group(proc, signal.SIGTERM)
            self._kill_later(proc)
        log.info("harness job %s started (pid %s, cwd %s)", job.id, proc.pid, job.cwd)
        self._notify()
        # Shielded so a timeout does NOT cancel the readers: the drain has to
        # keep running (and stay bounded) while we escalate signals, or the
        # child blocks on a full pipe instead of dying.
        collect = asyncio.ensure_future(self._collect(proc, job))
        try:
            out, err = await asyncio.wait_for(asyncio.shield(collect),
                                              timeout=self._timeout)
        except TimeoutError:
            self._signal_group(proc, signal.SIGTERM)
            try:
                out, err = await asyncio.wait_for(asyncio.shield(collect),
                                                  timeout=_STOP_GRACE)
            except TimeoutError:
                self._signal_group(proc, signal.SIGKILL)
                out, err = await collect
            if not job.cancel_requested:
                job.state = "timeout"
        job.exit_code = proc.returncode
        job.output = out.decode("utf-8", "replace")[-JOB_OUTPUT_MAX:]
        job.error = err.decode("utf-8", "replace")[-JOB_OUTPUT_MAX:]
        if job.overflowed:
            job.error = (job.error + "\n[output ceiling reached — job killed]").strip()
        if job.cancel_requested:
            job.state = "cancelled"
        elif job.state == "running":
            job.state = "done" if proc.returncode == 0 else "failed"
        job.ended_at = time.time()
        log.info("harness job %s %s (rc=%s, %.1fs)", job.id, job.state, proc.returncode,
                 job.ended_at - job.started_at)
        self._notify()

    @staticmethod
    def _sweep_group(proc: asyncio.subprocess.Process, sig: int) -> None:
        """Signal the job's process group, safely on both sides of the reap."""
        if proc.returncode is None:          # leader still ours: group id is safe
            JobRunner._signal_group(proc, sig)
            return
        for pid in _group_members(proc.pid):
            with contextlib.suppress(OSError):
                os.kill(pid, sig)

    @staticmethod
    def _signal_group(proc: asyncio.subprocess.Process, sig: int) -> None:
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            pass
        except (PermissionError, OSError):
            with contextlib.suppress(ProcessLookupError):
                proc.send_signal(sig)


# The one live runner (in-memory only; dies with the process).
runner = JobRunner()
