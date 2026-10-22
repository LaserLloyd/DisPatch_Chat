"""Settings -> Plugins: one place that answers "what is installed and what is on".

Contract: docs/design/platform-2-3.md section 3. Three routes, all operator-only
(a non-Admin or machine caller gets the same 403 the other admin routes give;
the route table's manage_plugins also bars the prefix one layer earlier):

  * ``GET  /api/plugins`` -- extensions (the host's registry plus the operator's
    on/off), the Tools rows (the same ones ``/api/tools`` returns), the
    built-in lookup set, and whether the voice add-on is installed.
  * ``PUT  /api/plugins/extensions/{id}`` -- ``{enabled}`` -> ``<data>/extensions.yaml``
    ``{disabled: [ids]}``. Takes effect at the next start (``discover()`` loads a
    listed id as state ``disabled``); the answer says ``restart_required``.
  * ``PUT  /api/plugins/livetools`` -- ``{enabled}`` -> tools.yaml
    ``livetools: {enabled}``. Effective at once: ``tools.livetools_enabled()`` is
    what the advisor consults before offering any tool.
  * ``GET  /api/plugins/manifests`` -- (3.0) every extension, app and page/URL tool as one
    Tool manifest, with the permissions it requests (``tool_manifests.py``).

Turning on a plugin that asks for permissions and was not running at the 3.0 upgrade needs
``{"enabled": true, "approve": [<the permissions it requests>]}``; without it the answer is a 409
``plugin_needs_approval`` listing them. ``manage_plugins`` is required for all of it.

The module does not import ``main`` (main includes this router).
"""
from __future__ import annotations

import contextlib
import logging
import os
import tempfile

import yaml
from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from . import accounts, config, extensions, livetools, policy, tool_manifests, tools

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


@router.get("/api/plugins/manifests")
def api_plugin_manifests(_=_gate):
    """Every plugin as a Tool manifest, with the permissions it requests and whether they were approved."""
    return {"manifests": tool_manifests.list_manifests(),
            "permission_labels": tool_manifests.PERMISSION_LABELS}


def _enabled_flag(payload) -> bool:
    if not isinstance(payload, dict) or not (set(payload) == {"enabled"} or set(payload) == {"enabled", "approve"}) \
            or not isinstance(payload["enabled"], bool):
        raise HTTPException(422, "body must be {enabled: true|false}")
    return payload["enabled"]


def _write_off_list(ids: list[str]) -> None:
    path = config.DATA_DIR / extensions.USER_OFF_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    text = yaml.safe_dump({"disabled": sorted(set(ids))}, sort_keys=False)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{extensions.USER_OFF_FILE}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
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
        text = path.read_text(encoding="utf-8")
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
def api_plugin_extension(request: Request, ext_id: str, payload: dict = Body(...), _=_gate):
    enabled = _enabled_flag(payload)
    if not extensions.ID_RE.match(ext_id) or not any(e.id == ext_id for e in extensions.registry()):
        raise HTTPException(404, "No such extension")
    approve = payload.get("approve")
    if approve is not None and (not isinstance(approve, list) or not all(isinstance(a, str) for a in approve)):
        raise HTTPException(422, "approve must be the list of permissions you reviewed")
    if enabled:
        try:
            tool_manifests.check_enable(ext_id, approve)
        except tool_manifests.PluginError as e:
            body = {"detail": str(e), "error": {"code": e.code, "message": str(e),
                    "fix": "Review the permissions, then enable it again with approve set to that list.",
                    "docs": e.code}, **e.extra}
            return JSONResponse(body, status_code=e.status)
        row = tool_manifests.get_manifest(ext_id)
        if row is not None and not row["approved"] and row["manifest"]["permissions_requested"]:
            who = policy.of(request).principal.profile_id
            tool_manifests.approve(ext_id, row["manifest"]["permissions_requested"], by=who or "")
            accounts.log_activity("plugin.approve", profile_id=who, subject=ext_id,
                                  detail=",".join(row["manifest"]["permissions_requested"])[:280],
                                  category="audit")
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
