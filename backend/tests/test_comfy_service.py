"""Unit tests for app.comfy_service — mocked subprocess + httpx, no real
systemctl/tailscale/ComfyUI calls. Run: cd backend && uv run pytest."""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest

_RealAsyncClient = httpx.AsyncClient

# Imported AFTER the capture above on purpose: the module-scoped fixture swaps
# httpx.AsyncClient out, and this test needs the real one to compare against.
from app import comfy_service as cs  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_module_state():
    """These are process-wide singletons in comfy_service; reset between
    tests so one test's mocked state can't leak into the next."""
    cs._svc_lock = asyncio.Lock()
    cs._hostname_cache = None
    yield


@pytest.fixture
def scratch_comfy_env(tmp_path, monkeypatch):
    env_path = tmp_path / "comfy.env"
    env_path.write_text(
        "COMFY_PORT=8188\nCOMFY_ARGS=\nHSA_OVERRIDE_GFX_VERSION=11.5.1\n"
        "PYTORCH_HIP_ALLOC_CONF=expandable_segments:True\nHIP_VISIBLE_DEVICES=0\n"
    )
    monkeypatch.setattr(cs, "COMFY_DIR", tmp_path)
    monkeypatch.setattr(cs, "COMFY_ENV_PATH", env_path)
    return env_path


# --------------------------------------------------------------------------- #
# (a) Flag schema validation
# --------------------------------------------------------------------------- #


def test_write_flags_rejects_unknown_key(scratch_comfy_env):
    with pytest.raises(cs.FlagValidationError) as exc:
        cs.write_flags({"nope": 1})
    assert exc.value.errors == {"nope": "unknown flag"}


def test_write_flags_clamps_int_range(scratch_comfy_env):
    with pytest.raises(cs.FlagValidationError) as exc:
        cs.write_flags({"port": 99})
    assert "port" in exc.value.errors


def test_write_flags_rejects_bad_enum(scratch_comfy_env):
    with pytest.raises(cs.FlagValidationError) as exc:
        cs.write_flags({"vram_mode": "bogus"})
    assert "vram_mode" in exc.value.errors


def test_write_flags_rejects_bad_regex(scratch_comfy_env):
    with pytest.raises(cs.FlagValidationError):
        cs.write_flags({"gfx_override": "not-a-version"})


def test_write_flags_accepts_null_float(scratch_comfy_env):
    result = cs.write_flags({"reserve_vram": None})
    assert result["values"]["reserve_vram"] is None


def test_write_flags_preserves_unmanaged_key(scratch_comfy_env):
    result = cs.write_flags({"disable_mmap": True})
    assert result["values"]["disable_mmap"] is True
    assert result["unmanaged"] == {"HIP_VISIBLE_DEVICES": "0"}
    # survives a second, unrelated write untouched
    result2 = cs.write_flags({"disable_mmap": False})
    assert result2["unmanaged"] == {"HIP_VISIBLE_DEVICES": "0"}


def test_write_flags_is_atomic_and_locked_down(scratch_comfy_env):
    cs.write_flags({"disable_mmap": True})
    assert (scratch_comfy_env.stat().st_mode & 0o777) == 0o600


def test_write_flags_rejects_symlink_outside_comfy_dir(tmp_path, monkeypatch):
    outside = tmp_path / "outside"
    outside.mkdir()
    comfy_dir = tmp_path / "comfy"
    comfy_dir.mkdir()
    target = outside / "comfy.env"
    target.write_text("COMFY_PORT=8188\n")
    link = comfy_dir / "comfy.env"
    link.symlink_to(target)
    monkeypatch.setattr(cs, "COMFY_DIR", comfy_dir)
    monkeypatch.setattr(cs, "COMFY_ENV_PATH", link)
    with pytest.raises(cs.ServiceError):
        cs.write_flags({"disable_mmap": True})


def test_build_args_always_includes_cors_baseline_without_duplicating(scratch_comfy_env):
    cs.write_flags({"disable_mmap": True})
    cs.write_flags({"disable_mmap": False})
    args_line = next(
        line for line in scratch_comfy_env.read_text().splitlines()
        if line.startswith("COMFY_ARGS")
    )
    assert args_line.count("--enable-cors-header") == 1


# --------------------------------------------------------------------------- #
# (b) start() polls health and times out correctly
# --------------------------------------------------------------------------- #


async def test_start_times_out_with_journal_tail(monkeypatch, scratch_comfy_env):
    monkeypatch.setattr(cs, "_START_WAIT_TIMEOUT", 0.05)
    monkeypatch.setattr(cs, "health", AsyncStub(return_value=None))

    async def fake_run(argv, timeout=cs._OP_TIMEOUT, env=None):
        if "journalctl" in argv[0]:
            return 0, "line one\nline two\n", ""
        return 0, "", ""   # systemctl start succeeds
    monkeypatch.setattr(cs, "_run", fake_run)

    with pytest.raises(cs.HealthTimeoutError) as exc:
        await cs._do_start()
    assert "line one" in exc.value.journal_tail


async def test_start_succeeds_once_health_comes_up(monkeypatch, scratch_comfy_env):
    monkeypatch.setattr(cs, "_START_WAIT_TIMEOUT", 5)
    calls = {"n": 0}

    async def flaky_health(port=None):
        calls["n"] += 1
        return None if calls["n"] < 2 else {"system": {"os": "linux"}}
    monkeypatch.setattr(cs, "health", flaky_health)

    async def fake_run(argv, timeout=cs._OP_TIMEOUT, env=None):
        return 0, "", ""
    monkeypatch.setattr(cs, "_run", fake_run)

    result = await cs._do_start()
    assert result == {"healthy": True, "stats": {"system": {"os": "linux"}}}
    assert calls["n"] >= 2


async def test_start_raises_service_error_on_systemctl_failure(monkeypatch, scratch_comfy_env):
    async def fake_run(argv, timeout=cs._OP_TIMEOUT, env=None):
        return 1, "", "Unit comfyui.service not found."
    monkeypatch.setattr(cs, "_run", fake_run)
    with pytest.raises(cs.ServiceError) as exc:
        await cs._do_start()
    assert "not found" in str(exc.value)


# --------------------------------------------------------------------------- #
# (c) gateway command argv is exactly the whitelisted form (never funnel)
# --------------------------------------------------------------------------- #


async def test_gateway_on_argv_is_whitelisted_serve_form(monkeypatch, scratch_comfy_env):
    captured = []

    async def fake_run(argv, timeout=cs._OP_TIMEOUT, env=None):
        captured.append(argv)
        return 0, json.dumps({"Self": {"DNSName": "chat-host.example.ts.net."}}), ""
    monkeypatch.setattr(cs, "_run", fake_run)

    url = await cs._do_gateway_on(8188)
    assert url == "https://chat-host.example.ts.net:8444/"

    serve_calls = [c for c in captured if len(c) > 1 and c[1] == "serve"]
    assert len(serve_calls) == 1
    call = serve_calls[0]
    assert call[-3:] == ["--bg", "--https=8444", "http://127.0.0.1:8188"]
    assert "tailscale" in call[0]
    for c in captured:
        assert "funnel" not in " ".join(c).lower()


async def test_gateway_off_argv_is_whitelisted_serve_form(monkeypatch):
    captured = []

    async def fake_run(argv, timeout=cs._OP_TIMEOUT, env=None):
        captured.append(argv)
        return 0, "", ""
    monkeypatch.setattr(cs, "_run", fake_run)

    await cs._do_gateway_off()
    assert captured[0][1:] == ["serve", "--https=8444", "off"]
    assert "funnel" not in " ".join(captured[0]).lower()


async def test_gateway_status_argv_and_parsing(monkeypatch):
    captured = []

    async def fake_run(argv, timeout=cs._OP_TIMEOUT, env=None):
        captured.append(argv)
        if argv[1:3] == ["serve", "status"]:
            return 0, json.dumps({"TCP": {"8444": {"HTTPS": True}}}), ""
        return 0, json.dumps({"Self": {"DNSName": "chat-host.example.ts.net."}}), ""
    monkeypatch.setattr(cs, "_run", fake_run)

    status = await cs.gateway_status()
    assert status == {"on": True, "url": "https://chat-host.example.ts.net:8444/"}
    assert captured[0][1:3] == ["serve", "status"]
    for c in captured:
        assert "funnel" not in " ".join(c).lower()


async def test_tailscale_failure_raises_typed_unavailable_error(monkeypatch):
    async def fake_run(argv, timeout=cs._OP_TIMEOUT, env=None):
        return 1, "", "tailscaled not running"
    monkeypatch.setattr(cs, "_run", fake_run)
    with pytest.raises(cs.TailscaleUnavailableError):
        await cs.gateway_status()


async def test_tailscale_operator_error_gets_actionable_hint(monkeypatch):
    async def fake_run(argv, timeout=cs._OP_TIMEOUT, env=None):
        return 1, "", "access denied: not the operator"
    monkeypatch.setattr(cs, "_run", fake_run)
    with pytest.raises(cs.TailscaleUnavailableError) as exc:
        await cs.gateway_status()
    assert "operator" in str(exc.value).lower()


# --------------------------------------------------------------------------- #
# (d) launch() composes start -> gateway -> URL
# --------------------------------------------------------------------------- #


async def test_launch_composes_start_then_gateway(monkeypatch, scratch_comfy_env):
    order = []

    async def fake_do_start():
        order.append("start")
        return {"healthy": True, "stats": {}}

    async def fake_do_gateway_on(comfy_port=None):
        order.append("gateway")
        return "https://chat-host.example.ts.net:8444/"

    monkeypatch.setattr(cs, "_do_start", fake_do_start)
    monkeypatch.setattr(cs, "_do_gateway_on", fake_do_gateway_on)

    result = await cs.launch()
    assert order == ["start", "gateway"]
    assert result == {"url": "https://chat-host.example.ts.net:8444/"}


# --------------------------------------------------------------------------- #
# Concurrency: a second in-flight op gets ServiceBusyError, not queued
# --------------------------------------------------------------------------- #


async def test_concurrent_start_calls_one_gets_busy(monkeypatch, scratch_comfy_env):
    async def slow_do_start():
        await asyncio.sleep(0.05)
        return {"healthy": True, "stats": {}}
    monkeypatch.setattr(cs, "_do_start", slow_do_start)

    results = await asyncio.gather(cs.start(), cs.start(), return_exceptions=True)
    oks = [r for r in results if isinstance(r, dict)]
    errs = [r for r in results if isinstance(r, BaseException)]
    assert len(oks) == 1 and len(errs) == 1
    assert isinstance(errs[0], cs.ServiceBusyError)


# --------------------------------------------------------------------------- #
# health() — mocked httpx (real MockTransport, no real network)
# --------------------------------------------------------------------------- #


async def test_health_returns_parsed_stats_on_200(monkeypatch):
    def handler(request):
        return httpx.Response(200, json={"system": {"os": "linux"}})
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        cs.httpx, "AsyncClient",
        lambda timeout=None: _RealAsyncClient(transport=transport, timeout=timeout),
    )
    result = await cs.health(8188)
    assert result == {"system": {"os": "linux"}}


async def test_health_returns_none_on_connection_error(monkeypatch):
    def handler(request):
        raise httpx.ConnectError("refused", request=request)
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        cs.httpx, "AsyncClient",
        lambda timeout=None: _RealAsyncClient(transport=transport, timeout=timeout),
    )
    result = await cs.health(8188)
    assert result is None


async def test_health_returns_none_on_non_200(monkeypatch):
    def handler(request):
        return httpx.Response(500)
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        cs.httpx, "AsyncClient",
        lambda timeout=None: _RealAsyncClient(transport=transport, timeout=timeout),
    )
    result = await cs.health(8188)
    assert result is None


# --------------------------------------------------------------------------- #
# Small helper: an async-callable stand-in with a fixed return value.
# --------------------------------------------------------------------------- #


class AsyncStub:
    def __init__(self, return_value=None):
        self._return_value = return_value

    async def __call__(self, *args, **kwargs):
        return self._return_value


# --------------------------------------------------------------------------- #
# Regressions from the 2026-07-05 multi-agent review
# --------------------------------------------------------------------------- #


def test_unbalanced_quote_in_comfy_args_does_not_brick_reads(scratch_comfy_env):
    """Hand-edited COMFY_ARGS with an unbalanced quote must degrade, not raise —
    a raw ValueError here 500'd every route that touches read_flags()."""
    scratch_comfy_env.write_text('COMFY_PORT=8188\nCOMFY_ARGS=--fast don\'t\n')
    flags = cs.read_flags()          # must not raise
    assert flags["values"]["fast_mode"] is True
    result = cs.write_flags({"disable_mmap": True})   # must not raise either
    assert result["values"]["disable_mmap"] is True


def test_explicit_cors_origin_is_preserved_not_widened(scratch_comfy_env):
    """A hand-tightened '--enable-cors-header <origin>' must survive saves —
    silently rewriting it as the bare wildcard flag widens CORS."""
    scratch_comfy_env.write_text(
        'COMFY_PORT=8188\nCOMFY_ARGS="--enable-cors-header https://trusted.example --fast"\n')
    cs.write_flags({"disable_mmap": True})
    args_line = next(ln for ln in scratch_comfy_env.read_text().splitlines()
                     if ln.startswith("COMFY_ARGS"))
    assert "https://trusted.example" in args_line
    assert args_line.count("--enable-cors-header") == 1
    # and it stays stable across a second save
    cs.write_flags({"disable_mmap": False})
    args_line2 = next(ln for ln in scratch_comfy_env.read_text().splitlines()
                      if ln.startswith("COMFY_ARGS"))
    assert "https://trusted.example" in args_line2
    assert args_line2.count("--enable-cors-header") == 1


def test_nan_and_infinity_rejected_for_float_flags(scratch_comfy_env):
    for bad in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(cs.FlagValidationError):
            cs.write_flags({"reserve_vram": bad})


def test_unmanaged_lines_preserved_byte_for_byte(scratch_comfy_env):
    """Hand-edited lines must be re-emitted verbatim — re-encoding through
    _quote mutated escapes on every save and could turn a single-quoted
    literal into a bash-expandable double-quoted one."""
    hand = "MYVAR='literal $(not expanded) \"quoted\"'"
    scratch_comfy_env.write_text(f"COMFY_PORT=8188\nCOMFY_ARGS=\n{hand}\n")
    cs.write_flags({"disable_mmap": True})
    assert hand in scratch_comfy_env.read_text().splitlines()
    cs.write_flags({"disable_mmap": False})
    assert hand in scratch_comfy_env.read_text().splitlines()


def test_quote_unquote_round_trip_is_stable(scratch_comfy_env):
    """Two consecutive saves must produce identical file content (no escape
    growth), even with an exotic leftover token containing a quote."""
    scratch_comfy_env.write_text('COMFY_PORT=8188\nCOMFY_ARGS="--fast --my-flag can\'"\'"\'t"\n')
    cs.write_flags({})
    first = scratch_comfy_env.read_text()
    cs.write_flags({})
    assert scratch_comfy_env.read_text() == first


def test_regex_flags_reject_a_trailing_newline(tmp_path, monkeypatch):
    """`^…$` also matches before a trailing newline, so "11.5.1\\n" validated
    and was written straight into the unit's environment. `\\A…\\Z` does not."""
    import re

    from app import comfy_service as cs
    spec = next(s for s in cs.FLAG_SCHEMA if s["key"] == "gfx_override")
    assert re.match(spec["pattern"], "11.5.1")
    assert not re.match(spec["pattern"], "11.5.1\n")
    assert not re.match(spec["pattern"], "11.5.1\nHSA_FOO=1")
