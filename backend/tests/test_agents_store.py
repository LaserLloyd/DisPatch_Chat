"""The agents tables: the additive migration (with its backup and an exact dry run), the daily
spend ledger, approvals, settings, and the retention purge."""
from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from test_migrate import legacy

from app import accounts, migrate
from app import agents_store as store


def _drop_agent_tables(d):
    con = sqlite3.connect(d / "chats.db")
    for t in store.TABLES:
        con.execute(f"DROP TABLE IF EXISTS {t}")
    con.commit()
    con.close()
    store.reset_for_tests()


def _tables(d):
    con = sqlite3.connect(d / "chats.db")
    try:
        return {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        con.close()


# --------------------------------------------------------------------------- #
# The migration step
# --------------------------------------------------------------------------- #


def test_the_dry_run_names_the_new_tables_and_writes_nothing(legacy):
    _drop_agent_tables(legacy)
    before = {p.name: p.read_bytes() for p in legacy.iterdir() if p.is_file()}
    rep = migrate.run(dry_run=True)
    mine = [a for a in rep.actions if a.step == "008_agents"]
    assert len(mine) == 1
    for table in store.TABLES:
        assert table in mine[0].text
    assert "nothing existing is altered" in mine[0].text
    assert {p.name: p.read_bytes() for p in legacy.iterdir() if p.is_file()} == before
    assert not (_tables(legacy) & set(store.TABLES))


def test_applying_it_backs_up_first_then_adds_only_new_tables(legacy):
    _drop_agent_tables(legacy)
    before = _tables(legacy)
    con = sqlite3.connect(legacy / "chats.db")
    threads_before = con.execute("SELECT id, title FROM threads ORDER BY id").fetchall()
    con.close()
    rep = migrate.run()
    assert "008_agents" in rep.applied
    assert rep.backup is not None and (rep.backup / "chats.db").exists()
    # the backup is the original: no agent tables in it
    assert not (_tables_of(rep.backup / "chats.db") & set(store.TABLES))
    after = _tables(legacy)
    assert set(store.TABLES) <= after
    assert before <= after, "nothing was dropped"
    con = sqlite3.connect(legacy / "chats.db")
    assert con.execute("SELECT id, title FROM threads ORDER BY id").fetchall() == threads_before
    assert con.execute("SELECT 1 FROM schema_migrations WHERE id='008_agents'").fetchone()
    con.close()
    assert migrate.plan().actions == [] and migrate.run().applied == [], "idempotent"


def _tables_of(path):
    con = sqlite3.connect(path)
    try:
        return {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        con.close()


def test_a_database_that_already_has_the_tables_has_nothing_to_migrate(legacy):
    migrate.run()
    assert not [a for a in migrate.plan().actions if a.step == "008_agents"]


# --------------------------------------------------------------------------- #
# The ledger, approvals, settings
# --------------------------------------------------------------------------- #


@pytest.fixture
def db(tmp_path):
    # tests/conftest.py already points config.DATA_DIR at <tmp>/data for every test.
    accounts.reset_for_tests()
    store.reset_for_tests()
    return tmp_path / "data"


def test_the_spend_ledger_adds_up_per_agent_and_per_profile_per_day(db):
    store.add_spend("ag1", "pA", usd=0.25, tokens=100, turns=2, tasks=1)
    store.add_spend("ag1", "pB", usd=0.5, tokens=50, turns=1)
    store.add_spend("ag2", "pA", usd=1.0)
    assert store.spent_today("agent", "ag1") == pytest.approx(0.75)
    assert store.spent_today("profile", "pA") == pytest.approx(1.25)
    assert store.spent_today("profile", "nobody") == 0.0
    assert store.spent_today("agent", "ag1", day="2001-01-01") == 0.0
    rows = {(r["kind"], r["subject"]): r for r in store.spend_for_day()}
    assert rows[("agent", "ag1")]["tokens"] == 150 and rows[("agent", "ag1")]["tasks"] == 1
    assert store.purge(30) == []                 # purge also trims ancient spend rows
    store.add_spend("ag1", "pA", usd=9.0, day="2001-01-01")
    store.create_task(agent_id="a", agent_name="a", backend="fake", spec_json="{}", state="succeeded",
                      finished_at=(datetime.now(UTC) - timedelta(days=90)).isoformat(timespec="seconds"))
    assert len(store.purge(30)) == 1
    assert store.spent_today("agent", "ag1", day="2001-01-01") == 0.0


def test_settings_default_and_roundtrip(db):
    assert store.get_setting("agents.retention_days") == 30
    assert store.get_setting("agents.unified_dispatch") is False
    store.set_setting("agents.retention_days", 7)
    assert store.get_setting("agents.retention_days") == 7
    store.set_profile_budget("p1", {"usd_per_day": 2.5})
    assert store.profile_budget("p1") == {"usd_per_day": 2.5}
    assert store.profile_budget("p2") == {}
    assert store.all_profile_budgets() == {"p1": {"usd_per_day": 2.5}}
    assert "profile_budget:p1" not in store.all_settings()
    store.set_profile_budget("p1", None)
    assert store.all_profile_budgets() == {}


def test_a_batch_is_all_or_nothing(db):
    t = store.create_task(agent_id="a", agent_name="a", backend="fake", spec_json="{}")
    ev = {"seq": 0, "ts": 1.0, "type": "status", "state": "queued"}

    def boom(con):
        raise RuntimeError("disk on fire")
    with pytest.raises(RuntimeError):
        store.run_batch([store.op_event(t["task_id"], ev), store.op_task(t["task_id"], state="running"), boom])
    assert store.list_events(t["task_id"]) == [] and store.get_task(t["task_id"])["state"] == "queued"
    store.run_batch([store.op_event(t["task_id"], ev), store.op_task(t["task_id"], state="running")])
    assert [e["seq"] for e in store.list_events(t["task_id"])] == [0]
    assert store.get_task(t["task_id"])["state"] == "running"
    # replaying the same event is harmless (INSERT OR IGNORE): the cursor makes it idempotent
    store.run_batch([store.op_event(t["task_id"], ev)])
    assert len(store.list_events(t["task_id"])) == 1


def test_approval_rows_record_who_decided_what_once(db):
    t = store.create_task(agent_id="a", agent_name="a", backend="fake", spec_json="{}")
    row = {"request_id": "apr_1", "task_id": t["task_id"], "tool": "Bash", "args_summary": "ls",
           "scopes": ["once"], "requested_at": store.now_iso()}
    store.run_batch([store.op_approval_add(row)])
    assert store.get_approval("apr_1")["decision"] is None
    assert store.pending_approval_count([t["task_id"]]) == {t["task_id"]: 1}
    store.run_batch([store.op_approval_decide("apr_1", "allow", "once", "p9")])
    store.run_batch([store.op_approval_decide("apr_1", "deny", "once", "p8")])      # too late
    a = store.get_approval("apr_1")
    assert (a["decision"], a["decided_by"], a["scope"]) == ("allow", "p9", "once")
    assert store.list_approvals(t["task_id"], pending_only=True) == []


def test_the_purge_window_and_what_it_keeps(db):
    old = (datetime.now(UTC) - timedelta(days=45)).isoformat(timespec="seconds")
    fresh = (datetime.now(UTC) - timedelta(days=5)).isoformat(timespec="seconds")
    a = store.create_task(agent_id="a", agent_name="Old", backend="fake", spec_json="{}", state="succeeded",
                          finished_at=old, prompt="old prompt", result_text="old result", cost_usd=0.5)
    b = store.create_task(agent_id="a", agent_name="New", backend="fake", spec_json="{}", state="succeeded",
                          finished_at=fresh, prompt="new prompt")
    c = store.create_task(agent_id="a", agent_name="Live", backend="fake", spec_json="{}", state="running",
                          created_at=old, prompt="running prompt")
    store.run_batch([store.op_event(a["task_id"], {"seq": 0, "ts": 1.0, "type": "status", "state": "queued"})])
    assert store.purge(0) == [], "0 means keep for ever"
    assert store.purge(30) == [a["task_id"]]
    ga = store.get_task(a["task_id"])
    assert ga["prompt"] is None and ga["result_text"] is None and ga["purged"] is True
    assert ga["cost_usd"] == 0.5 and ga["agent_name"] == "Old", "the audit row stays"
    assert store.list_events(a["task_id"]) == []
    assert store.get_task(b["task_id"])["prompt"] == "new prompt"
    assert store.get_task(c["task_id"])["prompt"] == "running prompt", "an unfinished task is never purged"
    assert store.purge(30) == []


def test_unknown_columns_are_refused(db):
    with pytest.raises(ValueError):
        store.create_task(agent_id="a", agent_name="a", backend="fake", spec_json="{}", nope=1)
    t = store.create_task(agent_id="a", agent_name="a", backend="fake", spec_json="{}")
    with pytest.raises(ValueError):
        store.update_task(t["task_id"], nope=1)
