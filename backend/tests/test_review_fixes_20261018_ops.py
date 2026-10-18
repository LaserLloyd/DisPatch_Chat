"""Operational fixes from the 3.0.0 independent review (2026-10-18).

- Settings -> Backups lists the container entrypoint's pre-upgrade copy (``<old image
  version>-YYYYMMDD-HHMMSS``) beside app/migrate.py's ``3.0.0-...`` copy, and the
  prune keeps the newest five by stamp whatever version sits in front.
- ``appearance.default_theme`` offers every theme of the vendored ForgeUI bundle
  (themes.json names them by ``slug``), not the one hard-coded fallback.
- Migration step 003 writes the roster raw: a key the version does not know survives.
Run: cd backend && uv run pytest tests/test_review_fixes_20261018_ops.py
"""
from __future__ import annotations

import json
import sqlite3

import pytest
import yaml

from app import accounts, backups, config, migrate, settings_schema


@pytest.fixture
def data(tmp_path, monkeypatch):
    """A populated data directory, no app instance (as tests/test_backups.py builds it)."""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "config.yaml")
    monkeypatch.setattr(config, "BACKUP_DIR", tmp_path / "backups")
    monkeypatch.setattr(config, "LOG_DIR", tmp_path / "logs")
    config._invalidate_bots_cache()
    accounts.reset_for_tests()
    con = sqlite3.connect(tmp_path / "chats.db")
    con.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, title TEXT)")
    con.commit()
    con.close()
    (tmp_path / "config.yaml").write_text(yaml.safe_dump({"bots": [{"id": "a", "name": "Alpha"}]}), encoding="utf-8")
    return tmp_path


def test_premigration_copies_from_the_entrypoint_are_listed_and_pruned_by_stamp(data):
    root = backups.backup_root()
    for name in ("3.0.0-b1-20260101-020202", "dev-20260101-030303", "3.0.0-20260101-010101",
                 "unrecorded-20260101-040404", "not-a-backup", "3.0.0-20260101T050505Z"):
        d = root / name
        d.mkdir(parents=True)
        (d / "chats.db").write_bytes(b"y")
    names = {r["name"] for r in backups.list_backups() if r["kind"] == "premigration"}
    assert names == {"3.0.0-b1-20260101-020202", "dev-20260101-030303", "3.0.0-20260101-010101",
                     "unrecorded-20260101-040404"}
    assert backups.resolve_id("premigration:dev-20260101-030303")[1].is_dir()
    # migrate.backup() prunes to the newest five BY STAMP, across the two writers.
    (root / "9.9.9-20250101-000000").mkdir()
    (root / "9.9.9-20250101-000000" / "chats.db").write_bytes(b"old")
    migrate.backup("3.0.0")
    kept = sorted(p.name for p in root.iterdir() if p.is_dir() and migrate._BACKUP_DIR_RE.match(p.name))
    assert len(kept) == migrate.KEEP_BACKUPS
    assert "9.9.9-20250101-000000" not in kept           # oldest stamp went, despite the highest version
    assert "3.0.0-20260101-010101" in kept


def test_the_default_theme_setting_offers_every_vendored_theme():
    doc = json.loads((settings_schema._frontend_dir() / "ui-theme" / "themes.json").read_text(encoding="utf-8"))
    items = doc.get("themes") if isinstance(doc, dict) else doc
    slugs = {t["slug"] for t in items}
    choices = set(settings_schema.dynamic_choices("themes"))
    assert choices == slugs and {"glacier", "daylight", "high-contrast"} <= choices


def test_step_003_keeps_keys_it_does_not_know(data, monkeypatch):
    monkeypatch.setattr(config, "CONFIG_PATH", data / "config.yaml")
    (data / "config.yaml").write_text(yaml.safe_dump({"bots": [
        {"id": "kid", "name": "Kit", "safe": True, "hide_unlocked": True, "future_field": {"x": 1}},
        {"id": "old", "name": "Old", "visible": False},
    ]}, sort_keys=False), encoding="utf-8")
    config._invalidate_bots_cache()
    migrate._apply_audience()
    raw = yaml.safe_load((data / "config.yaml").read_text(encoding="utf-8"))["bots"]
    by = {e["id"]: e for e in raw}
    assert by["kid"]["audience"] == "restricted" and by["kid"]["future_field"] == {"x": 1}
    assert by["old"]["audience"] == "members" and by["old"]["visible"] is False
    assert [b.audience for b in config.load_bots()] == ["restricted", "members"]
