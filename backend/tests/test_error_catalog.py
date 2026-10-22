"""The error catalog (app/contracts/error_catalog.json) cannot rot.

CI fails when the code raises an error the catalog has never heard of, when a catalogued
code has no fix or no docs anchor, or when a response is missing its ``error`` object.
"""
from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from app.contracts import errors

ROOT = Path(__file__).resolve().parents[2]
SOURCES = [ROOT / "backend" / "app", ROOT / "apps"]


def _text(node) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(str(v.value) if isinstance(v, ast.Constant) else "{}" for v in node.values)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        a, b = _text(node.left), _text(node.right)
        return a + b if a is not None and b is not None else None
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "format":
        return _text(node.func.value)
    return None


def _status(node) -> int | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, int):
        return node.value
    return None


def _raised_messages():
    """(status, message template, where) for every HTTPException with a literal message."""
    for root in SOURCES:
        for path in sorted(root.rglob("*.py")):
            if ".venv" in path.parts or "tests" in path.parts:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                fn = node.func
                name = fn.id if isinstance(fn, ast.Name) else fn.attr if isinstance(fn, ast.Attribute) else ""
                if name not in ("HTTPException", "StarletteHTTPException") or len(node.args) < 2:
                    continue
                status, msg = _status(node.args[0]), _text(node.args[1])
                if status and msg:
                    yield status, msg, f"{path.relative_to(ROOT)}:{node.lineno}"


def test_every_http_exception_literal_is_catalogued():
    missing = [f"{where}: {status} {msg!r}" for status, msg, where in _raised_messages()
               if errors.lookup(status, msg) is None]
    assert not missing, ("These errors are raised but not in backend/app/contracts/error_catalog.json "
                         "(add the message to a code's `patterns`, or add a code):\n  " + "\n  ".join(missing))


def test_the_catalog_is_well_formed():
    raw = json.loads(errors.CATALOG_PATH.read_text(encoding="utf-8"))
    codes = [e["code"] for e in raw["errors"]]
    assert len(codes) == len(set(codes)), "duplicate error codes"
    docs = (ROOT / "docs" / "errors.md").read_text(encoding="utf-8")
    for e in raw["errors"]:
        assert e["code"].replace("_", "").isalnum() and e["code"] == e["code"].lower()
        assert 400 <= e["status"] <= 599, e["code"]
        assert e["fix"].strip(), f"{e['code']} has no fix"
        assert e["message"].strip(), f"{e['code']} has no message"
        assert f'id="{e["docs"]}"' in docs, f"{e['code']} has no anchor in docs/errors.md"


def test_every_code_used_in_the_source_is_in_the_catalog():
    used: dict[str, str] = {}
    for root in SOURCES:
        for path in sorted(root.rglob("*.py")):
            if ".venv" in path.parts or "tests" in path.parts:
                continue
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if not isinstance(node, ast.Call):
                    continue
                fn = node.func
                name = fn.id if isinstance(fn, ast.Name) else fn.attr if isinstance(fn, ast.Attribute) else ""
                where = f"{path.relative_to(ROOT)}:{node.lineno}"
                if name == "AccountError" and node.args and isinstance(node.args[0], ast.Constant):
                    used[node.args[0].value] = where
                if name == "_problem" and len(node.args) > 1 and isinstance(node.args[1], ast.Constant):
                    used[node.args[1].value] = where
                if name == "envelope" and node.args and isinstance(node.args[0], ast.Constant) \
                        and isinstance(node.args[0].value, str):
                    used[node.args[0].value] = where
                if name == "Decision" and len(node.args) > 2 and isinstance(node.args[2], ast.Constant):
                    used[node.args[2].value] = where
    unknown = {c: w for c, w in used.items() if errors.get(c) is None}
    assert not unknown, f"error codes used in code but missing from the catalog: {unknown}"


def test_the_envelope_degrades_instead_of_raising():
    assert errors.envelope("no_such_code_anywhere")["code"] == "no_such_code_anywhere"
    assert errors.envelope(status=404, detail="something odd")["code"] == "not_found"
    assert errors.envelope(status=418)["code"] == "http_418"
    e = errors.envelope("thread_not_found")
    assert set(e) == {"code", "message", "fix", "docs"} and e["fix"]


def test_lookup_matches_templates_and_prefers_the_status():
    assert errors.lookup(404, "Unknown bot: nova").code == "bot_not_found"
    assert errors.lookup(429, "Too many attempts — wait 12s.").code == "too_many_attempts"
    assert errors.lookup(403, "Your profile does not have permission to do that.").code == "permission_denied"
    assert errors.lookup(500, "no such thing as this message") is None


def test_responses_carry_the_error_object(world):
    owner = world.create_owner()
    # A handler's HTTPException ...
    r = owner.get("/api/threads/no-such-thread")
    assert r.status_code == 404
    body = r.json()
    assert body["detail"] == "Thread not found"                       # unchanged
    assert body["error"]["code"] == "thread_not_found" and body["error"]["fix"]
    # ... a validation failure ...
    r = owner.post("/api/threads", json=[])
    assert r.status_code == 422 and r.json()["error"]["code"] == "validation_error"
    # ... a gate refusal ...
    anon = world.client(("203.0.113.9", 1))
    r = anon.get("/api/dashboard")
    assert r.status_code in (401, 403) and r.json()["error"]["code"] in ("safe_mode", "signin_required")
    # ... and a machine caller gets the same object inside the problem document.
    r = world.client(("127.0.0.1", 2), origin=False).get("/api/threads/no-such-thread")
    assert r.headers["content-type"].startswith("application/problem+json")
    assert r.json()["code"] == "thread_not_found" and r.json()["error"]["code"] == "thread_not_found"
    assert r.json()["detail"] == "Thread not found"


@pytest.mark.parametrize("status", [400, 401, 403, 404, 409, 413, 415, 422, 429, 500])
def test_every_generic_status_has_a_fallback_entry(status):
    assert errors.envelope(status=status)["code"] != f"http_{status}" or status == 500
