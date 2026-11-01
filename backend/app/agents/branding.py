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
# Repository-wide scan (issue #20): every surface a user or a reader sees
# --------------------------------------------------------------------------- #

# Repository-relative globs. ``.js`` files are scanned by their STRING LITERALS
# only (a UI label lives in a string; a code comment is not shipped text); every
# other file is scanned line by line. ``**`` is recursive.
REPO_SCAN_GLOBS: tuple[str, ...] = (
    "frontend/static/locales/*.json",
    "frontend/static/js/**/*.js",
    "frontend/static/index.html",
    "apps/*/static/**/*.js",
    "apps/*/static/**/*.html",
    "apps/*/static/**/*.json",
    "apps/*/locales/*.json",
    "backend/app/agents/**/*.py",
    "README.md",
    "CHANGELOG.md",
    "docs/**/*.md",
)
# Design notes and research quote product names on purpose (they discuss the
# label rules themselves); they are not user-facing.
REPO_SCAN_EXCLUDE: tuple[str, ...] = ("docs/design/",)

# Quoted history that may keep a forbidden name (e.g. a CHANGELOG entry quoting
# an old label it removed). Each entry is (repository-relative path, a fragment
# that must appear on the offending line). The fragment must NOT itself contain
# a forbidden label (this file is scanned). An entry that no longer matches any
# hit is reported as stale by :func:`stale_allowlist_entries`, so the list
# cannot silently grow into a loophole.
BRANDING_ALLOWLIST: tuple[tuple[str, str], ...] = ()


_JS_KEYWORDS_BEFORE_EXPR = frozenset({
    "return", "typeof", "case", "do", "else", "in", "of", "new", "delete", "void",
    "throw", "instanceof", "yield", "await"})


def _js_regex_may_start(last: str, word: str) -> bool:
    """A ``/`` starts a regex literal unless the previous token is a value."""
    if not last:
        return True
    if last.isalnum() or last in "_$":
        return word in _JS_KEYWORDS_BEFORE_EXPR
    return last not in ")]"     # after a value-closing bracket it is a division


def js_string_literals(src: str) -> list[tuple[int, str]]:
    """(line, text) of every string and template literal in ``src``.

    A small lexer, not a parser: it skips ``//`` and ``/* */`` comments and regex
    literals (best effort, by the previous token) so a quote inside them does not
    desynchronise it. Template ``${...}`` expressions are kept in the text, which
    can only add hits, never hide one.
    """
    out: list[tuple[int, str]] = []
    i, n, line = 0, len(src), 1
    last, word = "", ""  # last significant character / identifier, for the regex heuristic
    while i < n:
        c = src[i]
        if c == "\n":
            line += 1
            i += 1
            continue
        if c.isspace():
            i += 1
            continue
        if src.startswith("//", i):
            j = src.find("\n", i)
            i = n if j < 0 else j
            continue
        if src.startswith("/*", i):
            j = src.find("*/", i + 2)
            j = n if j < 0 else j + 2
            line += src.count("\n", i, j)
            i = j
            continue
        if c in "'\"`":
            start_line, j, buf = line, i + 1, []
            while j < n and src[j] != c:
                if src[j] == "\\" and j + 1 < n:
                    buf.append(src[j:j + 2])
                    if src[j + 1] == "\n":
                        line += 1
                    j += 2
                    continue
                if src[j] == "\n":
                    line += 1
                    if c != "`":
                        break        # unterminated: stop at the line end
                buf.append(src[j])
                j += 1
            out.append((start_line, "".join(buf)))
            i = j + 1
            last, word = ")", ""     # a string is a value
            continue
        if c == "/" and _js_regex_may_start(last, word):
            j, in_class = i + 1, False
            while j < n and src[j] != "\n":
                if src[j] == "\\":
                    j += 2
                    continue
                if src[j] == "[":
                    in_class = True
                elif src[j] == "]":
                    in_class = False
                elif src[j] == "/" and not in_class:
                    break
                j += 1
            i = j + 1
            last, word = ")", ""     # a regex is a value
            continue
        if c.isalnum() or c in "_$":
            j = i
            while j < n and (src[j].isalnum() or src[j] in "_$"):
                j += 1
            word, last = src[i:j], src[j - 1]
            i = j
            continue
        last, word = c, ""
        i += 1
    return out


def scan_js_file(path: Path) -> list[BrandingViolation]:
    """Forbidden labels inside the string literals of a JavaScript file."""
    out: list[BrandingViolation] = []
    for ln, s in js_string_literals(Path(path).read_text(encoding="utf-8")):
        for v in scan_text(s, str(path)):
            out.append(BrandingViolation(str(path), ln + v.line - 1, v.text, v.match))
    return out


def scan_file(path: Path) -> list[BrandingViolation]:
    """Scan one file the way its type calls for (JS literals, locale JSON, or lines)."""
    p = Path(path)
    if p.suffix == ".js":
        return scan_js_file(p)
    text = p.read_text(encoding="utf-8")
    return scan_text(text, str(p))


def repo_scan_files(repo: Path) -> list[Path]:
    """Every file the repository-wide lint covers, sorted, without duplicates."""
    repo = Path(repo)
    seen: dict[Path, None] = {}
    for pat in REPO_SCAN_GLOBS:
        for p in sorted(repo.glob(pat)):
            if not p.is_file() or "__pycache__" in p.parts or "node_modules" in p.parts:
                continue
            rel = p.relative_to(repo).as_posix()
            if any(rel.startswith(x) for x in REPO_SCAN_EXCLUDE):
                continue
            seen.setdefault(p, None)
    return list(seen)


def _allowed(rel: str, v: BrandingViolation,
             allowlist: Iterable[tuple[str, str]]) -> bool:
    return any(rel == path and frag in v.text for path, frag in allowlist)


def scan_repo_file(repo: Path, path: Path,
                   allowlist: Iterable[tuple[str, str]] = BRANDING_ALLOWLIST
                   ) -> list[BrandingViolation]:
    """Violations in one repository file, minus the allow-listed quoted history."""
    rel = Path(path).relative_to(repo).as_posix()
    allow = tuple(allowlist)
    return [v for v in scan_file(path) if not _allowed(rel, v, allow)]


def scan_repo(repo: Path, allowlist: Iterable[tuple[str, str]] = BRANDING_ALLOWLIST
              ) -> list[BrandingViolation]:
    allow = tuple(allowlist)
    out: list[BrandingViolation] = []
    for p in repo_scan_files(repo):
        out.extend(scan_repo_file(repo, p, allow))
    return out


def stale_allowlist_entries(repo: Path,
                            allowlist: Iterable[tuple[str, str]] = BRANDING_ALLOWLIST
                            ) -> list[tuple[str, str]]:
    """Allow-list entries that no longer excuse any hit (delete them)."""
    repo = Path(repo)
    stale = []
    for path, frag in allowlist:
        p = repo / path
        hits = scan_file(p) if p.is_file() else []
        if not any(frag in v.text for v in hits):
            stale.append((path, frag))
    return stale


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
