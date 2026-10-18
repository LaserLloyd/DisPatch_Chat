"""The host half of the voice consolidation (W6b): the models folder, owner-only state, the
extension credential store, the settings-store seam, the provider listing, the one-time moves.

The add-on is never installed here; tests/_extfake.py plays its part (see test_extensions.py).
"""
from __future__ import annotations

import logging
import os
import stat
import sys
import types
from pathlib import Path

import pytest
from _extfake import KNOBS, ext_env, install

from app import config, extensions

POSIX = os.name != "nt"


@pytest.fixture(autouse=True)
def _no_keyring(monkeypatch):
    """The credential store falls back to its private file when no keyring is importable
    (a headless box, a container); the dev machine's own keyring must never be touched."""
    monkeypatch.setitem(sys.modules, "keyring", None)
    monkeypatch.delenv("DISPATCH_SPEECH_MODELS", raising=False)
    monkeypatch.delenv("DISPATCH_MODELS_MIGRATE", raising=False)
    extensions._secret_stores.clear()


# --------------------------------------------------------------------------- #
# ExtPaths: <data>/models and owner-only state
# --------------------------------------------------------------------------- #

def test_models_dir_is_models_not_speech_models(ext_env):
    _c, tmp = ext_env
    install()
    p = KNOBS.ctx.paths
    assert p.models_dir == tmp / "models" and p.models_dir.name == "models"


def test_models_dir_env_is_the_operators_choice(ext_env, monkeypatch, tmp_path):
    install()
    monkeypatch.setenv("DISPATCH_SPEECH_MODELS", str(tmp_path / "mine"))
    assert KNOBS.ctx.paths.models_dir == tmp_path / "mine"


@pytest.mark.skipif(not POSIX, reason="POSIX mode bits")
def test_state_dir_and_ext_are_owner_only(ext_env):
    _c, tmp = ext_env
    install()
    p = KNOBS.ctx.paths
    assert stat.S_IMODE(p.state_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE((tmp / "ext").stat().st_mode) == 0o700


@pytest.mark.skipif(not POSIX, reason="POSIX mode bits")
def test_a_data_dir_this_call_creates_is_owner_only_and_an_existing_one_is_left(tmp_path):
    fresh = tmp_path / "fresh-data"
    extensions._ensure_private_tree(fresh, fresh / "ext" / "voice")
    assert stat.S_IMODE(fresh.stat().st_mode) == 0o700
    shared = tmp_path / "shared-data"
    shared.mkdir()
    os.chmod(shared, 0o750)                       # a bind mount another service user reads, on purpose
    extensions._ensure_private_tree(shared, shared / "ext" / "voice")
    assert stat.S_IMODE(shared.stat().st_mode) == 0o750
    assert stat.S_IMODE((shared / "ext" / "voice").stat().st_mode) == 0o700


def test_ensure_private_calls_the_fsperm_helper_for_both_folders(tmp_path, monkeypatch):
    calls: list[tuple[str, bool]] = []
    monkeypatch.setattr(extensions.fsperm, "ensure_private",
                        lambda p, directory=False: calls.append((Path(p).name, directory)) or True)
    extensions._ensure_private_tree(tmp_path / "d", tmp_path / "d" / "ext" / "voice")
    assert calls == [("d", True), ("ext", True), ("voice", True)]       # created data, ext, state
    calls.clear()
    extensions._ensure_private_tree(tmp_path / "d", tmp_path / "d" / "ext" / "voice")
    assert calls == []                                                     # once per process


def test_fsperm_on_this_platform(tmp_path):
    from app import fsperm
    d = tmp_path / "priv"
    d.mkdir()
    assert fsperm.restrict(d, directory=True) is True
    f = tmp_path / "priv" / "x"
    f.write_text("x")
    assert fsperm.ensure_private(f) is True
    assert fsperm.others_can_read(f) in (False, None)       # None: a filesystem with no ACLs
    if POSIX:
        assert stat.S_IMODE(f.stat().st_mode) == 0o600 and fsperm.mode_is_private(f) is True


# --------------------------------------------------------------------------- #
# The one-time move of <data>/speech-models
# --------------------------------------------------------------------------- #

def _seed(base: Path, *names: str, folder: str = "speech-models") -> None:
    for n in names:
        f = base / folder / n
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(n.encode())


def test_dry_run_reports_and_moves_nothing(tmp_path):
    _seed(tmp_path, "speech/silero_vad.onnx", "chatterbox/tokenizer.json")
    rep = extensions.migrate_models_dir(dry_run=True, data=tmp_path)
    assert rep["dry_run"] and rep["action"] == "rename" and rep["moved"] == ["chatterbox", "speech"]
    assert (tmp_path / "speech-models" / "speech" / "silero_vad.onnx").exists()
    assert not (tmp_path / "models").exists()


def test_the_move_is_one_rename_and_idempotent(tmp_path):
    _seed(tmp_path, "speech/silero_vad.onnx", "chatterbox/tokenizer.json")
    rep = extensions.migrate_models_dir(dry_run=False, data=tmp_path)
    assert rep["action"] == "rename" and rep["errors"] == []
    assert (tmp_path / "models" / "speech" / "silero_vad.onnx").read_bytes() == b"speech/silero_vad.onnx"
    assert not (tmp_path / "speech-models").exists()
    again = extensions.migrate_models_dir(dry_run=False, data=tmp_path)
    assert again["action"] == "none" and again["moved"] == []


def test_an_empty_new_folder_is_replaced(tmp_path):
    _seed(tmp_path, "silero_vad.onnx")
    (tmp_path / "models").mkdir()
    rep = extensions.migrate_models_dir(dry_run=False, data=tmp_path)
    assert rep["action"] == "rename" and (tmp_path / "models" / "silero_vad.onnx").exists()


def test_both_folders_with_files_merge_without_overwriting(tmp_path):
    _seed(tmp_path, "silero_vad.onnx", "only-old.bin")
    _seed(tmp_path, "silero_vad.onnx", "new-only.bin", folder="models")
    (tmp_path / "models" / "silero_vad.onnx").write_bytes(b"NEW")
    rep = extensions.migrate_models_dir(dry_run=False, data=tmp_path)
    assert rep["action"] == "merge" and rep["moved"] == ["only-old.bin"] and rep["skipped"] == ["silero_vad.onnx"]
    assert (tmp_path / "models" / "silero_vad.onnx").read_bytes() == b"NEW"          # never overwritten
    assert (tmp_path / "models" / "only-old.bin").exists()
    assert (tmp_path / "speech-models" / "silero_vad.onnx").exists()                 # kept: not moved, not deleted
    assert extensions.migrate_models_dir(dry_run=False, data=tmp_path)["moved"] == []


def test_the_operators_own_folder_is_never_touched(tmp_path, monkeypatch):
    _seed(tmp_path, "silero_vad.onnx")
    monkeypatch.setenv("DISPATCH_SPEECH_MODELS", str(tmp_path / "elsewhere"))
    rep = extensions.migrate_models_dir(dry_run=False, data=tmp_path)
    assert rep["action"] == "explicit" and (tmp_path / "speech-models" / "silero_vad.onnx").exists()


def test_a_folder_that_cannot_be_renamed_is_reported_and_left(tmp_path, monkeypatch):
    """A mount point (a user's bind mount at /data/speech-models) cannot be renamed."""
    _seed(tmp_path, "silero_vad.onnx")

    def busy(self, target):
        raise OSError(16, "Device or resource busy")
    monkeypatch.setattr(Path, "rename", busy)
    rep = extensions.migrate_models_dir(dry_run=False, data=tmp_path)
    assert rep["action"] == "error" and rep["errors"]
    assert (tmp_path / "speech-models" / "silero_vad.onnx").exists()


def test_a_symlinked_old_folder_is_left_alone(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    (real / "f").write_text("x")
    try:
        os.symlink(real, tmp_path / "speech-models", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("no symlinks here")
    assert extensions.migrate_models_dir(dry_run=False, data=tmp_path)["action"] == "none"


def test_loading_the_voice_add_on_moves_the_folder_before_setup(ext_env):
    _c, tmp = ext_env
    _seed(tmp, "speech/silero_vad.onnx")
    seen = {}
    real_setup = __import__("_extfake").setup

    def spy(ctx):
        seen["models"] = sorted(p.name for p in ctx.paths.models_dir.iterdir())
        return real_setup(ctx)
    import _extfake
    _extfake.setup, old = spy, _extfake.setup
    try:
        install()
    finally:
        _extfake.setup = old
    assert seen["models"] == ["speech"] and not (tmp / "speech-models").exists()


def test_the_start_up_switch_can_say_dry_run_or_off(ext_env, monkeypatch, caplog):
    _c, tmp = ext_env
    _seed(tmp, "silero_vad.onnx")
    monkeypatch.setenv("DISPATCH_MODELS_MIGRATE", "dry-run")
    with caplog.at_level(logging.INFO, logger="local-chat.ext"):
        extensions._migrate_models_at_start()
    assert "in dry run" in caplog.text and (tmp / "speech-models").exists() and not (tmp / "models").exists()
    monkeypatch.setenv("DISPATCH_MODELS_MIGRATE", "off")
    extensions._migrate_models_at_start()
    assert (tmp / "speech-models").exists()
    monkeypatch.setenv("DISPATCH_MODELS_MIGRATE", "auto")
    extensions._migrate_models_at_start()
    assert (tmp / "models" / "silero_vad.onnx").exists()


# --------------------------------------------------------------------------- #
# ctx.secrets
# --------------------------------------------------------------------------- #

def test_secrets_round_trip_in_a_private_file_under_state_dir(ext_env):
    _c, tmp = ext_env
    install()
    s = KNOBS.ctx.secrets
    assert s.get("speechforge_token") is None and not s.has("speechforge_token")
    s.set("speechforge_token", "tok-value-not-real")  # scrub-ok: test fixture
    assert s.get("speechforge_token") == "tok-value-not-real" and s.has("speechforge_token")
    f = tmp / "ext" / "voice" / "secrets.json"
    assert f.is_file() and s.backend == "file"
    if POSIX:
        assert stat.S_IMODE(f.stat().st_mode) == 0o600
    extensions._secret_stores.clear()                          # "a restart / an image update"
    assert KNOBS.ctx.secrets.get("speechforge_token") == "tok-value-not-real"
    s.delete("speechforge_token")
    assert KNOBS.ctx.secrets.get("speechforge_token") is None


def test_secrets_are_per_extension(ext_env):
    _c, tmp = ext_env
    a, b = extensions._secrets_for("voice"), extensions._secrets_for("other")
    a.set("k", "value-one-not-real")  # scrub-ok: test fixture
    assert b.get("k") is None
    assert (tmp / "ext" / "voice" / "secrets.json").exists() and not (tmp / "ext" / "other" / "secrets.json").exists()


def test_secret_names_and_values_are_validated(ext_env):
    install()
    s = KNOBS.ctx.secrets
    for bad in ("", "../x", "a b", "x" * 65, None):
        with pytest.raises(ValueError):
            s.set(bad, "value-not-real")  # scrub-ok: test fixture
    for bad_v in ("", None, "x" * 5000):
        with pytest.raises(ValueError):
            s.set("ok", bad_v)


def test_a_stored_secret_never_reaches_a_log_line_or_a_repr(ext_env, caplog):
    install()
    from app.agents.secrets import GLOBAL_REDACTOR
    s = KNOBS.ctx.secrets
    value = "tok-very-secret-not-real-0123"  # scrub-ok: test fixture
    s.set("speechforge_token", value)
    assert value not in repr(s) and GLOBAL_REDACTOR.redact(f"Bearer {value}") == "Bearer ***redacted***"


def test_the_secret_store_is_usable_after_setup_has_sealed_the_context(ext_env):
    install()
    assert KNOBS.ctx._sealed
    KNOBS.ctx.secrets.set("late", "late-value-not-real")  # scrub-ok: test fixture
    assert KNOBS.ctx.secrets.get("late") == "late-value-not-real"


# --------------------------------------------------------------------------- #
# ctx.settings_writer
# --------------------------------------------------------------------------- #

def test_without_a_settings_store_the_writer_is_a_no_op_that_logs_once(ext_env, caplog):
    install()
    w = KNOBS.ctx.settings_writer("voice")
    assert not extensions.is_live_writer(w)
    with caplog.at_level(logging.INFO, logger="local-chat.ext"):
        w("voice.speed", 1.1)
        w("voice.rig.url", "ws://x")
    assert caplog.text.count("settings store is not available") == 1


def test_the_writer_defaults_to_the_extensions_own_namespace(ext_env):
    install()
    assert KNOBS.ctx.settings_writer().namespace == "voice"
    assert KNOBS.ctx.settings_writer("voice.rig").namespace == "voice.rig"


def test_another_extensions_namespace_is_refused(ext_env):
    install()
    for ns in ("other", "voiceover", "agents", "voice2.x"):
        with pytest.raises(ValueError):
            KNOBS.ctx.settings_writer(ns)


def _fake_store(monkeypatch, **attrs):
    import importlib.machinery
    store = types.ModuleType("app.settings_store")
    store.__spec__ = importlib.machinery.ModuleSpec("app.settings_store", None)
    for k, v in attrs.items():
        setattr(store, k, v)
    monkeypatch.setitem(sys.modules, "app.settings_store", store)
    return store


def test_with_a_settings_store_the_writer_is_its_writer(ext_env, monkeypatch):
    install()
    got: list = []
    _fake_store(monkeypatch, writer=lambda ns: (lambda k, v: got.append((ns, k, v))))
    w = KNOBS.ctx.settings_writer("voice")
    assert extensions.is_live_writer(w)
    w("voice.speed", 1.2)
    assert got == [("voice", "voice.speed", 1.2)]


def test_a_settings_store_without_a_writer_or_that_raises_is_a_no_op(ext_env, monkeypatch, caplog):
    install()
    _fake_store(monkeypatch)                                           # no writer() yet
    assert not extensions.is_live_writer(KNOBS.ctx.settings_writer("voice"))

    def boom(ns):
        raise RuntimeError("store not ready")
    _fake_store(monkeypatch, writer=boom)
    with caplog.at_level(logging.ERROR, logger="local-chat.ext"):
        assert not extensions.is_live_writer(KNOBS.ctx.settings_writer("voice"))
    assert "settings store failed to load" in caplog.text


def test_a_missing_settings_store_is_not_an_error_in_the_log(ext_env, caplog):
    install()
    with caplog.at_level(logging.WARNING, logger="local-chat.ext"):
        KNOBS.ctx.settings_writer("voice")
    assert "failed" not in caplog.text


# --------------------------------------------------------------------------- #
# migrate_voice_yaml at start
# --------------------------------------------------------------------------- #

def _fake_migration(monkeypatch, calls):
    pkg = types.ModuleType("dispatch_voice")
    host = types.ModuleType("dispatch_voice.host")
    mig = types.ModuleType("dispatch_voice.host.migration")

    def migrate_voice_yaml(writer, dry_run=True, **kw):
        calls.append(dry_run)
        writer("voice.speed", 1.0)
        return {"status": "migrated", "written": 1}
    mig.migrate_voice_yaml = migrate_voice_yaml
    for name, mod in (("dispatch_voice", pkg), ("dispatch_voice.host", host), ("dispatch_voice.host.migration", mig)):
        monkeypatch.setitem(sys.modules, name, mod)


def test_the_voice_yaml_migration_waits_for_a_live_store(ext_env, monkeypatch):
    ext = install()
    calls: list = []
    _fake_migration(monkeypatch, calls)
    extensions._migrate_ext_settings(ext)
    assert calls == []                                       # no store: the add-on is not asked to record a move


def test_the_voice_yaml_migration_runs_for_real_once_the_store_exists(ext_env, monkeypatch):
    ext = install()
    calls: list = []
    written: list = []
    _fake_migration(monkeypatch, calls)
    monkeypatch.setattr(extensions, "_settings_writer", lambda ns: (lambda k, v: written.append((k, v))))
    extensions._migrate_ext_settings(ext)
    assert calls == [False] and written == [("voice.speed", 1.0)]


def test_a_failing_migration_never_stops_the_shell(ext_env, monkeypatch, caplog):
    ext = install()
    _fake_migration(monkeypatch, [])
    sys.modules["dispatch_voice.host.migration"].migrate_voice_yaml = lambda w, dry_run=True: 1 / 0
    monkeypatch.setattr(extensions, "_settings_writer", lambda ns: (lambda k, v: None))
    with caplog.at_level(logging.ERROR, logger="local-chat.ext"):
        extensions._migrate_ext_settings(ext)
    assert "settings migration failed" in caplog.text


def test_only_the_voice_extension_migrates(ext_env, monkeypatch):
    ext = install()
    ext.id = "other"
    calls: list = []
    _fake_migration(monkeypatch, calls)
    monkeypatch.setattr(extensions, "_settings_writer", lambda ns: (lambda k, v: None))
    extensions._migrate_ext_settings(ext)
    assert calls == []


# --------------------------------------------------------------------------- #
# GET /api/voice/providers
# --------------------------------------------------------------------------- #

class _EP:
    def __init__(self, name, dist_name, version):
        self.name, self.value = name, f"{dist_name}.plugin:setup"
        self.dist = types.SimpleNamespace(name=dist_name, version=version)


def test_the_provider_listing_without_the_add_on(ext_env, monkeypatch):
    c, _ = ext_env
    monkeypatch.setattr(extensions, "entry_points", lambda group: [_EP("kokoro", "dispatch-voice-kokoro", "0.4.0")]
                        if group == extensions.VOICE_PROVIDER_GROUP else [])
    r = c.get("/api/voice/providers")
    assert r.status_code == 200
    body = r.json()
    assert body["installed"] is False and body["state"] == "absent" and body["providers"] == {}
    assert body["plugins"] == [{"name": "kokoro", "package": "dispatch-voice-kokoro", "version": "0.4.0",
                                "enabled": False}]
    assert body["permissions"] == {"list": "manage_plugins", "use": "use_voice"}
    assert "dispatch_voice" not in r.text and "plugin:setup" not in r.text      # no module:attr target


def test_with_the_add_on_mounted_its_route_answers(ext_env):
    c, _ = ext_env
    assert c.get("/api/voice/providers").json()["installed"] is False
    install()
    r = c.get("/api/voice/providers")
    assert r.status_code == 200 and r.json()["from"] == "addon"


def test_a_failed_add_on_shows_its_state_in_the_listing(ext_env):
    c, _ = ext_env
    KNOBS.raise_in_setup = RuntimeError("boom")
    install()
    assert c.get("/api/voice/providers").json()["state"] == "failed"


def test_the_listing_is_a_plugin_manager_route(ext_env):
    from app import route_policy
    req = route_policy.declared()[("GET", "/api/voice/providers")]
    assert req.permission == "manage_plugins" and not req.public


def test_no_entry_points_means_an_empty_list_not_an_error(ext_env, monkeypatch):
    c, _ = ext_env
    monkeypatch.setattr(extensions, "entry_points", lambda group: (_ for _ in ()).throw(OSError("bad metadata")))
    assert c.get("/api/voice/providers").json()["plugins"] == []


def test_the_config_module_is_untouched_by_all_this():
    assert config.DATA_DIR is not None


# --------------------------------------------------------------------------- #
# PUT /api/bots/order with a partial entry (the character card sends only voice_rp)
# --------------------------------------------------------------------------- #

def _roster(c):
    return [b["id"] for b in c.get("/api/bots/all").json()["bots"]]


def test_a_partial_entry_with_only_voice_rp_saves_it_and_keeps_the_order(ext_env):
    c, _ = ext_env
    install()
    before = _roster(c)
    assert len(before) >= 3
    target = before[-1]                                    # the LAST character: order 0 would move it to the top
    r = c.put("/api/bots/order", json={"bots": [{"id": target, "voice_rp": "dialogue"}]})
    assert r.status_code == 200, r.text
    assert _roster(c) == before
    row = next(b for b in r.json()["bots"] if b["id"] == target)
    assert row["voice_rp"] == "dialogue"
    assert config.get_bot(target).voice_rp == "dialogue"


def test_a_partial_entry_changes_nothing_else_about_the_character(ext_env):
    c, _ = ext_env
    install()
    target = _roster(c)[1]
    b0 = config.get_bot(target)
    r = c.put("/api/bots/order", json={"bots": [{"id": target, "voice_rp": "all"}]})
    assert r.status_code == 200
    b1 = config.get_bot(target)
    for f in ("safe", "hide_unlocked", "audience", "reactions", "avatar_pool", "image_jobs", "voice", "name"):
        assert getattr(b1, f, None) == getattr(b0, f, None), f
    assert b1.voice_rp == "all"


def test_a_partial_entry_is_validated_like_a_full_one(ext_env):
    c, _ = ext_env
    install()
    target = _roster(c)[0]
    r = c.put("/api/bots/order", json={"bots": [{"id": target, "voice_rp": "mumble"}]})
    assert r.status_code == 422 and "role-play" in r.text
    assert config.get_bot(target).voice_rp == ""


def test_a_partial_entry_for_an_unknown_character_changes_nothing(ext_env):
    c, _ = ext_env
    install()
    before = _roster(c)
    r = c.put("/api/bots/order", json={"bots": [{"id": "no-such-bot", "voice_rp": "all"}]})
    assert r.status_code == 200 and _roster(c) == before


def test_several_partial_entries_and_voice_only_entries(ext_env):
    c, _ = ext_env
    install()
    ids = _roster(c)
    r = c.put("/api/bots/order", json={"bots": [{"id": ids[-1], "voice": "stock-host"},
                                                {"id": ids[0], "voice_rp": "dialogue"}]})
    assert r.status_code == 200, r.text
    assert _roster(c) == ids
    assert config.get_bot(ids[-1]).voice == "stock-host" and config.get_bot(ids[0]).voice_rp == "dialogue"


def test_a_full_reorder_still_reorders(ext_env):
    c, _ = ext_env
    ids = _roster(c)
    rev = list(reversed(ids))
    r = c.put("/api/bots/order", json={"bots": [{"id": b, "order": i} for i, b in enumerate(rev)]})
    assert r.status_code == 200 and _roster(c) == rev
