"""Secret providers and redaction (RESEARCH.md section 5 items 1, 2, 5)."""
from __future__ import annotations

import logging
import os
import sys

import pytest

from app.agents.secrets import (
    GLOBAL_REDACTOR,
    REDACTED,
    SCRUBBED_ENV,
    ChainSecretProvider,
    EnvSecretProvider,
    FileSecretProvider,
    MemorySecretProvider,
    RedactingFilter,
    SecretError,
    SecretRedactor,
    default_provider,
    get_agent_logger,
)

KEY = "sk-ant-api03-ABCDEFGH12345678-SECRETVALUE"  # scrub-ok: a fake key


def test_the_oauth_token_family_is_scrubbed():
    assert "CLAUDE_CODE_OAUTH_TOKEN" in SCRUBBED_ENV      # RESEARCH.md section 5 item 2


def test_file_provider_round_trip_and_delete(tmp_path):
    p = FileSecretProvider(tmp_path / "sub" / "secrets.json")
    assert p.get("ANTHROPIC_API_KEY") is None
    p.set("ANTHROPIC_API_KEY", KEY)
    assert p.get("ANTHROPIC_API_KEY") == KEY
    assert FileSecretProvider(p.path).get("ANTHROPIC_API_KEY") == KEY
    p.delete("ANTHROPIC_API_KEY")
    assert p.get("ANTHROPIC_API_KEY") is None
    assert not list(p.path.parent.glob("*.tmp")), "no temp file left behind"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes; on Windows the data-dir ACL applies")
def test_file_provider_is_0600(tmp_path):
    p = FileSecretProvider(tmp_path / "s.json")
    p.set("k", "v" * 12)
    assert (os.stat(p.path).st_mode & 0o777) == 0o600


def test_a_corrupt_secret_file_reads_as_empty_not_an_exception(tmp_path):
    f = tmp_path / "s.json"
    f.write_text("{not json")
    assert FileSecretProvider(f).get("x") is None


def test_env_provider_is_read_only(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", KEY)
    e = EnvSecretProvider()
    assert e.get("ANTHROPIC_API_KEY") == KEY
    with pytest.raises(SecretError):
        e.set("x", "y")


def test_chain_reads_first_hit_and_writes_to_first_writable(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    mem = MemorySecretProvider()
    chain = ChainSecretProvider([EnvSecretProvider(), mem])
    chain.set("ANTHROPIC_API_KEY", KEY)                       # env is read-only -> memory
    assert mem.get("ANTHROPIC_API_KEY") == KEY and chain.get("ANTHROPIC_API_KEY") == KEY
    monkeypatch.setenv("ANTHROPIC_API_KEY", "from-env-12345678")
    assert chain.get("ANTHROPIC_API_KEY") == "from-env-12345678", "the environment overrides"


def test_default_provider_uses_a_file_when_there_is_no_keyring(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "keyring", None)         # import keyring -> ImportError
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    prov = default_provider(tmp_path)
    prov.set("ANTHROPIC_API_KEY", KEY)
    assert (tmp_path / "agents" / "secrets.json").is_file()
    assert prov.get("ANTHROPIC_API_KEY") == KEY


def test_redactor_masks_registered_values_and_key_shapes():
    r = SecretRedactor()
    r.register("tok-registered-123456")
    r.register("short")                                       # below the minimum length: ignored
    assert r.redact("x tok-registered-123456 y") == f"x {REDACTED} y"
    assert r.redact("short stays") == "short stays"
    assert REDACTED in r.redact("leaked sk-ant-api03-NEVERREGISTERED999 here")  # scrub-ok: a fake key
    assert r.redact_obj({"a": ["tok-registered-123456", {"b": 3}]}) == {"a": [REDACTED, {"b": 3}]}


def test_the_log_filter_scrubs_message_args_and_tracebacks(caplog):
    GLOBAL_REDACTOR.register(KEY)
    lg = get_agent_logger("test_secrets")
    assert any(isinstance(f, RedactingFilter) for f in lg.filters)
    with caplog.at_level(logging.DEBUG, logger="dispatch.agents"):
        lg.warning("key=%s", KEY)
        try:
            raise RuntimeError(f"boom {KEY}")
        except RuntimeError:
            lg.exception("failed with %s", KEY)
    assert KEY not in caplog.text and REDACTED in caplog.text
    assert KEY not in "".join(r.exc_text or "" for r in caplog.records)
