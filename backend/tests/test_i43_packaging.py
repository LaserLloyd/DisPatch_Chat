"""Container packaging (4.3 WP-A9: issues #12, #15, #16, #18, #28, #38).

These tests parse the shipped Dockerfile, compose files and entrypoint; they build
nothing. The image itself is checked by hand with podman (see the WP report).

- #12  the Claude Agent backend can be baked in: `ARG AGENTS_CLAUDE` adds the
       `agents-claude` extra to EVERY `uv sync` (the second sync is exact and would
       remove it again otherwise).
- #16  the app runs on a read-only root with a small /tmp tmpfs; TMPDIR stays on /data.
- #38  HOME is /data/home (the voice add-on's rig token, the Claude SDK's ~/.claude),
       created by the entrypoint only when missing, never by a recursive chown.
- #15  the stack's network has no fixed `name:`.
- #18  compose tells the app which host port it is published on (the setup banner).
- #5   with the Caddy service in the file, the app trusts forwarded headers from
       Caddy's fixed address only (not from the whole subnet: the bridge gateway, which
       is where rootless podman's port forwarder connects from, is in that subnet).
- #28  docs/deploy/stack.md says the slim -cuda image fetches its engine itself.
Run: cd backend && uv run pytest tests/test_i43_packaging.py
"""
from __future__ import annotations

import ipaddress
import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = ROOT / "Dockerfile"
COMPOSE = ROOT / "docker-compose.yml"
STACK = ROOT / "deploy" / "compose" / "stack.yml"
ENTRYPOINT = ROOT / "docker-entrypoint.sh"


def _yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _env(service: dict) -> dict:
    env = service.get("environment") or {}
    if isinstance(env, list):
        env = dict(item.split("=", 1) for item in env)
    return {str(k): str(v) for k, v in env.items()}


def _defaults(value: str) -> str:
    """Resolve compose's `${VAR:-default}` to the default (what an empty .env gives)."""
    return re.sub(r"\$\{[A-Z_]+:-([^}]*)\}", r"\1", str(value))


def _stages(text: str) -> dict[str, str]:
    """Dockerfile text per `FROM ... AS <name>` stage."""
    parts = re.split(r"(?m)^FROM\s+\S+\s+AS\s+(\S+)\s*$", text)
    return {parts[i]: parts[i + 1] for i in range(1, len(parts) - 1, 2)}


def _runs(stage_text: str) -> list[str]:
    """Every RUN instruction of a stage, continuation lines joined, comments dropped."""
    lines = [ln for ln in stage_text.splitlines() if not ln.lstrip().startswith("#")]
    joined = re.sub(r"\\\n", " ", "\n".join(lines))
    return [ln for ln in joined.splitlines() if ln.lstrip().startswith("RUN ")]


# --- Dockerfile ----------------------------------------------------------------

def test_dockerfile_bakes_the_claude_extra_into_every_uv_sync():
    builder = _stages(DOCKERFILE.read_text(encoding="utf-8"))["builder"]
    assert re.search(r"(?m)^ARG AGENTS_CLAUDE=0\s*$", builder), "ARG AGENTS_CLAUDE=0 in the builder stage"
    syncs = [r for r in _runs(builder) if "uv sync" in r]
    assert len(syncs) >= 2
    for run in syncs:
        assert "AGENTS_CLAUDE" in run and "agents-claude" in run, run


def test_dockerfile_rejects_a_bad_agents_claude_value():
    builder = _stages(DOCKERFILE.read_text(encoding="utf-8"))["builder"]
    run = next(r for r in _runs(builder) if "uv sync" in r)
    assert "AGENTS_CLAUDE must be 0 or 1" in run


def test_dockerfile_home_is_on_the_data_volume():
    runtime = _stages(DOCKERFILE.read_text(encoding="utf-8"))["runtime"]
    assert re.search(r"(?m)^\s*HOME=/data/home\b", runtime) or re.search(r"(?m)^ENV HOME=/data/home\b", runtime)
    useradd = next(r for r in _runs(runtime) if "useradd" in r)
    assert "--home-dir /data/home" in useradd and "--no-create-home" in useradd


def test_entrypoint_has_lf_endings_and_gitattributes_pins_them():
    raw = ENTRYPOINT.read_bytes()
    assert b"\r" not in raw
    assert raw.startswith(b"#!/bin/sh\n")
    attrs = (ROOT / ".gitattributes").read_text(encoding="utf-8")
    assert re.search(r"(?m)^\*\.sh\s+text\s+eol=lf\s*$", attrs)


def test_entrypoint_creates_home_only_if_missing_and_never_chowns_it_recursively():
    text = ENTRYPOINT.read_text(encoding="utf-8")
    assert "export HOME=/home/app" not in text
    assert re.search(r'if \[ ! -d "\$HOME" \]', text), "HOME is created only when missing"
    assert "umask 077" in text
    for line in text.splitlines():
        if "chown" in line and "HOME" in line:
            assert "-R" not in line, line
    # The only recursive chown stays the pre-existing, consented, mismatch-gated one.
    recursive = [ln.strip() for ln in text.splitlines()
                 if re.search(r"\bchown\s+-R\b", ln) and not ln.lstrip().startswith(("#", "log"))]
    assert recursive == ['chown -R "$PUID:$PGID" "$DATA_DIR"']


# --- docker-compose.yml ----------------------------------------------------------

def test_compose_app_runs_read_only_with_a_small_tmp_and_home_on_data():
    app = _yaml(COMPOSE)["services"]["app"]
    assert app.get("read_only") is True
    assert app.get("tmpfs") == ["/tmp:size=64m"]
    env = _env(app)
    assert env.get("HOME") == "/data/home"
    assert "TMPDIR" not in env, "TMPDIR stays the image's /data/tmp, never the tmpfs"


def test_compose_passes_build_args_and_the_published_port():
    app = _yaml(COMPOSE)["services"]["app"]
    assert app["build"]["args"].get("AGENTS_CLAUDE") == "${AGENTS_CLAUDE:-0}"
    assert _env(app).get("DISPATCH_PUBLIC_PORT") == "${HOST_PORT:-8765}"


def test_compose_trusts_forwarded_headers_from_caddy_only():
    doc = _yaml(COMPOSE)
    services = doc["services"]
    assert "caddy" in services
    proxies = _defaults(_env(services["app"]).get("DISPATCH_TRUSTED_PROXIES", ""))
    assert proxies, "the app must trust the Caddy service's address (issue #5)"

    ipam = doc["networks"]["default"]["ipam"]["config"][0]
    subnet = ipaddress.ip_network(_defaults(ipam["subnet"]))
    dynamic = ipaddress.ip_network(_defaults(ipam["ip_range"]))
    gateway = ipaddress.ip_address(_defaults(ipam["gateway"]))
    caddy_ip = ipaddress.ip_address(_defaults(services["caddy"]["networks"]["default"]["ipv4_address"]))

    nets = [ipaddress.ip_network(p.strip()) for p in proxies.split(",") if p.strip()]
    assert nets == [ipaddress.ip_network(f"{caddy_ip}/32")]
    assert caddy_ip in subnet and dynamic.subnet_of(subnet)
    # Nothing else can be handed Caddy's address, and the gateway (where a port
    # forwarder's connections come from) is never trusted.
    assert caddy_ip not in dynamic
    assert gateway in subnet and gateway not in nets[0] and gateway not in dynamic
    # The app itself joins the same network (implicitly, as `default`).
    assert services["app"].get("networks", ["default"]) in (["default"], {"default": None})


# --- deploy/compose/stack.yml ----------------------------------------------------

def test_stack_network_has_no_fixed_name():
    doc = _yaml(STACK)
    assert "name" not in (doc["networks"]["stack"] or {})
    assert "dispatch-stack" not in yaml.safe_dump(doc["networks"])


def test_stack_dispatch_matches_the_single_file_posture():
    services = _yaml(STACK)["services"]
    app = services["dispatch"]
    assert app.get("read_only") is True
    assert app.get("tmpfs") == ["/tmp:size=64m"]
    env = _env(app)
    assert env.get("HOME") == "/data/home"
    assert env.get("DISPATCH_PUBLIC_PORT") == "${DISPATCH_PORT:-8765}"
    # No proxy in this file, so nothing is trusted to forward client addresses.
    assert "caddy" not in services
    assert "DISPATCH_TRUSTED_PROXIES" not in env
    assert services["dispatch-seed"]["build"]["args"].get("AGENTS_CLAUDE") == "${AGENTS_CLAUDE:-0}"


# --- docs / examples -------------------------------------------------------------

def test_stack_doc_says_the_slim_cuda_image_fetches_its_engine():
    doc = (ROOT / "docs" / "deploy" / "stack.md").read_text(encoding="utf-8")
    assert "SF_ENGINE_BOOTSTRAP=0" in doc
    assert "-cuda-devel" in doc
    assert "network (`dispatch-stack`)" not in doc


@pytest.mark.parametrize("name", ["AGENTS_CLAUDE", "DISPATCH_TRUSTED_PROXIES"])
def test_env_example_documents(name):
    assert name in (ROOT / ".env.example").read_text(encoding="utf-8")
