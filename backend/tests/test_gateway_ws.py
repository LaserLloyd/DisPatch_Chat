

# --- gateway_token(): SecretRef-shaped gateway.auth.token (OpenClaw 2026.10) --

def _write_cfg(tmp_path, token, providers=None):
    import json as _json
    cfg = {"gateway": {"auth": {"mode": "token", "token": token}}}
    if providers:
        cfg["secrets"] = {"providers": providers}
    p = tmp_path / "openclaw.json"
    p.write_text(_json.dumps(cfg))
    return p


def test_gateway_token_literal_and_file_ref(tmp_path, monkeypatch):
    from app.gateway_ws import gateway_token
    monkeypatch.delenv("OPENCLAW_GATEWAY_TOKEN", raising=False)
    assert gateway_token(_write_cfg(tmp_path, "lit-123")) == "lit-123"
    f = tmp_path / "tok"
    f.write_text("file-456\n")
    ref = {"source": "file", "provider": "gw", "id": "value"}
    cfg = _write_cfg(tmp_path, ref, {"gw": {"source": "file", "path": str(f), "mode": "singleValue"}})
    assert gateway_token(cfg) == "file-456"


def test_gateway_token_json_env_and_unresolvable(tmp_path, monkeypatch):
    from app.gateway_ws import gateway_token
    monkeypatch.delenv("OPENCLAW_GATEWAY_TOKEN", raising=False)
    f = tmp_path / "s.json"
    f.write_text('{"a": {"b/c": "json-789"}}')
    ref = {"source": "file", "provider": "j", "id": "/a/b~1c"}
    cfg = _write_cfg(tmp_path, ref, {"j": {"source": "file", "path": str(f)}})
    assert gateway_token(cfg) == "json-789"
    monkeypatch.setenv("SOME_TOK", "env-1")
    assert gateway_token(_write_cfg(tmp_path, {"source": "env", "provider": "default", "id": "SOME_TOK"})) == "env-1"
    # unknown provider / exec / missing file -> None, never raises
    assert gateway_token(_write_cfg(tmp_path, ref, {})) is None
    assert gateway_token(_write_cfg(tmp_path, {"source": "exec", "provider": "x", "id": "y"})) is None
    miss = {"source": "file", "provider": "gw", "id": "value"}
    assert gateway_token(_write_cfg(tmp_path, miss, {"gw": {"source": "file", "path": str(tmp_path / "nope"), "mode": "singleValue"}})) is None
