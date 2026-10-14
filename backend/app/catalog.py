"""The "Works with DisPatch" catalog: app/catalog.yaml plus live status.

The entries are data (catalog.yaml); the status of each comes from a PROBE, a
small function main.py supplies (it owns the availability checks). A probe
returns ``(status, detail)`` where status is one of:

* ``connected``      configured and in use
* ``installed``      present, but switched off or not yet set up inside
* ``not_configured`` nothing set up (the card says how)
* ``bundled``        ships with DisPatch
* ``unknown``        no probe

Nothing here makes a network call: status is what DisPatch already knows.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from functools import lru_cache
from pathlib import Path

import yaml

log = logging.getLogger("local-chat.catalog")

CATALOG_PATH = Path(__file__).resolve().parent / "catalog.yaml"
DOCS_URL = "https://github.com/LaserLloyd/DisPatch_Chat/blob/main/docs/integrations.md"
STATUSES = ("connected", "installed", "not_configured", "bundled", "unknown")
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$")

Probe = Callable[[str], tuple[str, str]]


@lru_cache(maxsize=1)
def entries() -> tuple[dict, ...]:
    """The validated entries. A malformed entry is skipped with a log line."""
    try:
        doc = yaml.safe_load(CATALOG_PATH.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        log.exception("catalog.yaml is unreadable")
        return ()
    out = []
    for e in doc.get("entries") or []:
        if not isinstance(e, dict) or not _ID_RE.match(str(e.get("id") or "")) \
                or not e.get("name") or not e.get("summary"):
            log.warning("catalog: skipping a malformed entry %r", e)
            continue
        repo = str(e.get("repo") or "")
        if repo and not repo.startswith("https://github.com/"):
            log.warning("catalog: %s repo is not a public GitHub URL", e["id"])
            repo = ""
        out.append({
            "id": str(e["id"]), "name": str(e["name"]), "summary": str(e["summary"]),
            "repo": repo, "repo_note": str(e.get("repo_note") or ""),
            "connects": str(e.get("connects") or ""),
            "probe": str(e.get("probe") or ""),
            "toggle": str(e.get("toggle") or ""),
            "guide": bool(e.get("guide")),
        })
    return tuple(out)


def resolve(probes: dict[str, Probe]) -> list[dict]:
    """Every entry with its live status. A probe that raises reads as unknown."""
    rows = []
    for e in entries():
        name, _, arg = e["probe"].partition(":")
        status, detail = "unknown", ""
        fn = probes.get(name)
        if name == "bundled":
            status = "bundled"
        elif fn is not None:
            try:
                status, detail = fn(arg)
            except Exception:
                log.exception("catalog probe %s failed", e["probe"])
                status, detail = "unknown", ""
        if status not in STATUSES:
            status = "unknown"
        rows.append({k: v for k, v in e.items() if k != "probe"}
                    | {"status": status, "detail": detail,
                       "docs": f"{DOCS_URL}#{e['id']}"})
    return rows
