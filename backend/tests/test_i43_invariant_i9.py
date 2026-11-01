"""4.3 A3: invariant I9 - no two connections share type, address and key fingerprint.

Report-only (it never merges anything). The fingerprint is a truncated hash: the report holds
connection ids and counts, never a key or a hash of one."""
from __future__ import annotations

from app import config, connections, invariants, secret_store

KEY = "sk-i9-0123456789abcdef"            # scrub-ok: test fixture, not a real credential


def _row(cid, type_="openai", url="https://api.openai.com/v1", key=KEY):
    return {"id": cid, "type": type_, "base_url": url, "key": key}


def _i9(rows):
    w = invariants.World(extra={"connections": rows})
    return [v for v in invariants.check(w) if v.code == "I9"]


def test_two_connections_with_the_same_type_address_and_key_are_reported():
    vs = _i9([_row("openai"), _row("openai-2"), _row("openai-3", url="https://api.openai.com/v1/")])
    assert len(vs) == 1
    v = vs[0]
    assert "openai" in v.subject and "openai-2" in v.subject and "openai-3" in v.subject
    blob = repr(v.to_dict())
    assert KEY not in blob and KEY[:8] not in blob
    import hashlib
    assert hashlib.sha256(KEY.encode()).hexdigest()[:8] not in blob


def test_distinct_connections_are_not_reported():
    assert _i9([_row("a"), _row("b", key="sk-other-0123456789"), _row("c", type_="deepseek"),
                _row("d", url="http://127.0.0.1:1234/v1")]) == []
    # connections without a key are not key-bearing: two keyless LM Studios are fine
    assert _i9([_row("lm", "lmstudio", "http://127.0.0.1:1234/v1", ""),
                _row("lm-2", "lmstudio", "http://127.0.0.1:1234/v1", "")]) == []


def test_i9_reads_the_live_connections(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setenv("DISPATCH_SECRET_STORE", "file")
    monkeypatch.setenv("SOME_API_KEY", KEY)
    secret_store.reset_for_tests()
    connections.create(type="openai", api_key=KEY, conn_id="one")
    connections.create(type="openai", api_key=KEY, conn_id="two")
    connections.create(type="openai", api_key_env="SOME_API_KEY", conn_id="three")   # same value
    connections.create(type="openai", api_key="sk-else-0123456789", conn_id="four")  # scrub-ok: test fixture, not a real credential
    w = invariants.World(live=True)
    vs = invariants._i9(w)
    assert len(vs) == 1 and set(vs[0].subject.split(",")) == {"one", "two", "three"}
    assert KEY not in repr([v.to_dict() for v in vs])
    secret_store.reset_for_tests()
