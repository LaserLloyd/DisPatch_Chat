"""The Restricted tier is gone (4.0, SPEC section 6 / BUILD-PLAN-v2 §1.11): no turn-time safety
layer, no quiet hours or daily budgets, no decoy view, no locked voice socket - and nothing in
the shipped files is made for a restricted audience. A Basic profile is an ordinary profile
whose role holds fewer permissions; every refusal it meets is a named permission or ownership
check.

Run: cd backend && uv run pytest tests/test_no_restricted_tier.py
"""
from __future__ import annotations

import ast
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "backend" / "app"
APPS = ROOT / "apps"

#: Names that belonged to the Restricted tier. None may be defined or used anywhere in the app.
GONE_SYMBOLS = (
    "LOCKED_RULES", "tools_locked", "locked_down", "quiet_hours", "is_restricted", "_is_decoy",
    "conn_decoy", "redact_for_decoy", "_safe_bot_ids", "not_restricted", "RESTRICTED_NEVER",
    "_restricted_limit_denied", "_restricted_limits_ok", "_note_active_time", "_decoy_quota_left",
    "_decoy_action_allowed", "_deny_decoy_bot", "_deny_decoy_thread", "_deny_decoy_mutation",
    "_is_safe_mode_caller", "turn_locked", "locked_may_see", "locked_client_ip", "locked_status",
    "safe_ids", "DECOY_BODY", "AppDecoy", "DECOY_TURN_QUOTA", "DECOY_THREAD_QUOTA",
    "DECOY_FEEDBACK_QUOTA", "DECOY_UPLOAD_QUOTA", "UsageMap", "usage_charge", "clean_budgets",
    "SAFE_SYSTEM_PROMPT",
)


def _sources():
    for base in (APP, APPS):
        for p in sorted(base.rglob("*.py")):
            if ".venv" in p.parts or "tests" in p.parts or "__pycache__" in p.parts:
                continue
            yield p


def _names(path: Path) -> set[str]:
    out: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Name):
            out.add(node.id)
        elif isinstance(node, ast.Attribute):
            out.add(node.attr)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.add(node.name)
        elif isinstance(node, ast.arg):
            out.add(node.arg)
        elif isinstance(node, ast.keyword) and node.arg:
            out.add(node.arg)
    return out


def test_no_restricted_tier_symbol_is_left_in_the_app():
    hits = []
    for p in _sources():
        found = _names(p) & set(GONE_SYMBOLS)
        if found:
            hits.append(f"{p.relative_to(ROOT)}: {sorted(found)}")
    assert not hits, "Restricted-tier code is back:\n  " + "\n  ".join(hits)


def test_the_limits_module_and_the_restricted_pack_are_deleted():
    assert not (APP / "limits.py").exists()
    assert not (APP / "starter_packs" / "animal_friends.yaml").exists()


def test_the_turn_options_carry_no_locked_flag():
    from app import main
    assert "locked" not in main.TurnOptions._fields


def test_no_request_or_socket_carries_a_decoy_flag():
    from app import main, ws
    for p in (APP / "main.py", APP / "extensions.py", APP / "apps_loader.py", APP / "tools.py"):
        assert 'state, "decoy"' not in p.read_text(encoding="utf-8"), p.name
        assert "state.decoy" not in p.read_text(encoding="utf-8"), p.name
    assert not hasattr(ws.ConnectionManager, "conn_decoy")
    assert not hasattr(main.manager, "redactor")


def test_no_shipped_yaml_is_made_for_a_restricted_audience():
    tracked = subprocess.run(["git", "-C", str(ROOT), "ls-files", "*.yaml", "*.yml"],
                             capture_output=True, text=True, check=True).stdout.split()
    bad = []
    for rel in tracked:
        doc = yaml.safe_load((ROOT / rel).read_text(encoding="utf-8"))

        def walk(x, rel=rel):
            if isinstance(x, dict):
                if x.get("audience") == "restricted":
                    bad.append(rel)
                for v in x.values():
                    walk(v)
            elif isinstance(x, list):
                for v in x:
                    walk(v)
        walk(doc)
    assert not bad, f"audience: restricted in shipped files: {sorted(set(bad))}"


def test_no_tracked_file_names_a_removed_pack_bot():
    tracked = subprocess.run(["git", "-C", str(ROOT), "ls-files"], capture_output=True,
                             text=True, check=True).stdout.split()
    needle = "kids" + "_"            # split so this file does not match itself
    hits = []
    for rel in tracked:
        p = ROOT / rel
        try:
            text = p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if needle in text:
            hits.append(rel)
    assert not hits, f"{needle!r} in tracked files: {hits}"


def test_the_error_catalog_has_no_restricted_tier_code():
    from app.contracts import errors
    for code in ("safe_mode", "quiet_hours", "time_limit", "daily_limit", "budgets_invalid",
                 "task_thread_not_allowed", "device_bind_role"):
        assert errors.get(code) is None, code


def test_a_basic_profile_meets_named_checks_not_a_decoy(world):
    """A Basic profile's refusals are the ordinary 403 permission_denied (with the permission
    it lacks), never the 3.x 'Unlock for full access' decoy body."""
    owner = world.create_owner()
    p = world.person(owner, "Bea", "basic", pin="7373", household_bots=["alpha"],
                     grants=["dispatch_agents"])
    c = world.client(("203.0.113.88", 47000))
    assert world.login(c, profile_id=p["id"], pin="7373").status_code == 200
    for path in ("/api/files", "/api/bots/all", "/api/people"):
        r = c.get(path)
        assert r.status_code == 403, (path, r.status_code)
        body = r.json()
        assert "decoy" not in body and body["detail"] != "Unlock for full access", path
        assert body["error"]["code"] == "permission_denied", path
    # ...and its own chat works like anyone's.
    t = c.post("/api/threads", json={"bot_id": "alpha"})
    assert t.status_code == 200, t.text
    assert c.get(f"/api/threads/{t.json()['id']}/messages").status_code == 200
    # The reaction pack is every role's (no "safe" subset), its curation the Admins'.
    rx = c.get("/api/reactions")
    assert rx.status_code == 200 and rx.json()["can_manage"] is False
    assert len(rx.json()["reactions"]) == len(owner.get("/api/reactions").json()["reactions"])
    st = c.get("/api/auth/status").json()
    assert st["authenticated"] is True and "decoy" not in st   # 4.0: the 3.x key is gone
