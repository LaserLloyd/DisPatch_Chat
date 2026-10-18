"""Text files are read and written as UTF-8 on purpose, not by the platform's default.

On Windows the default is the ANSI code page (cp1252), so a config.yaml that holds a character's
name in Japanese, or an em dash in the recovery file, used to need ``PYTHONUTF8=1`` to survive a
round trip. Every text-mode ``read_text`` / ``write_text`` / ``open`` / ``os.fdopen`` under
``app/`` now names its encoding; this scan keeps it that way (binary modes and non-file ``open``
calls such as ``tarfile.open`` are not text files and are skipped).
"""
from __future__ import annotations

import ast
from pathlib import Path

import yaml

from app import auth, config

APP = Path(__file__).resolve().parent.parent / "app"
NON_FILE_OPENERS = {"tarfile", "Image", "os", "webbrowser", "zipfile", "io", "codecs", "tempfile", "gzip",
                    "sqlite3", "wave", "socket", "asyncio", "lock_path", "im", "self"}


def _mode(call: ast.Call, pos: int):
    if len(call.args) > pos and isinstance(call.args[pos], ast.Constant):
        return call.args[pos].value
    for k in call.keywords:
        if k.arg == "mode" and isinstance(k.value, ast.Constant):
            return k.value.value
    return None


def _offenders(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    bad = []
    for n in ast.walk(tree):
        if not isinstance(n, ast.Call) or any(k.arg in ("encoding", None) for k in n.keywords):
            continue
        f = n.func
        kind = None
        if isinstance(f, ast.Attribute) and f.attr in ("read_text", "write_text"):
            kind = f.attr
        elif isinstance(f, ast.Name) and f.id == "open" and "b" not in str(_mode(n, 1)):
            kind = "open"
        elif isinstance(f, ast.Attribute) and f.attr == "fdopen" and "b" not in str(_mode(n, 1)):
            kind = "os.fdopen"
        elif (isinstance(f, ast.Attribute) and f.attr == "open" and isinstance(f.value, ast.Name)
              and f.value.id not in NON_FILE_OPENERS and "b" not in str(_mode(n, 0))):
            kind = "path.open"
        if kind:
            bad.append(f"{path.relative_to(APP.parent)}:{n.lineno} {kind}")
    return bad


def test_every_text_file_access_under_app_names_its_encoding():
    bad = [b for p in sorted(APP.rglob("*.py")) for b in _offenders(p)]
    assert not bad, "text-mode file access without encoding='utf-8':\n  " + "\n  ".join(bad)


def test_config_and_security_yaml_round_trip_non_ascii(tmp_path, monkeypatch):
    """The two files the whole app starts from, with characters cp1252 cannot hold."""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "config.yaml")
    monkeypatch.setattr(auth, "SECURITY_PATH", tmp_path / "security.yaml")
    text = yaml.safe_dump({"bots": [{"id": "sakura", "name": "桜 — Sakura"}]}, allow_unicode=True)
    (tmp_path / "config.yaml").write_bytes(text.encode("utf-8"))
    raw = yaml.safe_load((tmp_path / "config.yaml").read_text(encoding="utf-8"))
    assert raw["bots"][0]["name"] == "桜 — Sakura"
    (tmp_path / "security.yaml").write_bytes(yaml.safe_dump({"profile_note": "日本語"}, allow_unicode=True).encode())
    auth._cache = None
    cfg = auth.load()                      # must not raise on bytes that are not cp1252
    assert cfg is not None
