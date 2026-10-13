"""Settings -> Plugins: one place that answers "what is installed and what is on".

Contract: docs/design/platform-2-3.md section 3. Three routes, all operator-only
(a Safe-Mode or machine caller gets the same 403 the other admin routes give;
``main._decoy_blocked`` also bars the prefix one layer earlier):

  * ``GET  /api/plugins`` -- extensions (the host's registry plus the operator's
    on/off), the Tools rows (the same ones ``/api/tools`` returns), the
    built-in lookup set, and whether the voice add-on is installed.
  * ``PUT  /api/plugins/extensions/{id}`` -- ``{enabled}`` -> ``<data>/extensions.yaml``
    ``{disabled: [ids]}``. Takes effect at the next start (``discover()`` loads a
    listed id as state ``disabled``); the answer says ``restart_required``.
  * ``PUT  /api/plugins/livetools`` -- ``{enabled}`` -> tools.yaml
    ``livetools: {enabled}``. Effective at once: ``tools.livetools_enabled()`` is
    what the advisor consults before offering any tool.

The module does not import ``main`` (main includes this router).
"""
from __future__ import annotations

import contextlib
import logging
import os
import tempfile

import yaml
from fastapi import APIRouter, Body, Depends, HTTPException

from . import config, extensions, livetools, tools

log = logging.getLogger("local-chat.plugins")

router = APIRouter()

#: The voice add-on's install command, as js/voice-absent.js COMMANDS.install
#: types it. A test pins the two together (js/about.js holds the repo URL).
VOICE_REPO_URL = "https://github.com/LaserLloyd/DisPatch_Voice"
VOICE_TAG = "v1"
VOICE_INSTALL_CMD = (
    f'uv pip install --python .venv/bin/python "dispatch-voice @ git+{VOICE_REPO_URL}@{VOICE_TAG}"')

_gate = Depends(extensions.require_operator)


def _extension_rows(off: set[str]) -> list[dict]:
    rows = []
    for e in extensions.registry():
        row = e.public(operator=True)
        rows.append({
            "id": e.id, "name": e.name or e.id, "version": e.version, "state": e.state,
            # What the switch shows: the operator's choice, which differs from
            # `state` between a toggle and the next restart.
            "enabled": e.id not in off,
            # The choice and the running state disagree until the next start.
            "restart_pending": ((e.id in off and e.state != "disabled")
                                or (e.id not in off and e.state == "disabled"
                                    and e.detail == extensions.USER_OFF_DETAIL)),
            "source": row["source"],
            "detail": row.get("detail", ""),
        })
    return rows


def _voice_block(rows: list[dict]) -> dict:
    row = next((r for r in rows if r["id"] == "voice"), None)
    out: dict = {"installed": row is not None, "install_cmd": VOICE_INSTALL_CMD}
    if row is not None:
        out["version"] = row["version"]
        out["state"] = row["state"]
    return out


@router.get("/api/plugins")
def api_plugins(_=_gate):
    rows = _extension_rows(extensions.user_disabled())
    return {
        "extensions": rows,
        "tools": tools.list_out(True),
        "livetools": {"enabled": tools.livetools_enabled(), "names": list(livetools.TOOL_NAMES)},
        "voice": _voice_block(rows),
    }


def _enabled_flag(payload) -> bool:
    if not isinstance(payload, dict) or set(payload) != {"enabled"} \
            or not isinstance(payload["enabled"], bool):
        raise HTTPException(422, "body must be {enabled: true|false}")
    return payload["enabled"]


def _write_off_list(ids: list[str]) -> None:
    path = config.DATA_DIR / extensions.USER_OFF_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    text = yaml.safe_dump({"disabled": sorted(set(ids))}, sort_keys=False)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{extensions.USER_OFF_FILE}.")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _read_off_list_strict() -> set[str]:
    """The ids switched off, from ``<data>/extensions.yaml`` — or ValueError when
    the file exists but is not ``{disabled: [ids]}``. The reader the loader uses
    (`extensions.user_disabled`) treats junk as "nothing off", which is right at
    boot; a WRITE must not turn that into a file that drops every other id."""
    path = config.DATA_DIR / extensions.USER_OFF_FILE
    try:
        text = path.read_text()
    except FileNotFoundError:
        return set()
    except OSError as e:
        raise ValueError(str(e)) from e
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise ValueError(f"not valid YAML: {e}") from e
    if doc is None:
        return set()
    if not isinstance(doc, dict) or set(doc) - {"disabled"}:
        raise ValueError("expected {disabled: [ids]}")
    ids = doc.get("disabled") or []
    if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
        raise ValueError("`disabled` must be a list of ids")
    return set(ids)


@router.put("/api/plugins/extensions/{ext_id}")
def api_plugin_extension(ext_id: str, payload: dict = Body(...), _=_gate):
    enabled = _enabled_flag(payload)
    if not extensions.ID_RE.match(ext_id) or not any(e.id == ext_id for e in extensions.registry()):
        raise HTTPException(404, "No such extension")
    try:
        off = _read_off_list_strict()
    except ValueError as e:
        raise HTTPException(409, f"extensions.yaml cannot be read ({e}); fix it by hand first") from e
    if enabled:
        off.discard(ext_id)
    else:
        off.add(ext_id)
    _write_off_list(list(off))
    log.info("plugins: extension %s switched %s (effective after a restart)",
             ext_id, "on" if enabled else "off")
    return {"id": ext_id, "enabled": enabled, "restart_required": True}


@router.put("/api/plugins/livetools")
def api_plugin_livetools(payload: dict = Body(...), _=_gate):
    enabled = _enabled_flag(payload)
    try:
        tools.set_livetools_enabled(enabled)
    except ValueError as e:
        raise HTTPException(409, f"tools.yaml cannot be read, fix it first: {e}") from e
    return {"enabled": tools.livetools_enabled(), "names": list(livetools.TOOL_NAMES),
            "restart_required": False}
