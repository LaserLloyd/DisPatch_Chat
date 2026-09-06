"""Prompts must never be able to become command-line OPTIONS.

The image CLI is invoked with a fixed argv (never a shell), but the prompt
used to sit at ``argv[1]`` as a bare positional — so a prompt of
``--list-workflows`` (or anything else the CLI accepts) was interpreted as a
flag rather than as text. Prompts arrive from ``POST /api/reactions/generate``
and from the prompt banks (``PUT /api/reactions/prompts``,
``PUT /api/avatar-pool/<bot>/prompts``), i.e. from anything holding the API
key or talking to the loopback agent surface.

Two locks, both asserted here:

* the argv puts ``--`` before every positional, so option parsing has already
  ended by the time the prompt is read;
* a value whose stripped form starts with ``-`` is refused at the write /
  request boundary, so it never reaches the process table at all.
"""
from __future__ import annotations

import json
import pathlib

import pytest
from test_reactions import rx_env

from app import avatar_pool, config, pool_guard, reactions


class _FakeProc:
    returncode = 0
    stderr = ""

    def __init__(self, stdout: str) -> None:
        self.stdout = stdout


@pytest.fixture
def rig(monkeypatch, tmp_path):
    """A fake image CLI that records argv and hands back one real PNG."""
    calls: list[list[str]] = []
    cli = tmp_path / "imagecli"
    cli.write_text("#!/bin/sh\nexit 0\n")
    cli.chmod(0o755)
    monkeypatch.setattr(reactions, "_IMAGE_CLI", cli)
    monkeypatch.setattr(avatar_pool, "_IMAGE_CLI", cli)

    def fake_run(argv, **kw):
        calls.append(list(argv))
        # Write where the caller asked — the app only accepts files that land
        # inside the --output directory it named.
        out = pathlib.Path(argv[argv.index("--output") + 1])
        out.mkdir(parents=True, exist_ok=True)
        src = next(config.REACTIONS_BUILTIN_DIR.glob("*.png"))
        made = out / f"made{len(calls)}.png"
        made.write_bytes(src.read_bytes())
        return _FakeProc(json.dumps({"status": "ok", "files": [str(made)]}))

    monkeypatch.setattr(reactions.subprocess, "run", fake_run)
    monkeypatch.setattr(avatar_pool.subprocess, "run", fake_run)
    return calls


def _save_v5_bank(monkeypatch):
    """A minimal valid v5 avatar bank, with the bits-prompt helper stubbed —
    compose_prompt shells out to it for the tier strings, and under the rig
    fixtures every subprocess.run is the fake image CLI. The v1
    base+variations shape is refused by bank_save since v5."""
    monkeypatch.setattr(
        avatar_pool, "_bits_prompt",
        lambda *flags: "tier1 identity" if "--tier1" in flags else "tier2 detail")
    avatar_pool.bank_save(
        {"categories": {"calm": {"label": "Calm", "prompts": ["a portrait"]}},
         "ratio": "1:1"}, "main")


# --------------------------------------------------------------------------- #
# argv shape
# --------------------------------------------------------------------------- #


def test_generate_puts_the_prompt_after_a_double_dash(rx_env, rig):
    reactions.generate("a cheerful robot", style="ink", workflow="krea2")
    argv = rig[0]
    assert argv[-2:] == ["--", "a cheerful robot"]
    # Everything before the separator is an option or an option value.
    assert "--" not in argv[1:-2]


def test_pool_generate_puts_the_prompt_after_a_double_dash(rx_env, rig):
    cfg = reactions.pool_load().config
    cat = next(iter(reactions.bank_load()["categories"]))
    assert reactions._pool_generate_one(cfg, cat) is not None
    argv = rig[0]
    assert argv[-2] == "--"
    assert not argv[-1].startswith("-")


def test_avatar_pair_puts_the_prompt_after_a_double_dash(rx_env, rig, monkeypatch):
    _save_v5_bank(monkeypatch)
    st = avatar_pool.load_state("main")
    st.config.enabled = True
    avatar_pool.save_state(st, "main")
    assert avatar_pool.generate_pair("main") is not None
    argv = rig[0]
    assert argv[-2] == "--"
    assert not argv[-1].startswith("-")
    assert "a portrait" in argv[-1]


def _enable_pool() -> None:
    st = avatar_pool.load_state("main")
    st.config.enabled = True
    avatar_pool.save_state(st, "main")


def test_a_banks_own_negative_replaces_the_helpers(rx_env, rig, monkeypatch):
    """The negative follows the identity. The helper's names specific hair,
    eyes and species, so sending it with somebody else's `base` would fight
    that character on every render."""
    monkeypatch.setattr(
        avatar_pool, "_bits_prompt",
        lambda *f: (_ for _ in ()).throw(AssertionError("helper called")))
    avatar_pool.bank_save({"base": "a photoreal instructor", "negative": "blurry, extra limbs",
                           "categories": {"calm": {"label": "Calm",
                                                   "prompts": ["a portrait"]}}}, "main")
    _enable_pool()
    assert avatar_pool.generate_pair("main") is not None
    argv = rig[0]
    assert argv[argv.index("--negative") + 1] == "blurry, extra limbs"


def test_a_bank_that_owns_its_identity_sends_no_borrowed_negative(rx_env, rig, monkeypatch):
    monkeypatch.setattr(
        avatar_pool, "_bits_prompt",
        lambda *f: (_ for _ in ()).throw(AssertionError("helper called")))
    avatar_pool.bank_save({"base": "a photoreal instructor",
                           "categories": {"calm": {"label": "Calm",
                                                   "prompts": ["a portrait"]}}}, "main")
    _enable_pool()
    assert avatar_pool.generate_pair("main") is not None
    assert "--negative" not in rig[0]


def test_the_helpers_negative_still_rides_with_the_helpers_identity(rx_env, rig, monkeypatch):
    _save_v5_bank(monkeypatch)          # no `base` — the helper owns the face
    monkeypatch.setattr(                # …re-stubbed: _save_v5_bank sets its own
        avatar_pool, "_bits_prompt",
        lambda *f: "helper negative" if "--negative" in f else "helper identity")
    _enable_pool()
    assert avatar_pool.generate_pair("main") is not None
    argv = rig[0]
    assert argv[argv.index("--negative") + 1] == "helper negative"


# --------------------------------------------------------------------------- #
# queue band
#
# A shelf refill has nobody waiting on it, and the rig runs one job at a time
# per GPU — so a refill that submits in the same band as a chat request puts a
# family member behind a picture that will not be looked at for hours.
# --------------------------------------------------------------------------- #


def test_the_reaction_pool_refills_in_the_background_band(rx_env, rig):
    cfg = reactions.pool_load().config
    cat = next(iter(reactions.bank_load()["categories"]))
    assert reactions._pool_generate_one(cfg, cat) is not None
    argv = rig[0]
    assert argv[argv.index("--priority") + 1] == "3"


def test_the_avatar_pool_refills_in_the_background_band(rx_env, rig, monkeypatch):
    _save_v5_bank(monkeypatch)
    st = avatar_pool.load_state("main")
    st.config.enabled = True
    avatar_pool.save_state(st, "main")
    assert avatar_pool.generate_pair("main") is not None
    argv = rig[0]
    assert argv[argv.index("--priority") + 1] == "3"
    # The band is an option like any other: it stays in front of the `--`, so
    # a prompt can never be read as its value.
    assert argv.index("--priority") < argv.index("--")


# --------------------------------------------------------------------------- #
# validation boundary
# --------------------------------------------------------------------------- #


def test_generate_refuses_a_prompt_that_looks_like_a_flag(rx_env, rig):
    with pytest.raises(reactions.ReactionError) as e:
        reactions.generate("--list-workflows")
    assert e.value.status == 400
    assert not rig, "the CLI must not run at all"


def test_generate_route_400s_on_a_flag_prompt(rx_env, rig):
    client = rx_env()
    r = client.post("/api/reactions/generate", json={"prompt": "  --status"})
    assert r.status_code == 400
    assert not rig


def test_reaction_bank_refuses_flag_shaped_entries(rx_env):
    bank = reactions.bank_load()
    bad = json.loads(json.dumps(bank))
    cat = next(iter(bad["categories"]))
    bad["categories"][cat]["prompts"] = ["--status"]
    with pytest.raises(reactions.ReactionError) as e:
        reactions.bank_save(bad)
    assert e.value.status == 400

    bad2 = json.loads(json.dumps(bank))
    bad2["base"] = "-o /etc"
    with pytest.raises(reactions.ReactionError):
        reactions.bank_save(bad2)


def test_reaction_prompts_route_400s_on_a_flag_shaped_entry(rx_env):
    client = rx_env()
    bank = json.loads(json.dumps(reactions.bank_load()))
    cat = next(iter(bank["categories"]))
    bank["categories"][cat]["prompts"] = ["--list-styles"]
    r = client.put("/api/reactions/prompts", json={"prompts": bank})
    assert r.status_code == 400


def test_avatar_bank_refuses_flag_shaped_entries(rx_env):
    with pytest.raises(avatar_pool.PoolError) as e:
        avatar_pool.bank_save({"base": "--status"}, "main")
    assert e.value.status == 400
    with pytest.raises(avatar_pool.PoolError):
        avatar_pool.bank_save({"base": "ok", "variations": ["--output=/tmp"]}, "main")


# --------------------------------------------------------------------------- #
# companion CLI (VRAM guard)
# --------------------------------------------------------------------------- #


def test_pool_guard_never_passes_a_dash_leading_model_id(monkeypatch):
    seen: list[list[str]] = []
    monkeypatch.setattr(pool_guard, "gpu_cli_bin", lambda: "/bin/true")
    monkeypatch.setattr(pool_guard, "_run_json",
                        lambda argv, **kw: seen.append(argv) or {"pinned": False})
    assert pool_guard._model_pinned("--help") is False
    assert not seen, "a flag-shaped model id must not reach the CLI"
    pool_guard._model_pinned("vendor/model")
    assert seen[0][-2:] == ["--", "vendor/model"]


# --------------------------------------------------------------------------- #
# The rig's answer may only name files inside the directory we asked for
# --------------------------------------------------------------------------- #


@pytest.fixture
def stray_rig(monkeypatch, tmp_path):
    """A rig that names a file OUTSIDE the --output dir it was given."""
    stray = tmp_path / "not-ours.png"
    cli = tmp_path / "imagecli"
    cli.write_text("#!/bin/sh\nexit 0\n")
    cli.chmod(0o755)
    monkeypatch.setattr(reactions, "_IMAGE_CLI", cli)
    monkeypatch.setattr(avatar_pool, "_IMAGE_CLI", cli)

    def fake_run(argv, **kw):
        src = next(config.REACTIONS_BUILTIN_DIR.glob("*.png"))
        stray.write_bytes(src.read_bytes())
        return _FakeProc(json.dumps({"status": "ok", "files": [str(stray)]}))

    monkeypatch.setattr(reactions.subprocess, "run", fake_run)
    monkeypatch.setattr(avatar_pool.subprocess, "run", fake_run)
    return stray


def test_generate_ignores_a_file_outside_the_output_dir(rx_env, stray_rig):
    with pytest.raises(reactions.ReactionError) as e:
        reactions.generate("a cheerful robot")
    assert e.value.status == 502
    assert stray_rig.is_file(), "a file we never asked for was DELETED"


def test_pool_generate_ignores_a_file_outside_the_output_dir(rx_env, stray_rig):
    cfg = reactions.pool_load().config
    cat = next(iter(reactions.bank_load()["categories"]))
    assert reactions._pool_generate_one(cfg, cat) is None
    assert stray_rig.is_file()


def test_avatar_pair_ignores_a_file_outside_the_output_dir(rx_env, stray_rig, monkeypatch):
    _save_v5_bank(monkeypatch)
    assert avatar_pool.generate_pair("main") is None
    assert stray_rig.is_file()
