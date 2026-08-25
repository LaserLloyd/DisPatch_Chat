"""Guards for the two lint faults that took CI red on 2026-08-25.

CI runs `uvx ruff check app tests` — an UNPINNED ruff, whose rule set moves
under us. Neither guard here replaces that run; each pins the specific mistake
that produced the three errors, in a form that costs no network and no ruff.

Fault 1 (RUF100 ×2): a suppression comment naming a rule this project does not
enable. Ruff reports an unused suppression, so writing one to be "safe" is not
free — it is a CI failure. The `why` belongs in prose instead.

Fault 2 (I001): `from .reactions import (...)` written in the hanging-indent
style ruff's isort does not emit, so every fresh checkout was one `--fix` away
from a diff.
"""
from __future__ import annotations

import re
import tomllib
from pathlib import Path

APP = Path(__file__).resolve().parent.parent / "app"
PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"

# Matches "noqa: E501" / "noqa: E501, B904" — an explicit code list only.
# A blanket bare suppression names no rule and is out of scope here.
_NOQA = re.compile(r"#\s*noqa:\s*([A-Z]+[0-9]+(?:\s*,\s*[A-Z]+[0-9]+)*)")
_RULE = re.compile(r"^([A-Z]+)([0-9]+)$")


def _enabled_families() -> tuple[set[str], set[str]]:
    cfg = tomllib.loads(PYPROJECT.read_text())["tool"]["ruff"]["lint"]
    return set(cfg["select"]), set(cfg["ignore"])


def test_no_suppression_names_a_rule_ruff_does_not_enable():
    """A suppression for a non-selected (or globally ignored) rule is dead — and
    ruff FAILS on it. app/pool_guard.py carried `S310` (bandit: not selected)
    and `BLE001` (selected via B, then ignored project-wide) and took CI red."""
    selected, ignored = _enabled_families()
    offenders: list[str] = []
    for path in sorted(APP.rglob("*.py")):
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            m = _NOQA.search(line)
            if not m:
                continue
            for code in (c.strip() for c in m.group(1).split(",")):
                rm = _RULE.match(code)
                if not rm:
                    continue
                family = rm.group(1)
                # Enabled means the family (or the exact code) is selected AND
                # the exact code is not on the project-wide ignore list.
                live = (family in selected or code in selected) and code not in ignored
                if not live:
                    offenders.append(f"{path.name}:{lineno}: # noqa: {code}")
    assert offenders == [], (
        "suppressions naming rules this project does not enable — ruff reports "
        "each as RUF100 and CI goes red:\n  " + "\n  ".join(offenders))


def test_multiline_imports_use_the_style_ruffs_isort_emits():
    """Ruff's isort rewrites a wrapped `from x import (...)` to one name per
    line with a magic trailing comma. A hanging-indent block (two or more
    names sharing a line after the paren) is I001 on sight."""
    offenders: list[str] = []
    for path in sorted(APP.rglob("*.py")):
        lines = path.read_text().splitlines()
        inside = False
        for lineno, line in enumerate(lines, 1):
            if not inside:
                if re.match(r"^(?:from\s+\S+\s+)?import\s+\(\s*$", line.strip()) \
                        or re.search(r"^from\s+\S+\s+import\s+\(", line):
                    # A single-line `import (a, b)` that closes on the same
                    # line is not a wrapped block.
                    if line.rstrip().endswith("("):
                        inside = True
                    elif "," in line.split("(", 1)[1].rstrip().rstrip(")"):
                        offenders.append(f"{path.name}:{lineno}: {line.strip()[:70]}")
                continue
            body = line.strip()
            if body.startswith(")"):
                inside = False
                continue
            if body.rstrip(",").count(",") >= 1:
                offenders.append(f"{path.name}:{lineno}: {body[:70]}")
    assert offenders == [], (
        "wrapped imports packing several names per line — ruff's isort emits "
        "one per line, so these are I001:\n  " + "\n  ".join(offenders))
