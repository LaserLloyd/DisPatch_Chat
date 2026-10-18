"""Find servers (N-8): where it looks is a setting, and the Docker host only inside a container.

The HTTP layer is a ``httpx.MockTransport`` that records every host and port a probe reaches, so the
tests assert on what would have been contacted. Nothing here opens a socket, and 1234/8080 (the owner's
live gateway and panel) are only ever names in a recorded list.
"""
from __future__ import annotations

import httpx
import pytest

from app import config, connections, llm_api, settings_schema, settings_store

DOCKER = "host.docker.internal"


@pytest.fixture
def probes(tmp_path, monkeypatch):
    """A data directory, a recording fake network (nothing answers), no container, no name resolves."""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "config.yaml")
    for name in ("DISPATCH_IN_CONTAINER", "DISPATCH_IN_DOCKER", "DISPATCH_DISCOVERY_TARGETS",
                 "DISPATCH_DISCOVERY_PROBE_DOCKER_HOST"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(connections, "_DOCKERENV", tmp_path / "no-dockerenv")
    monkeypatch.setattr(connections, "_resolves", lambda host: False)
    seen: list[tuple[str, int]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append((req.url.host, req.url.port))
        raise httpx.ConnectError("no route")

    def factory(read_timeout: float = llm_api.READ_TIMEOUT_S) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=read_timeout,
                                 follow_redirects=False)
    monkeypatch.setattr(llm_api, "http_client", factory)
    return seen


def hosts(seen) -> set[str]:
    return {h for h, _ in seen}


def test_the_setting_is_in_the_schema_with_the_documented_defaults():
    t = settings_schema.get_def("connections.discovery_targets")
    d = settings_schema.get_def("connections.discovery_probe_docker_host")
    assert t is not None and t.type == "list" and t.group == "connections"
    assert t.default == ["127.0.0.1", "studioforge", "lmstudio", "ollama"]
    assert DOCKER not in t.default
    assert d is not None and d.type == "boolean" and d.default is True
    assert d.env == "DISPATCH_DISCOVERY_PROBE_DOCKER_HOST" and t.env == "DISPATCH_DISCOVERY_TARGETS"


async def test_a_run_outside_a_container_never_probes_the_docker_host(probes):
    await connections.discover()
    assert DOCKER not in hosts(probes)
    assert hosts(probes) == {"127.0.0.1"}                       # names that do not resolve are skipped
    assert sorted(p for _, p in probes if _ == "127.0.0.1") and {p for _, p in probes} == {1234, 8080, 11434}


async def test_outside_a_container_the_setting_and_a_listed_name_change_nothing(probes):
    settings_store.set_many({"connections.discovery_probe_docker_host": True,
                             "connections.discovery_targets": ["127.0.0.1", DOCKER.upper()]})
    await connections.discover()
    assert DOCKER not in {h.lower() for h in hosts(probes)}


@pytest.mark.parametrize("how", ["env", "dockerenv", "legacy_env"])
async def test_inside_a_container_the_docker_host_is_probed_by_default(probes, monkeypatch, tmp_path, how):
    if how == "env":
        monkeypatch.setenv("DISPATCH_IN_CONTAINER", "1")
    elif how == "legacy_env":
        monkeypatch.setenv("DISPATCH_IN_DOCKER", "1")
    else:
        (tmp_path / "dockerenv").write_text("", encoding="utf-8")
        monkeypatch.setattr(connections, "_DOCKERENV", tmp_path / "dockerenv")
    await connections.discover()
    assert {(DOCKER, p) for p in (1234, 8080, 11434)} <= set(probes)
    assert "127.0.0.1" in hosts(probes)


async def test_the_opt_out_stops_the_docker_host_probe_in_a_container(probes, monkeypatch):
    monkeypatch.setenv("DISPATCH_IN_CONTAINER", "1")
    settings_store.set_many({"connections.discovery_probe_docker_host": False})
    await connections.discover()
    assert DOCKER not in hosts(probes) and hosts(probes) == {"127.0.0.1"}
    # ... and the opt-out also beats a hand-written entry in the list
    settings_store.set_many({"connections.discovery_targets": ["127.0.0.1", DOCKER]})
    probes.clear()
    await connections.discover()
    assert DOCKER not in hosts(probes)


async def test_the_environment_variable_is_an_opt_out_too(probes, monkeypatch):
    monkeypatch.setenv("DISPATCH_IN_CONTAINER", "1")
    monkeypatch.setenv("DISPATCH_DISCOVERY_PROBE_DOCKER_HOST", "0")
    await connections.discover()
    assert DOCKER not in hosts(probes)


async def test_a_person_can_still_name_the_docker_host_in_the_dialog(probes):
    await connections.discover(hosts=[DOCKER])
    assert {(DOCKER, p) for p in (1234, 8080, 11434)} <= set(probes)


async def test_compose_names_are_probed_only_when_they_resolve(probes, monkeypatch):
    monkeypatch.setattr(connections, "_resolves", lambda host: host == "studioforge")
    await connections.discover()
    assert hosts(probes) == {"127.0.0.1", "studioforge"}


async def test_listed_targets_replace_the_defaults_and_accept_host_port(probes):
    settings_store.set_many({"connections.discovery_targets": ["192.0.2.10", "192.0.2.11:9000", "[::1]:1234"]})
    await connections.discover()
    assert {h for h, _ in probes} == {"192.0.2.10", "192.0.2.11", "::1"}
    assert {p for h, p in probes if h == "192.0.2.11"} == {9000}
    assert {p for h, p in probes if h == "192.0.2.10"} == {1234, 8080, 11434}
    assert "127.0.0.1" not in hosts(probes)
    settings_store.set_many({"connections.discovery_targets": []})       # an empty list: look nowhere
    probes.clear()
    assert await connections.discover() == [] and probes == []


async def test_metadata_and_link_local_are_never_probed_even_when_allowed_or_listed(probes):
    bad = ["169.254.169.254", "169.254.170.2", "fe80::1", "metadata.google.internal", "0.0.0.0", "[fe80::2]:1234"]
    settings_store.set_many({"connections.allow_private_targets": True,
                             "connections.discovery_targets": ["127.0.0.1", *bad]})
    await connections.discover(hosts=bad)
    assert hosts(probes) == {"127.0.0.1"}


def test_targets_are_parsed_strictly():
    parse = connections.parse_discovery_target
    assert parse("studioforge") == ("studioforge", None)
    assert parse("http://192.0.2.5:8080/") == ("192.0.2.5", 8080)
    assert parse("[::1]:1234") == ("::1", 1234)
    assert parse("::1") == ("::1", None)
    for junk in ("", "a b", "host:99999", "host:0", "x/../y", "user@host", "169.254.1.1", "169.254.169.254"):
        assert parse(junk) is None, junk


def test_the_container_check_reads_the_environment(monkeypatch, tmp_path):
    monkeypatch.setattr(connections, "_DOCKERENV", tmp_path / "none")
    for name in ("DISPATCH_IN_CONTAINER", "DISPATCH_IN_DOCKER"):
        monkeypatch.delenv(name, raising=False)
    assert connections.in_container() is False
    for value, expect in (("1", True), ("true", True), ("0", False), ("", False)):
        monkeypatch.setenv("DISPATCH_IN_CONTAINER", value)
        assert connections.in_container() is expect, value
