"""Persistence for the agent-dispatch feature: agents, tasks, task events, approvals
and the daily spend ledger.

Everything lives in ``chats.db`` beside the conversations, so the rotating verified
backups and the pre-migration backup (``app/migrate.py``, step ``005_agents``) cover it.
Like :mod:`app.accounts` this is a synchronous ``sqlite3`` module on purpose: the task
manager writes small rows in batches from one writer, the routes read small result sets,
and neither wants the chat database's single shared aiosqlite connection in its way.

The schema is additive only (``CREATE TABLE IF NOT EXISTS``, nothing dropped or altered
in an existing table): a 2.x or 3.0 binary run against a migrated database simply ignores
these tables.

What is stored, and what is NOT:

* ``agents.spec`` is the :class:`app.agents.AgentSpec` as JSON. Secrets are never in it:
  an MCP server's ``header_secrets`` maps a header to the NAME of a secret that lives in
  ``app/agents/secrets.py``'s store.
* ``tasks.prompt`` / ``tasks.result_text`` and the ``task_events`` rows are the
  *transcript*. They are what the retention purge deletes (``purge``), and what the
  support bundle never contains (``redacted_summary``) - the brief: "Support bundles
  exclude prompts by default".
* Costs are ESTIMATES (RESEARCH.md F6e, R-COST): ``tasks.cost_usd`` and the ledger are
  built from the backend's client-side figures and are never billing data.
"""

from __future__ import annotations

import contextlib
import json
import logging
import sqlite3
import threading
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

from . import accounts

log = logging.getLogger("local-chat.agents.store")

SCHEMA = """
CREATE TABLE IF NOT EXISTS agents (
    id           TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    description  TEXT NOT NULL DEFAULT '',
    spec         TEXT NOT NULL,
    published_to TEXT NOT NULL DEFAULT '[]',
    created_by   TEXT,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    disabled     INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_agents_name ON agents(name COLLATE NOCASE);

CREATE TABLE IF NOT EXISTS tasks (
    task_id            TEXT PRIMARY KEY,
    agent_id           TEXT NOT NULL,
    agent_name         TEXT NOT NULL DEFAULT '',
    backend            TEXT NOT NULL,
    owner              TEXT NOT NULL DEFAULT '',
    kind               TEXT NOT NULL DEFAULT 'task',
    state              TEXT NOT NULL,
    reason             TEXT,
    hold               TEXT,
    spec_json          TEXT NOT NULL,
    prompt             TEXT,
    result_text        TEXT,
    session_id         TEXT,
    cost_usd           REAL,
    tokens             INTEGER NOT NULL DEFAULT 0,
    turns              INTEGER NOT NULL DEFAULT 0,
    last_seq           INTEGER NOT NULL DEFAULT -1,
    resumable          INTEGER NOT NULL DEFAULT 0,
    created_at         TEXT NOT NULL,
    started_at         TEXT,
    finished_at        TEXT,
    origin_thread_id   TEXT,
    origin_message_id  TEXT,
    advisor_request_id TEXT,
    repo_path          TEXT,
    approvals_allowed  INTEGER NOT NULL DEFAULT 0,
    approvals_denied   INTEGER NOT NULL DEFAULT 0,
    purged             INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_tasks_owner_created ON tasks(owner, created_at);
CREATE INDEX IF NOT EXISTS idx_tasks_state ON tasks(state);
CREATE INDEX IF NOT EXISTS idx_tasks_agent ON tasks(agent_id);
CREATE INDEX IF NOT EXISTS idx_tasks_origin ON tasks(origin_thread_id);

CREATE TABLE IF NOT EXISTS task_events (
    task_id TEXT NOT NULL,
    seq     INTEGER NOT NULL,
    ts      REAL NOT NULL,
    type    TEXT NOT NULL,
    data    TEXT NOT NULL,
    PRIMARY KEY (task_id, seq)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS approvals (
    request_id   TEXT PRIMARY KEY,
    task_id      TEXT NOT NULL,
    tool         TEXT NOT NULL,
    args_summary TEXT NOT NULL DEFAULT '',
    scopes       TEXT NOT NULL DEFAULT '["once"]',
    call_id      TEXT,
    requested_at TEXT NOT NULL,
    decision     TEXT,
    scope        TEXT,
    decided_by   TEXT,
    decided_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_approvals_task ON approvals(task_id);

CREATE TABLE IF NOT EXISTS agent_spend (
    day     TEXT NOT NULL,
    kind    TEXT NOT NULL,
    subject TEXT NOT NULL,
    usd     REAL NOT NULL DEFAULT 0,
    tokens  INTEGER NOT NULL DEFAULT 0,
    turns   INTEGER NOT NULL DEFAULT 0,
    tasks   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, kind, subject)
);

CREATE TABLE IF NOT EXISTS agent_settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

#: The tables this module owns, children first (the migration's dry run lists them).
TABLES = ("agent_settings", "agent_spend", "approvals", "task_events", "tasks", "agents")

TERMINAL = ("succeeded", "failed", "cancelled")
ACTIVE = ("running", "awaiting_approval")
STATES = ("queued", "running", "awaiting_approval", "succeeded", "failed", "cancelled")

#: Setting keys and their defaults. ``agents.unified_dispatch`` is the feature flag
#: (docs/agents.md): off, DisPatch behaves exactly as before for chat turns and the
#: advisor's handoff cards.
SETTING_DEFAULTS: dict[str, Any] = {
    "agents.unified_dispatch": False,
    "agents.retention_days": 30,
    "agents.max_concurrent": 4,
    "agents.claude.auth_route": "api_key",
}

_lock = threading.RLock()
_ready: set[str] = set()


# --------------------------------------------------------------------------- #
# Connection
# --------------------------------------------------------------------------- #


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def new_id(prefix: str = "") -> str:
    return prefix + uuid.uuid4().hex[:12]


@contextlib.contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    """A connection to chats.db with this module's tables ensured."""
    path = accounts.db_path()
    with accounts.connect(path) as con:
        key = str(path)
        if key not in _ready:
            con.executescript(SCHEMA)
            _ready.add(key)
        yield con


def reset_for_tests() -> None:
    with _lock:
        _ready.clear()


def _j(text: str | None, default: Any) -> Any:
    if not text:
        return default
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return default


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #


def get_setting(key: str, default: Any = None) -> Any:
    if default is None:
        default = SETTING_DEFAULTS.get(key)
    with connect() as con:
        row = con.execute("SELECT value FROM agent_settings WHERE key=?", (key,)).fetchone()
    return _j(row["value"], default) if row else default


def set_setting(key: str, value: Any) -> None:
    with _lock, connect() as con:
        con.execute("INSERT INTO agent_settings (key,value) VALUES (?,?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (key, json.dumps(value)))


def all_settings() -> dict[str, Any]:
    with connect() as con:
        rows = con.execute("SELECT key,value FROM agent_settings "
                           "WHERE key NOT LIKE 'profile_budget:%'").fetchall()
    out = dict(SETTING_DEFAULTS)
    for r in rows:
        out[r["key"]] = _j(r["value"], SETTING_DEFAULTS.get(r["key"]))
    return out


def profile_budget(profile_id: str) -> dict[str, Any]:
    """The per-profile agent budget (USD/day, max turns, wall-clock, token cap), {} if none."""
    v = get_setting(f"profile_budget:{profile_id}", default={})
    return v if isinstance(v, dict) else {}


def set_profile_budget(profile_id: str, budget: dict[str, Any] | None) -> None:
    if not budget:
        with _lock, connect() as con:
            con.execute("DELETE FROM agent_settings WHERE key=?", (f"profile_budget:{profile_id}",))
        return
    set_setting(f"profile_budget:{profile_id}", budget)


def all_profile_budgets() -> dict[str, dict]:
    with connect() as con:
        rows = con.execute("SELECT key,value FROM agent_settings "
                           "WHERE key LIKE 'profile_budget:%'").fetchall()
    return {r["key"].split(":", 1)[1]: _j(r["value"], {}) for r in rows}


# --------------------------------------------------------------------------- #
# Agents
# --------------------------------------------------------------------------- #


def _agent_row(r: sqlite3.Row) -> dict[str, Any]:
    return {"id": r["id"], "name": r["name"], "description": r["description"],
            "spec": _j(r["spec"], {}), "published_to": _j(r["published_to"], []),
            "created_by": r["created_by"], "created_at": r["created_at"],
            "updated_at": r["updated_at"], "disabled": bool(r["disabled"])}


def list_agents() -> list[dict[str, Any]]:
    with connect() as con:
        rows = con.execute("SELECT * FROM agents ORDER BY name COLLATE NOCASE").fetchall()
    return [_agent_row(r) for r in rows]


def get_agent(agent_id: str) -> dict[str, Any] | None:
    with connect() as con:
        r = con.execute("SELECT * FROM agents WHERE id=?", (agent_id,)).fetchone()
    return _agent_row(r) if r else None


def create_agent(*, name: str, spec: dict, published_to: list[str], created_by: str,
                 description: str = "", disabled: bool = False) -> dict[str, Any]:
    aid = new_id("ag_")
    now = now_iso()
    with _lock, connect() as con:
        con.execute("INSERT INTO agents (id,name,description,spec,published_to,created_by,"
                    "created_at,updated_at,disabled) VALUES (?,?,?,?,?,?,?,?,?)",
                    (aid, name, description, json.dumps(spec), json.dumps(published_to),
                     created_by, now, now, int(disabled)))
    return get_agent(aid)  # type: ignore[return-value]


def update_agent(agent_id: str, **fields: Any) -> dict[str, Any] | None:
    cols = {"name": lambda v: v, "description": lambda v: v, "spec": json.dumps,
            "published_to": json.dumps, "disabled": int}
    sets, args = ["updated_at=?"], [now_iso()]
    for k, v in fields.items():
        if k in cols:
            sets.append(f"{k}=?")
            args.append(cols[k](v))
    args.append(agent_id)
    with _lock, connect() as con:
        con.execute(f"UPDATE agents SET {', '.join(sets)} WHERE id=?", args)
    return get_agent(agent_id)


def delete_agent(agent_id: str) -> bool:
    with _lock, connect() as con:
        cur = con.execute("DELETE FROM agents WHERE id=?", (agent_id,))
    return cur.rowcount > 0


def agent_by_name(name: str) -> dict[str, Any] | None:
    with connect() as con:
        r = con.execute("SELECT * FROM agents WHERE name=? COLLATE NOCASE", (name,)).fetchone()
    return _agent_row(r) if r else None


# --------------------------------------------------------------------------- #
# Tasks
# --------------------------------------------------------------------------- #

_TASK_COLS = (
    "task_id", "agent_id", "agent_name", "backend", "owner", "kind", "state", "reason", "hold",
    "spec_json", "prompt", "result_text", "session_id", "cost_usd", "tokens", "turns", "last_seq",
    "resumable", "created_at", "started_at", "finished_at", "origin_thread_id",
    "origin_message_id", "advisor_request_id", "repo_path", "approvals_allowed",
    "approvals_denied", "purged")


def _task_row(r: sqlite3.Row) -> dict[str, Any]:
    d = {k: r[k] for k in _TASK_COLS}
    d["resumable"] = bool(d["resumable"])
    d["purged"] = bool(d["purged"])
    return d


def create_task(**fields: Any) -> dict[str, Any]:
    row: dict[str, Any] = {"state": "queued", "kind": "task", "created_at": now_iso(),
                           "tokens": 0, "turns": 0, "last_seq": -1, "resumable": 0}
    row.update(fields)
    row.setdefault("task_id", new_id("t_"))
    unknown = set(row) - set(_TASK_COLS)
    if unknown:
        raise ValueError(f"unknown task columns: {sorted(unknown)}")
    cols = list(row)
    with _lock, connect() as con:
        con.execute(f"INSERT INTO tasks ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                    [row[c] for c in cols])
    return get_task(row["task_id"])  # type: ignore[return-value]


def get_task(task_id: str) -> dict[str, Any] | None:
    with connect() as con:
        r = con.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
    return _task_row(r) if r else None


def update_task(task_id: str, **fields: Any) -> None:
    if not fields:
        return
    with _lock, connect() as con:
        _update_task(con, task_id, fields)


def _update_task(con: sqlite3.Connection, task_id: str, fields: dict[str, Any]) -> None:
    unknown = set(fields) - set(_TASK_COLS)
    if unknown or "task_id" in fields:
        raise ValueError(f"cannot update task columns: {sorted(unknown | ({'task_id'} & set(fields)))}")
    sets = ", ".join(f"{k}=?" for k in fields)
    con.execute(f"UPDATE tasks SET {sets} WHERE task_id=?", [*fields.values(), task_id])


def list_tasks(*, owner: str | None = None, states: list[str] | None = None,
               agent_id: str | None = None, kind: str | None = None, since: str | None = None,
               until: str | None = None, thread_id: str | None = None, limit: int = 100,
               offset: int = 0, include_chat: bool = False) -> list[dict[str, Any]]:
    where, args = ["1=1"], []
    if owner is not None:
        where.append("owner=?")
        args.append(owner)
    if states:
        where.append(f"state IN ({','.join('?' * len(states))})")
        args.extend(states)
    if agent_id:
        where.append("agent_id=?")
        args.append(agent_id)
    if kind:
        where.append("kind=?")
        args.append(kind)
    elif not include_chat:
        where.append("kind != 'chat'")        # a chat turn is not a task the person assigned
    if since:
        where.append("created_at >= ?")
        args.append(since)
    if until:
        where.append("created_at < ?")
        args.append(until)
    if thread_id:
        where.append("origin_thread_id=?")
        args.append(thread_id)
    q = (f"SELECT * FROM tasks WHERE {' AND '.join(where)} "
         f"ORDER BY created_at DESC, rowid DESC LIMIT ? OFFSET ?")
    with connect() as con:
        rows = con.execute(q, [*args, max(1, min(int(limit), 500)), max(0, int(offset))]).fetchall()
    return [_task_row(r) for r in rows]


def count_tasks(*, states: list[str] | None = None, kind: str | None = None,
                exclude_kind: str | None = None) -> int:
    where, args = ["1=1"], []
    if states:
        where.append(f"state IN ({','.join('?' * len(states))})")
        args.extend(states)
    if kind:
        where.append("kind=?")
        args.append(kind)
    if exclude_kind:
        where.append("kind != ?")
        args.append(exclude_kind)
    with connect() as con:
        return int(con.execute(f"SELECT COUNT(*) FROM tasks WHERE {' AND '.join(where)}",
                               args).fetchone()[0])


def nonterminal_tasks() -> list[dict[str, Any]]:
    with connect() as con:
        rows = con.execute("SELECT * FROM tasks WHERE state NOT IN ('succeeded','failed','cancelled') "
                           "ORDER BY created_at, rowid").fetchall()
    return [_task_row(r) for r in rows]


def queued_tasks(limit: int = 50) -> list[dict[str, Any]]:
    """Tasks waiting for a free slot (NOT the ones held for a human's Send)."""
    with connect() as con:
        rows = con.execute("SELECT * FROM tasks WHERE state='queued' AND hold IS NULL "
                           "ORDER BY created_at, rowid LIMIT ?", (limit,)).fetchall()
    return [_task_row(r) for r in rows]


def task_by_advisor_request(rid: str) -> dict[str, Any] | None:
    with connect() as con:
        r = con.execute("SELECT * FROM tasks WHERE advisor_request_id=? ORDER BY rowid DESC LIMIT 1",
                        (rid,)).fetchone()
    return _task_row(r) if r else None


# --------------------------------------------------------------------------- #
# Events
# --------------------------------------------------------------------------- #


def event_row(task_id: str, ev: dict[str, Any]) -> tuple:
    return (task_id, int(ev["seq"]), float(ev.get("ts") or 0.0), str(ev["type"]),
            json.dumps(ev, separators=(",", ":")))


def append_events(rows: list[tuple]) -> None:
    if not rows:
        return
    with _lock, connect() as con:
        con.executemany("INSERT OR IGNORE INTO task_events (task_id,seq,ts,type,data) "
                        "VALUES (?,?,?,?,?)", rows)


def list_events(task_id: str, after_seq: int = -1, limit: int = 5000) -> list[dict[str, Any]]:
    with connect() as con:
        rows = con.execute("SELECT data FROM task_events WHERE task_id=? AND seq>? "
                           "ORDER BY seq LIMIT ?", (task_id, int(after_seq), int(limit))).fetchall()
    return [_j(r["data"], {}) for r in rows]


def last_seq(task_id: str) -> int:
    with connect() as con:
        r = con.execute("SELECT MAX(seq) AS m FROM task_events WHERE task_id=?", (task_id,)).fetchone()
    return int(r["m"]) if r and r["m"] is not None else -1


# --------------------------------------------------------------------------- #
# Approvals
# --------------------------------------------------------------------------- #


def _approval_row(r: sqlite3.Row) -> dict[str, Any]:
    return {"request_id": r["request_id"], "task_id": r["task_id"], "tool": r["tool"],
            "args_summary": r["args_summary"], "scopes": _j(r["scopes"], ["once"]),
            "call_id": r["call_id"], "requested_at": r["requested_at"], "decision": r["decision"],
            "scope": r["scope"], "decided_by": r["decided_by"], "decided_at": r["decided_at"]}


def get_approval(request_id: str) -> dict[str, Any] | None:
    with connect() as con:
        r = con.execute("SELECT * FROM approvals WHERE request_id=?", (request_id,)).fetchone()
    return _approval_row(r) if r else None


def list_approvals(task_id: str, *, pending_only: bool = False) -> list[dict[str, Any]]:
    q = "SELECT * FROM approvals WHERE task_id=?"
    if pending_only:
        q += " AND decision IS NULL"
    with connect() as con:
        rows = con.execute(q + " ORDER BY requested_at, rowid", (task_id,)).fetchall()
    return [_approval_row(r) for r in rows]


def pending_approval_count(task_ids: list[str]) -> dict[str, int]:
    if not task_ids:
        return {}
    with connect() as con:
        rows = con.execute(
            f"SELECT task_id, COUNT(*) AS n FROM approvals WHERE decision IS NULL "
            f"AND task_id IN ({','.join('?' * len(task_ids))}) GROUP BY task_id", task_ids).fetchall()
    return {r["task_id"]: int(r["n"]) for r in rows}


# --------------------------------------------------------------------------- #
# Spend ledger (feeds Budget.max_usd_per_day, per agent and per profile)
# --------------------------------------------------------------------------- #


def spent_today(kind: str, subject: str, day: str | None = None) -> float:
    with connect() as con:
        r = con.execute("SELECT usd FROM agent_spend WHERE day=? AND kind=? AND subject=?",
                        (day or today(), kind, subject)).fetchone()
    return float(r["usd"]) if r else 0.0


def spend_for_day(day: str | None = None) -> list[dict[str, Any]]:
    with connect() as con:
        rows = con.execute("SELECT * FROM agent_spend WHERE day=? ORDER BY kind, subject",
                           (day or today(),)).fetchall()
    return [{"day": r["day"], "kind": r["kind"], "subject": r["subject"], "usd": round(r["usd"], 6),
             "tokens": r["tokens"], "turns": r["turns"], "tasks": r["tasks"]} for r in rows]


def _add_spend(con: sqlite3.Connection, day: str, agent_id: str, profile: str, usd: float,
               tokens: int, turns: int, tasks: int) -> None:
    for kind, subject in (("agent", agent_id), ("profile", profile or "")):
        if not subject:
            continue
        con.execute(
            "INSERT INTO agent_spend (day,kind,subject,usd,tokens,turns,tasks) VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT(day,kind,subject) DO UPDATE SET usd=usd+excluded.usd, "
            "tokens=tokens+excluded.tokens, turns=turns+excluded.turns, tasks=tasks+excluded.tasks",
            (day, kind, subject, usd, tokens, turns, tasks))


def add_spend(agent_id: str, profile: str, usd: float = 0.0, tokens: int = 0, turns: int = 0,
              tasks: int = 0, day: str | None = None) -> None:
    with _lock, connect() as con:
        _add_spend(con, day or today(), agent_id, profile, usd, tokens, turns, tasks)


# --------------------------------------------------------------------------- #
# Batched writes: the task manager's single writer runs one transaction per batch
# --------------------------------------------------------------------------- #

Op = Callable[[sqlite3.Connection], None]


def op_event(task_id: str, ev: dict[str, Any]) -> Op:
    row = event_row(task_id, ev)

    def run(con: sqlite3.Connection) -> None:
        con.execute("INSERT OR IGNORE INTO task_events (task_id,seq,ts,type,data) VALUES (?,?,?,?,?)", row)
    return run


def op_task(task_id: str, **fields: Any) -> Op:
    def run(con: sqlite3.Connection) -> None:
        _update_task(con, task_id, fields)
    return run


def op_approval_add(row: dict[str, Any]) -> Op:
    def run(con: sqlite3.Connection) -> None:
        con.execute("INSERT OR IGNORE INTO approvals (request_id,task_id,tool,args_summary,scopes,"
                    "call_id,requested_at) VALUES (?,?,?,?,?,?,?)",
                    (row["request_id"], row["task_id"], row["tool"], row["args_summary"],
                     json.dumps(row["scopes"]), row.get("call_id"), row["requested_at"]))
    return run


def op_approval_decide(request_id: str, decision: str, scope: str | None, decided_by: str | None,
                       only_if_pending: bool = True) -> Op:
    def run(con: sqlite3.Connection) -> None:
        q = ("UPDATE approvals SET decision=?, scope=?, decided_by=?, decided_at=? "
             "WHERE request_id=?" + (" AND decision IS NULL" if only_if_pending else ""))
        con.execute(q, (decision, scope, decided_by, now_iso(), request_id))
    return run


def op_approvals_expire(task_id: str) -> Op:
    def run(con: sqlite3.Connection) -> None:
        con.execute("UPDATE approvals SET decision='expired', decided_at=? "
                    "WHERE task_id=? AND decision IS NULL", (now_iso(), task_id))
    return run


def op_spend(agent_id: str, profile: str, usd: float, tokens: int, turns: int, tasks: int = 0) -> Op:
    day = today()

    def run(con: sqlite3.Connection) -> None:
        _add_spend(con, day, agent_id, profile, usd, tokens, turns, tasks)
    return run


def run_batch(ops: list[Op]) -> None:
    """All of ``ops`` in ONE transaction (all or nothing)."""
    if not ops:
        return
    with _lock, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        try:
            for op in ops:
                op(con)
        except BaseException:
            con.execute("ROLLBACK")
            raise
        con.execute("COMMIT")


# --------------------------------------------------------------------------- #
# Retention
# --------------------------------------------------------------------------- #


def purge(retention_days: int, *, now: datetime | None = None) -> list[str]:
    """Delete the transcript of every finished task older than ``retention_days``.

    Removes its events, clears the prompt and the result text and blanks the approval
    arguments. The task row itself (who, which agent, cost, duration, outcome) stays: it is
    the audit trail. Returns the task ids purged, so the caller can delete their workspaces.
    ``retention_days <= 0`` means "keep for ever".
    """
    if retention_days <= 0:
        return []
    cutoff = ((now or datetime.now(UTC)) - timedelta(days=retention_days)).isoformat(timespec="seconds")
    with _lock, connect() as con:
        rows = con.execute(
            "SELECT task_id FROM tasks WHERE purged=0 AND state IN ('succeeded','failed','cancelled') "
            "AND COALESCE(finished_at, created_at) < ?", (cutoff,)).fetchall()
        ids = [r["task_id"] for r in rows]
        if not ids:
            return []
        marks = ",".join("?" * len(ids))
        con.execute("BEGIN IMMEDIATE")
        try:
            con.execute(f"DELETE FROM task_events WHERE task_id IN ({marks})", ids)
            con.execute(f"UPDATE approvals SET args_summary='' WHERE task_id IN ({marks})", ids)
            con.execute(f"UPDATE tasks SET prompt=NULL, result_text=NULL, purged=1 "
                        f"WHERE task_id IN ({marks})", ids)
            con.execute("DELETE FROM agent_spend WHERE day < ?", (_day_cutoff(now),))
        except BaseException:
            con.execute("ROLLBACK")
            raise
        con.execute("COMMIT")
    return ids


def _day_cutoff(now: datetime | None) -> str:
    """Spend rows are tiny, but not forever: 400 days."""
    return ((now or datetime.now()) - timedelta(days=400)).strftime("%Y-%m-%d")


# --------------------------------------------------------------------------- #
# Support bundle
# --------------------------------------------------------------------------- #


def redacted_summary() -> dict[str, Any]:
    """What a support bundle may say about agents: counts and configuration shape, NEVER a
    prompt, an instruction, a result, an event, a path, a secret name or a person's name.

    (W5 owns ``/api/support-bundle``; it embeds this under the ``agents`` key.)
    """
    with connect() as con:
        by_state = {r["state"]: r["n"] for r in con.execute(
            "SELECT state, COUNT(*) AS n FROM tasks GROUP BY state").fetchall()}
        by_backend = {r["backend"]: r["n"] for r in con.execute(
            "SELECT backend, COUNT(*) AS n FROM tasks GROUP BY backend").fetchall()}
        events = int(con.execute("SELECT COUNT(*) FROM task_events").fetchone()[0])
        approvals = {str(r["decision"] or "pending"): r["n"] for r in con.execute(
            "SELECT decision, COUNT(*) AS n FROM approvals GROUP BY decision").fetchall()}
        spend = [{"kind": r["kind"], "usd_estimate": round(r["usd"], 4), "tasks": r["tasks"]}
                 for r in con.execute("SELECT kind, SUM(usd) AS usd, SUM(tasks) AS tasks FROM agent_spend "
                                      "WHERE day=? GROUP BY kind", (today(),)).fetchall()]
    agents = []
    for a in list_agents():
        spec = a["spec"] if isinstance(a["spec"], dict) else {}
        agents.append({
            "backend": spec.get("backend"), "disabled": a["disabled"],
            "published_to_count": len(a["published_to"]),
            "tools": len(spec.get("tool_allowlist") or []),
            "mcp_servers": len(spec.get("mcp_servers") or {}),
            "approval_mode": (spec.get("approval_policy") or {}).get("mode"),
            "workspace_mode": (spec.get("workspace_policy") or {}).get("mode"),
            "trust_repo_settings": bool((spec.get("workspace_policy") or {}).get("trust_repo_settings")),
            "has_budget": bool(spec.get("budget")),
        })
    settings = {k: v for k, v in all_settings().items()}
    return {"agents": agents, "tasks_by_state": by_state, "tasks_by_backend": by_backend,
            "task_events": events, "approvals": approvals, "spend_today": spend,
            "settings": settings, "prompts_included": False}
