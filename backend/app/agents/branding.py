"""Branding and content-policy helpers for the agent feature.

Two small, pure rule sets that tests and the UI layer share:

1. BRANDING (RESEARCH.md F3, section 5 item 3). The backend may be labelled
   "Claude Agent" in pickers, "Claude" inside a menu labelled "Agents", or
   "<Name> Powered by Claude". The Anthropic CLI product name, its "Agent" form,
   and the Cowork product names are NOT permitted, and nothing may look like an
   Anthropic product. The forbidden strings are assembled from fragments below
   so this file (which the branding lint scans) does not contain them.

2. QUARANTINE (brief: "an agent that reads untrusted content does not also hold
   high-privilege tools; split into a reader agent and an actor agent with an
   approval gate between them"). :func:`quarantine_violations` is the single
   implementation; ``AgentSpec`` runs it on construction, so a violating spec
   cannot exist.
"""
from __future__ import annotations

import fnmatch
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# --------------------------------------------------------------------------- #
# Branding
# --------------------------------------------------------------------------- #

_PRODUCT = "Claude"
# Built from parts on purpose: see the module docstring.
FORBIDDEN_LABELS: tuple[str, ...] = (
    _PRODUCT + " " + "Co" + "de",
    _PRODUCT + " " + "Co" + "de" + " Agent",
    _PRODUCT + " " + "Co" + "work",
    _PRODUCT + " " + "Co" + "work" + " Agent",
)
# One pattern covers the family, any case, any amount of (non-breaking) space.
# Underscore/hyphen forms (env var names, package names) are deliberately NOT
# matched: they are identifiers, not UI labels.
_FORBIDDEN_RE = re.compile(
    r"\b" + _PRODUCT + r"[  ]+(?:Co" + r"de|Co" + r"work)\b", re.IGNORECASE)

DEFAULT_PRODUCT_LABEL = "DisPatch Agent"     # RESEARCH.md section 6 question 5 (proposed)


def allowed_labels(product_name: str = DEFAULT_PRODUCT_LABEL) -> dict[str, str]:
    """The labels a UI may put on the Claude backend, with where each is allowed."""
    return {
        "Claude Agent": "anywhere an agent backend is picked (dropdowns, badges)",
        "Claude": 'only inside a menu or section that is itself labelled "Agents"',
        f"{product_name} Powered by Claude": "product-level naming, e.g. the setup screen header",
    }


def is_allowed_agent_label(label: str, *, in_agents_menu: bool = False,
                           product_name: str = DEFAULT_PRODUCT_LABEL) -> bool:
    label = label.strip()
    if label == "Claude Agent":
        return True
    if label == "Claude":
        return in_agents_menu
    return label == f"{product_name} Powered by Claude"


@dataclass(frozen=True)
class BrandingViolation:
    source: str          # file path (as given) or logical name
    line: int
    text: str            # the offending line, trimmed
    match: str


def scan_text(text: str, source: str = "<text>") -> list[BrandingViolation]:
    out: list[BrandingViolation] = []
    for n, line in enumerate(text.splitlines(), 1):
        for m in _FORBIDDEN_RE.finditer(line):
            out.append(BrandingViolation(source, n, line.strip()[:200], m.group(0)))
    return out


def _json_strings(obj: Any) -> Iterable[str]:
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for k, v in obj.items():
            yield str(k)
            yield from _json_strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _json_strings(v)


def scan_locale_file(path: Path) -> list[BrandingViolation]:
    """Every key and value of a locale JSON (a UI string is a UI string)."""
    data = json.loads(path.read_text(encoding="utf-8"))
    out: list[BrandingViolation] = []
    for s in _json_strings(data):
        out.extend(scan_text(s, str(path)))
    return out


def scan_tree(root: Path, patterns: Iterable[str] = ("*.py", "*.json", "*.md", "*.html", "*.js")
              ) -> list[BrandingViolation]:
    out: list[BrandingViolation] = []
    for pat in patterns:
        for p in sorted(Path(root).rglob(pat)):
            if "__pycache__" in p.parts:
                continue
            try:
                text = p.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            out.extend(scan_text(text, str(p)))
    return out


# --------------------------------------------------------------------------- #
# Quarantine (reader vs actor)
# --------------------------------------------------------------------------- #

# Tools that change the world. Patterns are fnmatch, case-insensitive, matched
# against the full tool name (MCP tools are mcp__<server>__<tool>).
HIGH_PRIVILEGE_TOOL_PATTERNS: tuple[str, ...] = (
    "Bash", "PowerShell", "Write", "Edit", "MultiEdit", "NotebookEdit",
    "mcp__*__write*", "mcp__*__delete*", "mcp__*__remove*", "mcp__*__admin*",
    "mcp__*__exec*", "mcp__*__send*", "mcp__*__post*", "mcp__*__create*",
    "mcp__*__update*",
)

# Tools that pull untrusted text into the model's context. Holding one means
# the agent reads untrusted content whether or not its author said so.
UNTRUSTED_SOURCE_TOOL_PATTERNS: tuple[str, ...] = ("WebFetch", "WebSearch", "mcp__*__fetch*",
                                                   "mcp__*__browse*", "mcp__*__read_mail*",
                                                   "mcp__*__read_email*")


def tool_matches(name: str, patterns: Iterable[str]) -> bool:
    low = name.lower()
    return any(fnmatch.fnmatchcase(low, p.lower()) for p in patterns)


def effective_reads_untrusted(spec: Any) -> bool:
    """Declared OR implied by the allowlist (a web tool is an untrusted source)."""
    if getattr(spec, "reads_untrusted_content", False):
        return True
    return any(tool_matches(t, UNTRUSTED_SOURCE_TOOL_PATTERNS) for t in spec.tool_allowlist)


def high_privilege_tools_of(spec: Any) -> list[str]:
    """Every allowed tool / MCP server that counts as high-privilege."""
    extra = list(getattr(spec, "high_privilege_tools", []) or [])
    found = [t for t in spec.tool_allowlist
             if tool_matches(t, HIGH_PRIVILEGE_TOOL_PATTERNS) or tool_matches(t, extra)]
    for name, srv in (getattr(spec, "mcp_servers", {}) or {}).items():
        if getattr(srv, "privileged", False):
            found.append(f"mcp:{name}")
    return found


def quarantine_violations(spec: Any) -> list[str]:
    """Human-readable reasons ``spec`` breaks the quarantine rule (empty = fine)."""
    if not effective_reads_untrusted(spec):
        return []
    hp = high_privilege_tools_of(spec)
    if not hp:
        return []
    return [
        "an agent that reads untrusted content must not hold high-privilege tools "
        f"({', '.join(sorted(set(hp)))}); split it into a reader agent and an actor agent "
        "with an approval gate between them"
    ]
