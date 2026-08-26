"""Deciding whether a path is disposable — the rule behind the write guard.

Lives in its own module, apart from conftest.py, for one reason: a fixture
cannot easily be tested, and this rule has already failed silently once. It is
imported by conftest's `_no_writes_outside_tmp` and asserted directly by
tests/test_tmp_guard.py.

WHAT WENT WRONG. The original rule derived a "temp root" by string surgery on
the basetemp path -- `Path(tmp_path).parents[-2]`, i.e. its TOP-LEVEL
directory -- and then compared with `str.startswith`. With pytest's default
basetemp under /tmp that root is `/tmp` and the rule holds. Point pytest
anywhere else, though, and the root becomes that location's top-level
directory. `--basetemp=/var/tmp/...` (the obvious move when /tmp fills up)
yields `/var`, and on a system whose home is /var/home/<user> -- Fedora
Atomic, where /home is a symlink to /var/home -- EVERY real path on the box
then answers "yes, that is temp". The guard no-ops, the suite runs against the
live install, and because a test writing to the wrong place still passes,
nothing says a word. That is how the family app's avatar-snapshot store came to
be deleted by `test_prune_keeps_referenced_and_drops_orphans`.

THE RULE NOW. Containment in a KNOWN disposable root -- the session's real
basetemp (asked of pytest, never inferred from a string), the platform temp
directory, and /tmp -- tested with `Path.relative_to` rather than
`str.startswith`, which additionally stops `/tmpfoo` reading as inside `/tmp`.
No parent-walking, so there is no depth for a different basetemp to change.
"""
from __future__ import annotations

import tempfile
from pathlib import Path


def temp_roots(basetemp: Path | str | None = None) -> list[Path]:
    """The directories a test may freely write into."""
    roots = [Path("/tmp"), Path(tempfile.gettempdir())]
    if basetemp:
        roots.append(Path(basetemp))
    out: list[Path] = []
    for r in roots:
        try:
            rp = r.resolve()
        except OSError:
            continue
        if rp not in out:
            out.append(rp)
    return out


def is_disposable(path: Path | str, basetemp: Path | str | None = None) -> bool:
    """Is `path` inside a known-disposable root?

    Anything that cannot be resolved answers False: the guard's default has to
    be "redirect this somewhere safe", never "let it through".
    """
    try:
        p = Path(path).resolve()
    except OSError:
        return False
    for root in temp_roots(basetemp):
        try:
            p.relative_to(root)
            return True
        except ValueError:
            continue
    return False
