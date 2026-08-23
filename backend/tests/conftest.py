"""Shared pytest fixtures for the backend test suite."""
from __future__ import annotations

import pytest

from app import config as config_module
from app import openclaw


@pytest.fixture
def monkeypatch_root(tmp_path):
    """A fresh synthetic OpenClaw agents-dir root, restored after the test.

    test_recovery.py's own tests point `openclaw.OPENCLAW_AGENTS_DIR` at the
    Path this fixture yields; matches the ad-hoc root the file's standalone
    runner used to build by hand.
    """
    root = tmp_path / "agents"
    root.mkdir(parents=True)
    orig = openclaw.OPENCLAW_AGENTS_DIR
    yield root
    openclaw.OPENCLAW_AGENTS_DIR = orig


# --------------------------------------------------------------------------- #
# The roster the tests assert against.
#
# These used to be config.DEFAULT_BOTS, which meant every access-control test
# silently depended on the roster the product happens to SHIP — so changing the
# starter bots (as the open-source build did, from a personal seven-bot roster
# to two generic ones) turned 33 unrelated tests red for no behavioural reason.
#
# The suite needs a specific SHAPE, not specific defaults: several safe bots,
# several non-safe ones, mixed-case ids (the session-key lowercasing bug lives
# there), and exactly one bot with reactions enabled. That shape is declared
# here and written into each test's throwaway config.yaml, so the product is
# free to ship whatever starter roster it likes.
# --------------------------------------------------------------------------- #

TEST_BOTS: list[dict] = [
    # `avatar` is set even though the product now ships none: the Safe-Mode
    # gate allow-lists safe bots' avatar FILENAMES, so a roster without them
    # would make that allowlist empty and the gate untestable.
    {"id": "alpha",   "name": "Alpha",   "avatar": "alpha.svg", "emoji": "✨",
     "order": 0, "safe": True},
    {"id": "beta",    "name": "Beta",    "avatar": "beta.svg",  "emoji": "🔷",
     "order": 1, "safe": True},
    # Deliberately capitalised: ids are lowercased on the agent-session path,
    # and more than one bug has hidden in that mismatch.
    {"id": "Atlas", "name": "Atlas", "avatar": "Atlas.svg", "emoji": "🟢",
     "order": 2, "safe": True, "color": "#a01818"},
    # The only bot with reactions on — several tests assert that exclusivity.
    {"id": "main",    "name": "Nova",    "avatar": "nova.png",  "emoji": "⚡",
     "order": 3, "safe": False, "reactions": True},
    {"id": "Scout",    "name": "Scout",    "avatar": "Scout.svg",  "emoji": "🔧",
     "order": 4, "safe": False},
    {"id": "AI_Sage", "name": "Sage", "avatar": "AI_Sage.svg", "emoji": "🧠",
     "order": 5, "safe": False},
    {"id": "AI_Swift", "name": "Swift",  "avatar": "AI_Swift.svg", "emoji": "⚡",
     "order": 6, "safe": False},
]

SAFE_BOT_IDS = {b["id"] for b in TEST_BOTS if b["safe"]}


@pytest.fixture(autouse=True)
def _test_roster():
    """Give every test the roster above instead of the one the product ships.

    Autouse, and on its OWN MonkeyPatch instance, for two reasons:

    * Autouse means no fixture has to remember to ask for it — the roster is a
      property of the suite, not of individual tests.
    * A private MonkeyPatch because at least one test calls
      `monkeypatch.undo()` mid-body to drop its own patch. `undo()` reverts
      *everything* on that instance, so sharing the test's monkeypatch would
      silently restore the shipped roster half way through and fail the second
      half of the test with a confusing error.

    Patches `DEFAULT_BOTS` rather than writing each test's config.yaml by hand:
    every suite points DATA_DIR at a fresh tmp dir, so `load_bots()` MATERIALISES
    config.yaml from the shipped roster on first use — patching the roster is
    therefore the whole job, and each test still gets a real config.yaml it can
    edit through the Bot Manager routes.

    (It used to be load-bearing for a second reason: `load_bots()` merged
    DEFAULT_BOTS into whatever the file said, so a shipped bot a test never
    asked for reappeared in its roster — and, if flagged safe, in every
    Safe-Mode assertion. That merge is gone; the file is now the whole roster
    once it exists. See test_reactions.py's roster section.)
    """
    from app.config import Bot
    mp = pytest.MonkeyPatch()
    mp.setattr(config_module, "DEFAULT_BOTS", [Bot(**b) for b in TEST_BOTS])
    config_module._invalidate_bots_cache()
    yield
    mp.undo()
    config_module._invalidate_bots_cache()

# --------------------------------------------------------------------------- #
# Making the suite fast enough to actually run
#
# Two things dominated the runtime, both measured, neither of them testing
# anything:
#
#   1. Pillow re-rendered the same eleven 640x640 starter reaction cards on
#      EVERY TestClient startup — 395 calls, 3,740 identical PNGs, 271s of a
#      309s run (88%). config derives REACTIONS_BUILTIN_DIR from the current
#      DATA_DIR, every suite monkeypatches DATA_DIR to a fresh tmp_path, and
#      the app's lifespan seeds the pack into it.
#   2. PBKDF2 at its production 200,000 iterations ran 414 times (19.4s, 44%
#      of what remained). That is the KDF doing its job; a test asserting the
#      job is slow is a test asserting nothing.
#
# Both are fixed here rather than in app code, so what the tests OBSERVE is
# unchanged: the same eleven cards exist, the same reactions.yaml is written,
# hashing and verification stay symmetric.
# --------------------------------------------------------------------------- #

@pytest.fixture(scope="session")
def _starter_cards(tmp_path_factory):
    """Render the starter pack ONCE per session and hand back the directory."""
    from app import config as _config
    from app import reactions as _reactions

    cache = tmp_path_factory.mktemp("starter-cards")
    orig = _config.DATA_DIR
    try:
        _config.DATA_DIR = cache
        _reactions.seed_starter_pack()
        built = _config.REACTIONS_BUILTIN_DIR
        return list(built.glob("*.png")) if built.is_dir() else []
    finally:
        _config.DATA_DIR = orig


@pytest.fixture(autouse=True)
def _reuse_starter_cards(_starter_cards, monkeypatch):
    """Drop the pre-rendered cards into place before any TestClient starts.

    seed_starter_pack() skips a card whose file already exists, so it becomes a
    near no-op that still writes reactions.yaml — the state tests assert on.

    copyfile, NOT a hardlink: a test that overwrites a builtin card in place
    would corrupt the shared cache for every test after it.
    """
    import shutil

    from app import config as _config
    from app import reactions as _reactions

    if not _starter_cards:
        yield
        return

    real_seed = _reactions.seed_starter_pack

    def seed_from_cache(*a, **kw):
        try:
            dest = _config.REACTIONS_BUILTIN_DIR
            dest.mkdir(parents=True, exist_ok=True)
            for src in _starter_cards:
                target = dest / src.name
                if not target.exists():
                    shutil.copyfile(src, target)
        except OSError:
            pass          # fall through: the real seeder still runs below
        return real_seed(*a, **kw)

    monkeypatch.setattr(_reactions, "seed_starter_pack", seed_from_cache)
    yield


@pytest.fixture(autouse=True)
def _cheap_kdf(monkeypatch):
    """Drop the PIN KDF to a token cost for tests.

    Both the module constant AND the dataclass field default have to move: the
    default is bound at class-creation time, so patching the constant alone
    changes nothing (measured: zero improvement).

    The SHIPPED default is asserted separately at full strength — see
    test_auth_gate.py::test_shipped_kdf_cost_is_not_weakened.
    """
    from app import auth as _auth

    monkeypatch.setattr(_auth, "PBKDF2_ITERATIONS", 1_000)
    monkeypatch.setattr(_auth.SecurityConfig.__dataclass_fields__["iterations"],
                        "default", 1_000)
    yield

# --------------------------------------------------------------------------- #
# Tests must never touch a real directory.
#
# config.DATA_DIR is XDG-derived (~/.local/share/local-chat), so a test that
# forgets to monkeypatch it writes into THE LIVE INSTALL. That is not
# hypothetical: ten tests in test_avatar_snapshots.py had no fixture, and their
# fixture bytes — a file containing literally b"KEEP-ME" — ended up in the
# family app's data directory. config.AVATAR_DIR is derived from the repo
# checkout instead, so the same tests wrote junk PNGs over the repo's avatars,
# including a 40-byte "main-face.png".
#
# Nothing caught it, because a test writing to the wrong place still PASSES.
# This fixture is the thing that catches it: it runs for every test and refuses
# to let one start with either path pointing somewhere real.
# --------------------------------------------------------------------------- #

@pytest.fixture(autouse=True)
def _no_writes_outside_tmp(tmp_path, monkeypatch):
    import os
    from pathlib import Path

    from app import config as _config

    tmp_roots = (Path("/tmp"), Path(os.environ.get("PYTEST_TMPDIR", "/tmp")),
                 Path(tmp_path).parents[-2] if len(Path(tmp_path).parents) > 1 else Path("/tmp"))

    def _is_temp(p: Path) -> bool:
        try:
            rp = Path(p).resolve()
        except OSError:
            return False
        return any(str(rp).startswith(str(Path(r).resolve())) for r in tmp_roots)

    # Default every test into an isolated directory. A test that wants its own
    # still monkeypatches DATA_DIR itself; this only decides where an
    # UNCONFIGURED test lands, and "somewhere disposable" beats "the family's
    # chat history".
    if not _is_temp(_config.DATA_DIR):
        monkeypatch.setattr(_config, "DATA_DIR", tmp_path / "data")
    if not _is_temp(_config.AVATAR_DIR):
        monkeypatch.setattr(_config, "AVATAR_DIR", tmp_path / "avatars")
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    (tmp_path / "avatars").mkdir(parents=True, exist_ok=True)
    # The media-origins ledger caches in-process and is keyed off DATA_DIR,
    # which this fixture (and most suites) just moved — drop the cache so one
    # test's origins can never answer another test's dedup question.
    from app import main as _main
    _main._media_origins_reset()
    yield
    _main._media_origins_reset()
