"""A scratch data directory for the profile-lifecycle suites (WP-PROF).

``scratch`` points every path module at a throwaway directory, creates the real chats.db schema and
hands back a :class:`Scratch` with small builders (profiles, chats, pictures, files, bots) and
:meth:`Scratch.grep`, the check the Reset acceptance test is built on: look through EVERY file of
the data directory (databases and their write-ahead logs included) for a marker string.

Everything here is neutral: profiles ``Ada``/``Bram``/..., bots ``pack_otter``/``pack_heron``,
markers ``SENT-<who>-<what>``.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Callable
from pathlib import Path

import pytest
import yaml

from app import accounts, auth, config, connections, ext_profile_hooks, ownership, settings_store
from app.contracts.permissions import Role

HOUSEHOLD = [
    {"id": "pack_otter", "name": "Otter", "emoji": "🦦", "order": 0,
     "advisor": {"providers": [{"provider": "openai_compatible", "model": "m1"}], "tools": True}},
    {"id": "pack_heron", "name": "Heron", "emoji": "🪶", "order": 1,
     "advisor": {"providers": [{"provider": "openai_compatible", "model": "m1"}], "tools": True},
     "voice": "builtin:af_test"},
    {"id": "helper", "name": "Helper", "emoji": "🔧", "order": 2, "agent": "helper"},      # agent bot
]


class Scratch:
    def __init__(self, root: Path) -> None:
        self.root = root

    # ---- running async Database code ------------------------------------ #
    def run(self, fn: Callable):
        """Run ``async fn(db)`` against chats.db with a Database that is closed afterwards."""
        from app.database import Database

        async def go():
            db = Database(self.root / "chats.db")
            await db.connect()
            try:
                return await fn(db)
            finally:
                await db.close()
        return asyncio.run(go())

    # ---- people and bots ------------------------------------------------ #
    def admin(self, name: str = "Ada") -> accounts.Profile:
        return accounts.create_profile(name, Role.ADMIN, protected=True)

    def profile(self, name: str, role: Role | str = Role.USER, **kw) -> accounts.Profile:
        return accounts.create_profile(name, role, **kw)

    def connection(self, *, default: bool = True, model: str = "m1", name: str = "Main"):
        c = connections.create(type="openai_compatible", name=name,
                               base_url="http://127.0.0.1:9/v1", default_model=model)
        if default:
            assert settings_store.set_many({"connections.default": c.id}).ok
        return c

    def bots(self) -> list:
        return config.load_bots()

    def personal(self, pid: str) -> list:
        return [b for b in config.load_bots() if b.scope == "personal" and b.owner_profile == pid]

    # ---- content -------------------------------------------------------- #
    def thread(self, owner: str | None, bot_id: str = "pack_otter", title: str = "chat", *,
               messages: list[tuple] | None = None, snapshot: str | None = None) -> str:
        """A chat. ``messages`` = (role, content[, media_url[, metadata]]) tuples."""
        async def go(db):
            t = await db.create_thread(bot_id, title, owner_profile_id=owner)
            for m in messages or [("user", f"hello {title}")]:
                role, content, *rest = m
                await db.add_message(t.id, role, content, media_url=rest[0] if rest else None,
                                     metadata=rest[1] if len(rest) > 1 else None)
            if snapshot:
                await db.db.execute("UPDATE threads SET avatar_snapshot = ? WHERE id = ?", (snapshot, t.id))
            return t.id
        return self.run(go)

    def media(self, name: str, content: bytes) -> Path:
        d = Path(config.MEDIA_DIR)
        d.mkdir(parents=True, exist_ok=True)
        p = d / name
        p.write_bytes(content)
        return p

    def snapshot(self, sid: str, content: bytes) -> Path:
        d = self.root / "avatar-snapshots"
        d.mkdir(parents=True, exist_ok=True)
        p = d / sid
        p.write_bytes(content)
        return p

    def file_row(self, owner: str | None, name: str, blob: bytes, *, source: str = "chat") -> tuple[dict, Path]:
        stored = f"{abs(hash(name)) % 10**10}-{name}"
        d = Path(config.FILES_DIR)
        d.mkdir(parents=True, exist_ok=True)
        (d / stored).write_bytes(blob)

        async def go(db):
            return await db.add_file(name, stored, len(blob), "text/plain", source, owner_profile_id=owner)
        return self.run(go), d / stored

    def image_job(self, thread_id: str, message_id: str, spec: str, bot_id: str = "pack_otter",
                  state: str = "queued") -> str:
        async def go(db):
            await db.add_image_job(f"job-{message_id[:8]}", message_id, thread_id, bot_id, spec, state=state)
            return f"job-{message_id[:8]}"
        return self.run(go)

    def message_ids(self, thread_id: str) -> list[str]:
        async def go(db):
            return [m.id for m in await db.dump_messages(thread_id)]
        return self.run(go)

    def ledger(self, rows: list[dict]) -> Path:
        p = self.root / "apps-data" / "imagestudio" / "images.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
        return p

    def advisor_request(self, thread_id: str, brief: str, bot_id: str = "pack_otter") -> None:
        from app.advisor import Ledger
        led = Ledger(self.root / "advisor.db")
        led.add(bot_id=bot_id, thread_id=thread_id, kind="research", agent="ds_flash", brief=brief,
                state="done", context="")
        led._conn.close()

    def agent_task(self, owner: str, prompt: str, origin_thread: str | None = None) -> str:
        con = sqlite3.connect(self.root / "chats.db")
        tid = f"task-{abs(hash(prompt)) % 10**8}"
        con.execute(
            "INSERT INTO tasks (task_id, agent_id, backend, owner, state, spec_json, prompt, result_text, "
            "created_at, origin_thread_id) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (tid, "agent1", "openclaw", owner, "succeeded", "{}", prompt, f"result of {prompt}",
             "2026-01-01T00:00:00+00:00", origin_thread))
        con.execute("INSERT INTO task_events (task_id, seq, ts, type, data) VALUES (?,?,?,?,?)",
                    (tid, 0, 1.0, "text", json.dumps({"t": f"event of {prompt}"})))
        con.commit()
        con.close()
        return tid

    def voice_cursor(self, key: str, meta: str) -> Path:
        d = self.root / "ext" / "voice"
        d.mkdir(parents=True, exist_ok=True)
        p = d / "cursor.sqlite"
        con = sqlite3.connect(p)
        con.execute("CREATE TABLE IF NOT EXISTS cursor (k TEXT PRIMARY KEY, upto INTEGER, open_from INTEGER, "
                    "meta TEXT, updated REAL NOT NULL, used REAL NOT NULL)")
        con.execute("INSERT OR REPLACE INTO cursor VALUES (?,?,?,?,?,?)", (json.dumps([key, "s1"]), 5, None, meta, 1.0, 1.0))
        con.commit()
        con.close()
        return p

    def voice_acks(self, doc: dict) -> Path:
        p = self.root / "voice-acks.yaml"
        p.write_text(yaml.safe_dump(doc), encoding="utf-8")
        return p

    # ---- the acceptance check ------------------------------------------- #
    def grep(self, *needles: str) -> dict[str, list[str]]:
        """Which files under the data directory hold each needle (bytes, so SQLite files, their
        write-ahead logs and free pages count). Empty lists mean "no trace"."""
        found: dict[str, list[str]] = {n: [] for n in needles}
        for p in sorted(self.root.rglob("*")):
            if not p.is_file():
                continue
            data = p.read_bytes()
            for n in needles:
                if n.encode() in data:
                    found[n].append(str(p.relative_to(self.root)))
        return found


def _reference_hooks(monkeypatch) -> None:
    """The bodies WP-CORE gives the four host hooks, written out from their docstrings, so this
    package is tested against the contract rather than against Foundation's permissive stubs.
    (Once WP-CORE is merged the real ones do the same thing and this just restates them.)"""
    def count(profile_id):
        total: dict[str, int] = {}
        for c, _p in ext_profile_hooks.registered_profile_data().values():
            if c is None:
                continue
            try:
                for k, v in dict(c(profile_id)).items():
                    total[k] = total.get(k, 0) + int(v)
            except Exception:
                pass
        return total

    def purge(profile_id):
        n = 0
        for _c, p in ext_profile_hooks.registered_profile_data().values():
            if p is None:
                continue
            try:
                n += int(p(profile_id) or 0)
            except Exception:
                pass
        return n

    def allowed(voice_id, bot_owner):
        if ext_profile_hooks.is_household_voice(voice_id):
            return True
        rules = ext_profile_hooks.registered_voice_access().values()
        for fn in rules:
            try:
                if not fn(voice_id, bot_owner):
                    return False
            except Exception:
                return False
        return True

    monkeypatch.setattr(ext_profile_hooks, "count_profile_data", count)
    monkeypatch.setattr(ext_profile_hooks, "purge_profile_data", purge)
    monkeypatch.setattr(ext_profile_hooks, "voice_allowed", allowed)
    monkeypatch.setattr(ext_profile_hooks, "voice_owned_clones_active",
                        lambda: bool(ext_profile_hooks.registered_voice_access()))


@pytest.fixture
def scratch(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "config.yaml")
    monkeypatch.setattr(config, "MEDIA_DIR", tmp_path / "media")
    monkeypatch.setattr(config, "FILES_DIR", tmp_path / "files")
    monkeypatch.setattr(config, "AVATAR_DIR", tmp_path / "avatars")
    monkeypatch.setattr(config, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(config, "BACKUP_DIR", tmp_path / "backups")
    monkeypatch.setattr(auth, "SECURITY_PATH", tmp_path / "security.yaml")
    monkeypatch.setattr(auth, "RECOVERY_PATH", tmp_path / "RECOVERY-CODE.txt")
    monkeypatch.setattr(auth, "TRUSTED_PATH", tmp_path / "trusted-devices.yaml")
    monkeypatch.setattr(auth, "SETUP_TOKEN_PATH", tmp_path / "SETUP-TOKEN.txt")
    (tmp_path / "config.yaml").write_text(yaml.safe_dump({"bots": HOUSEHOLD}, sort_keys=False))
    config._invalidate_bots_cache()
    accounts.reset_for_tests()
    ownership.forget_all()
    ext_profile_hooks.reset_for_tests()
    _reference_hooks(monkeypatch)
    auth._cache = None
    auth._trusted = None
    s = Scratch(tmp_path)
    s.run(lambda db: asyncio.sleep(0))            # creates chats.db with the real schema
    yield s
    ext_profile_hooks.reset_for_tests()
    ownership.forget_all()
