"""Only accounts.py knows the stored role words (BUILD-PLAN-v2 §1.1, Fable amendment 1).

``profiles.role`` still holds the 3.x words (``owner/admin/member/restricted``) so 4.0 needs no
table rebuild. Everywhere else a profile has a ``Role`` (``admin/user/basic``) and a
``protected`` flag. A comparison with, or a write of, a stored word outside accounts.py would
silently stop matching (``Profile.role`` is never ``"owner"`` now) or bypass the mapping, so
this gate fails the build on one.

Allowed: accounts.py (the mapping), contracts/permissions.py (``LEGACY_ROLE``, the INPUT
vocabulary), and old words passed AS INPUT (``create_profile(name, "member")`` in a test, a
3.x API client sending ``role: "member"``): those are normalised.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORDS = r"(?:owner|member|restricted)"

#: (pattern, why) checked in app code.
APP_PATTERNS = [
    (re.compile(r"\bRole\.(?:OWNER|MEMBER|RESTRICTED)\b"), "a 3.x Role member"),
    (re.compile(r"\.role\b[^\n#]{0,40}?(?:==|!=|\bnot in\b|\bin\b)[^\n#]{0,80}?[\"']" + WORDS + r"[\"']"),
     "a role compared with a stored word"),
    (re.compile(r"[\"']" + WORDS + r"[\"']\s*(?:==|!=)\s*[\w.]*\brole\b"), "a stored word compared with a role"),
    (re.compile(r"\brole\s*=\s*'" + WORDS + r"'"), "SQL naming a stored word"),
    (re.compile(r"\bcreate_profile\([^)\n]*[\"']" + WORDS + r"[\"']"), "a profile created with a stored word"),
]

#: Checked in tests too: an API/profile role read back must be a 4.0 word.
TEST_PATTERNS = [
    (re.compile(r"\bRole\.(?:OWNER|MEMBER|RESTRICTED)\b"), "a 3.x Role member"),
    (re.compile(r"\.role\s*(?:==|!=|is)\s*[\"']" + WORDS + r"[\"']"), "a profile role compared with a stored word"),
    (re.compile(r"\[[\"']role[\"']\]\s*(?:==|!=)\s*[\"']" + WORDS + r"[\"']"), "an API role compared with a stored word"),
]

ALLOWED = {
    "backend/app/accounts.py",
    "backend/app/contracts/permissions.py",
    "backend/tests/test_stored_role_strings.py",
}


def _files(*globs: str):
    for g in globs:
        for f in sorted(ROOT.glob(g)):
            rel = f.relative_to(ROOT).as_posix()
            if "__pycache__" in rel or rel in ALLOWED or "/.venv/" in rel:
                continue
            yield rel, f


def _scan(files, patterns) -> list[str]:
    hits = []
    for rel, f in files:
        for n, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            code = line.split("#", 1)[0] if not line.lstrip().startswith(("\"", "'")) else line
            for rx, why in patterns:
                if rx.search(code):
                    hits.append(f"{rel}:{n}: {why}: {line.strip()[:120]}")
    return hits


def test_no_app_module_touches_a_stored_role_word():
    hits = _scan(_files("backend/app/**/*.py", "apps/*/*.py"), APP_PATTERNS)
    assert not hits, "stored role words outside accounts.py:\n" + "\n".join(hits)


def test_no_test_reads_back_a_stored_role_word():
    hits = _scan(_files("backend/tests/**/*.py", "apps/*/tests/*.py"), TEST_PATTERNS)
    assert not hits, "tests expecting a 3.x role word back:\n" + "\n".join(hits)


def test_the_gate_catches_what_it_is_for():
    bad = [
        'if p.role == "owner":',
        "if prof.role in ('owner', 'admin'):",
        'x = Role.RESTRICTED',
        'con.execute("SELECT 1 FROM profiles WHERE role=\'owner\'")',
        'accounts.create_profile("Owner", "owner", pin_hash=h)',
    ]
    for line in bad:
        assert any(rx.search(line) for rx, _ in APP_PATTERNS), line
    good = ['if p.protected:', 'if p.role is Role.BASIC:', 'kind == "owner"  # a task owner key',
            'accounts.create_profile("Owner", Role.ADMIN, protected=True)']
    for line in good:
        assert not any(rx.search(line) for rx, _ in APP_PATTERNS), line
