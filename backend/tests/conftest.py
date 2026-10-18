"""Shared pytest fixtures for the backend test suite."""
from __future__ import annotations

import os

import pytest

# Drive mode warms its speech models in a background thread at app start-up.
# Never in the suite: every TestClient start would load ~1 GB of models when
# DISPATCH_SPEECH_MODELS happens to be set. Tests that want it call
# app.voice.routes.prewarm() themselves.
os.environ.setdefault("DISPATCH_VOICE_PREWARM", "0")

# Importing app.main mounts the installed extensions at IMPORT time, before any
# fixture can redirect config.DATA_DIR: the extension host's crash-loop marker
# (<data>/ext/.booting) must never be written by a test run — with the voice
# add-on installed it would land in the real data directory and count a "failed
# start" there. Tests that exercise the marker turn it on themselves.
os.environ.setdefault("DISPATCH_EXT_BOOT_MARKER", "0")

# Connection keys and secret settings go to a 0600 file under the (per-test) data dir, never to a
# developer's real OS keyring (app/secret_store.py).
os.environ.setdefault("DISPATCH_SECRET_STORE", "file")

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
def _w5_stores_start_empty(monkeypatch):
    """Per-test isolation for the 3.0 settings / connections / secret / plugin-approval stores:
    their schema-ready sets and caches are keyed by data directory, and the plugin settings
    registry is process-wide. Backups use a cheap scrypt so a round trip takes milliseconds."""
    from app import backups, connections, secret_store, settings_store, tool_manifests

    def clear():
        settings_store.reset_for_tests()
        connections.reset_for_tests()
        secret_store.reset_for_tests()
        tool_manifests.reset_for_tests()
    clear()
    monkeypatch.setattr(backups, "SCRYPT_N", 2 ** 10)
    yield
    clear()


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

@pytest.fixture(autouse=True)
def _accounts_and_auth_state(tmp_path, tmp_path_factory, monkeypatch):
    """Per-test isolation for the 3.0 account layer.

    * argon2id at a token cost (the shipped cost is asserted in test_accounts.py);
    * security.yaml / RECOVERY-CODE.txt / trusted-devices.yaml follow the test's tmp
      dir unless the test chose its own - they are bound at import time, so without
      this a test that set a PIN wrote the REAL install's security file;
    * the profile cache, the throttle buckets, the setup token and the per-profile
      time counters start empty;
    * ``DISPATCH_ALLOW_OPEN_INSTALL=1``: most of the suite exercises the product with
      no owner created, from a TestClient whose peer is not loopback. That is the 2.x
      "open until a PIN is set" behaviour, which 3.0 keeps only behind this switch
      (test_open_install.py turns it off to test the secure default).
    """
    from _tmpguard import is_disposable

    from app import accounts, auth, limits

    monkeypatch.setenv("DISPATCH_ARGON2_MEMORY_KIB", "8")
    monkeypatch.setenv("DISPATCH_ARGON2_PASSES", "1")
    monkeypatch.setenv("DISPATCH_ALLOW_OPEN_INSTALL", "1")
    base = tmp_path / "data"
    base.mkdir(parents=True, exist_ok=True)
    for name, fname in (("SECURITY_PATH", "security.yaml"), ("RECOVERY_PATH", "RECOVERY-CODE.txt"),
                        ("TRUSTED_PATH", "trusted-devices.yaml")):
        if not is_disposable(getattr(auth, name), tmp_path_factory.getbasetemp()):
            monkeypatch.setattr(auth, name, base / fname)
    accounts.reset_for_tests()
    limits.reset()
    auth._cache = None
    auth._trusted = None
    auth._sessions.clear()
    auth._buckets.clear()
    auth._fail_count = 0
    auth._fail_until = 0.0
    auth._setup_token = None
    yield
    accounts.reset_for_tests()


# --------------------------------------------------------------------------- #
# Tests must never touch a real directory.
#
# config.DATA_DIR is XDG-derived (~/.local/share/local-chat), so a test that
# forgets to monkeypatch it writes into THE LIVE INSTALL. That is not
# hypothetical: ten tests in test_avatar_snapshots.py had no fixture, and their
# fixture bytes -- a file containing literally b"KEEP-ME" -- ended up in the
# family app's data directory, while _write_avatar dropped a 40-byte
# "main-face.png" over the bot's real face.
#
# Nothing caught it, because a test writing to the wrong place still PASSES.
# Worse, test_prune_keeps_referenced_and_drops_orphans calls prune() with a
# keep-set of ONE, so pointed at the live store it deletes every snapshot the
# family's 163 threads reference -- and still reports green.
#
# This fixture is the thing that catches it. The rule it applies lives in
# tests/_tmpguard.py, tested directly by tests/test_tmp_guard.py, because the
# rule itself is what failed the first time: it inferred a "temp root" from the
# basetemp string and mistook all of /var -- and therefore /var/home/<user> --
# for scratch space. See that module for the full account.
# --------------------------------------------------------------------------- #

@pytest.fixture(autouse=True)
def _no_writes_outside_tmp(tmp_path, tmp_path_factory, monkeypatch):
    from pathlib import Path

    from _tmpguard import is_disposable

    from app import config as _config

    # Ask pytest where its scratch space actually is rather than deriving it
    # from a path: an inferred root is what broke last time.
    basetemp = tmp_path_factory.getbasetemp()

    # Default every test into an isolated directory. A test that wants its own
    # still monkeypatches DATA_DIR itself; this only decides where an
    # UNCONFIGURED test lands, and "somewhere disposable" beats "the family's
    # chat history".
    if not is_disposable(_config.DATA_DIR, basetemp):
        monkeypatch.setattr(_config, "DATA_DIR", tmp_path / "data")
    if not is_disposable(_config.AVATAR_DIR, basetemp):
        monkeypatch.setattr(_config, "AVATAR_DIR", tmp_path / "avatars")
    # CONFIG_PATH is computed from DATA_DIR at IMPORT time, so moving DATA_DIR
    # alone leaves it on the live config.yaml: a test whose own fixture forgot
    # it (turn_env-style) and called config.upsert_bot() rewrote the family
    # app's bot roster (2026-10-12: the live advisor block was replaced by a
    # test fixture's). Follow DATA_DIR unless the test already chose a path.
    if not is_disposable(_config.CONFIG_PATH, basetemp):
        monkeypatch.setattr(_config, "CONFIG_PATH", _config.DATA_DIR / "config.yaml")
        _config._invalidate_bots_cache()
    # Same import-time binding for the blob stores and the backup dir. Left on
    # the live store, a test that patched only BACKUP_DIR and called
    # _make_backup() rsynced the family's 2.4 GB media dir into tmp on every
    # run (2026-10-13: two runs filled the tmpfs quota and took the OpenClaw
    # gateway's SQLite worker down with it).
    for name, sub in (("MEDIA_DIR", "media"), ("FILES_DIR", "files"),
                      ("BACKUP_DIR", "backups")):
        if not is_disposable(getattr(_config, name), basetemp):
            monkeypatch.setattr(_config, name, _config.DATA_DIR / sub)
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    (tmp_path / "avatars").mkdir(parents=True, exist_ok=True)

    # Belt and braces, and the part that would have turned a silent disaster
    # into a red test: having redirected, PROVE both paths are disposable. If
    # the rule above is ever wrong again, every test errors here instead of
    # quietly editing the live install.
    for label, value in (("DATA_DIR", _config.DATA_DIR),
                         ("AVATAR_DIR", _config.AVATAR_DIR),
                         ("CONFIG_PATH", _config.CONFIG_PATH)):
        assert is_disposable(value, basetemp), (
            f"config.{label} points at {value}, which is not inside pytest's "
            f"scratch space ({basetemp}). Refusing to run: a test writing "
            f"there would edit a real installation.")

    # The advisor's hooks are bound at app.main IMPORT time with
    # data_dir=config.DATA_DIR -- a snapshot of the REAL directory, taken
    # before this fixture could redirect anything. advisor.ledger() and
    # advisor.index_for() derive advisor.db / knowledge-<tag>.db from that
    # snapshot, so a test that forgot to bind its own hooks wrote ledger rows
    # into the live advisor.db and left a stray knowledge-<tag>.db behind on
    # every run (48 rows and 37 files before this guard existed). Point the
    # bound hooks at this test's data dir and drop the cached ledger/indexes.
    from app import advisor as _advisor
    from app import knowledge as _knowledge
    if _advisor._hooks is not None and not is_disposable(_advisor._hooks.data_dir,
                                                         basetemp):
        monkeypatch.setattr(_advisor._hooks, "data_dir", _config.DATA_DIR)
    if _advisor._ledger is not None and not is_disposable(_advisor._ledger.path,
                                                          basetemp):
        monkeypatch.setattr(_advisor, "_ledger", None)
    for key, idx in list(_advisor._indexes.items()):
        if not is_disposable(idx.db_path, basetemp):
            _advisor._indexes.pop(key, None)

    # ...and fail LOUDLY if anything still opens an advisor ledger or a
    # knowledge index outside scratch space, whatever path it took to get there.
    def _guarded(cls, label):
        real_init = cls.__init__

        def init(self, *a, **kw):
            real_init(self, *a, **kw)
            path = getattr(self, "path", None) or getattr(self, "db_path", None)
            assert path is None or is_disposable(Path(path), basetemp), (
                f"{label} opened at {path}, outside pytest's scratch space "
                f"({basetemp}). Bind the advisor to tmp hooks in this test.")
        monkeypatch.setattr(cls, "__init__", init)

    _guarded(_advisor.Ledger, "advisor.Ledger")
    _guarded(_knowledge.KnowledgeIndex, "knowledge.KnowledgeIndex")

    # The media-origins ledger caches in-process and is keyed off DATA_DIR,
    # which this fixture (and most suites) just moved -- drop the cache so one
    # test's origins can never answer another test's dedup question.
    from app import main as _main
    _main._media_origins_reset()
    yield
    _main._media_origins_reset()


@pytest.fixture(autouse=True)
def _pin_the_autopilot_clock(monkeypatch):
    """Stop the suite's result depending on what time it is.

    Reaction autopilot fires a mood on a bot's reply unless it is night
    (`_AUTOPILOT_NIGHT_HOURS`), so any test that asserts on how many rows a
    turn persists passes between midnight and 07:00 and fails the rest of the
    day — three tests in test_media_dedup.py did exactly that, and which run
    you happened to do decided whether the suite was green.

    A test suite whose answer changes with the clock is not reporting anything.
    Default everything to a night hour (autopilot off, one row per reply);
    test_reaction_autopilot.py pins its own hour per case, which still wins
    because a test's own monkeypatch is applied after this fixture.
    """
    from app import main as _main
    monkeypatch.setattr(_main, "_autopilot_now_hour", lambda: 3)


@pytest.fixture(autouse=True)
def _no_hard_exit_on_failed_startup(monkeypatch):
    """A failed lifespan in a test is an assertion, never a process to kill.

    Production arms a daemon timer that os._exit(3)s a failed startup which
    did not exit on its own (see main._arm_failed_startup_exit). Inside
    pytest that timer would take the whole run down five seconds later.
    tests/test_startup_failure.py exercises the real backstop in a subprocess.
    """
    from app import main as _main
    monkeypatch.setattr(_main, "_HARD_EXIT_ON_FAILED_STARTUP", False,
                        raising=False)


#: The fixed ports of the owner's live services (StudioForge gateway / panel / MCP, SpeechForge,
#: ClawChat, ClawForge, DisPatch itself). A test must never reach one: a wizard or connection
#: discovery that is not stubbed would probe the real gateway on 1234.
LIVE_SERVICE_PORTS = frozenset({1234, 1235, 1236, 7865, 8080, 8288, 8700, 8765})


@pytest.fixture(autouse=True)
def _never_reach_live_service_ports(monkeypatch):
    """Refuse (as a closed port would) any socket connect to a live service's fixed port.

    Tests that need a server start one on an ephemeral port; everything else that reaches for
    1234/8080/... is a test that forgot its stub, and must fail as 'connection refused' instead
    of talking to the real thing.
    """
    import socket

    real_connect, real_connect_ex = socket.socket.connect, socket.socket.connect_ex

    def _blocked(address) -> bool:
        return (isinstance(address, tuple) and len(address) >= 2
                and isinstance(address[1], int) and address[1] in LIVE_SERVICE_PORTS)

    def connect(self, address):
        if _blocked(address):
            raise ConnectionRefusedError(111, f"tests never connect to live service port {address[1]}")
        return real_connect(self, address)

    def connect_ex(self, address):
        if _blocked(address):
            return 111
        return real_connect_ex(self, address)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)


@pytest.fixture(autouse=True)
def _no_real_systemctl(monkeypatch):
    """The suite never talks to a real systemd.

    `harness.unit_state` shells out to `systemctl --user show`. A bare
    container has no systemctl and a CI runner has one but no user session bus,
    so the call failed ("failed to launch systemctl") or returned noise
    depending on the machine. Answer systemctl as "no such unit" everywhere;
    every other argv (the fake dsh the harness tests launch) is untouched, and a
    test that wants specific unit state still patches `harness._run` itself.
    """
    from app import harness

    real = harness._run

    async def run(argv, timeout=harness._OP_TIMEOUT):
        if argv and os.path.basename(str(argv[0])) == "systemctl":
            return 4, "", "Unit could not be found (stubbed by the test suite)"
        return await real(argv, timeout)

    monkeypatch.setattr(harness, "_run", run)


# --------------------------------------------------------------------------- #
# A fully wired app with accounts, for the 3.0 access-control suites.
# --------------------------------------------------------------------------- #

class World:
    """An isolated app + a factory for clients with chosen peer addresses, and the
    handful of helpers every access-control test wants."""

    LOOPBACK = ("127.0.0.1", 50000)
    LAN = ("203.0.113.50", 50001)

    def __init__(self, tmp_path, main_module, db):
        self.tmp = tmp_path
        self.main = main_module
        self.db = db
        self._clients: list = []

    def client(self, addr=None, *, origin: bool = True):
        from fastapi.testclient import TestClient
        c = TestClient(self.main.app, client=addr or self.LOOPBACK)
        c.__enter__()
        self._clients.append(c)
        if origin:
            # What our own page sends on a state-changing fetch: the Host the client
            # talks to and a matching Origin.
            c.headers.update({"Origin": "http://testserver"})
        return c

    def create_owner(self, client=None, *, name="Olive", password="owner-pass1", pin="2468"):
        c = client or self.client()
        r = c.post("/api/auth/setup", json={"name": name, "password": password, "new_pin": pin})
        assert r.status_code == 200, r.text
        return c

    def person(self, owner_client, name, role, *, pin=None, password=None, **extra):
        body = {"name": name, "role": role, **extra}
        if pin:
            body["pin"] = pin
        if password:
            body["password"] = password
        r = owner_client.post("/api/people", json=body)
        assert r.status_code == 201, r.text
        return r.json()

    def login(self, client, *, profile_id=None, name=None, pin=None, password=None):
        body = {k: v for k, v in {"profile_id": profile_id, "name": name, "pin": pin, "password": password}.items() if v}
        return client.post("/api/auth/login", json=body)

    def close(self):
        for c in self._clients:
            c.__exit__(None, None, None)


@pytest.fixture
def world(tmp_path, monkeypatch):
    import asyncio

    from app import auth, config, main
    from app.database import Database

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "config.yaml")
    monkeypatch.setattr(config, "MEDIA_DIR", tmp_path / "media")
    monkeypatch.setattr(config, "FILES_DIR", tmp_path / "files")
    monkeypatch.setattr(config, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(config, "BACKUP_DIR", tmp_path / "backups")
    monkeypatch.setattr(main, "MEDIA_DIR", tmp_path / "media")
    monkeypatch.setattr(main, "FILES_DIR", tmp_path / "files")
    monkeypatch.setattr(auth, "SECURITY_PATH", tmp_path / "security.yaml")
    monkeypatch.setattr(auth, "RECOVERY_PATH", tmp_path / "RECOVERY-CODE.txt")
    monkeypatch.setattr(auth, "TRUSTED_PATH", tmp_path / "trusted-devices.yaml")
    config._invalidate_bots_cache()
    from app import accounts
    accounts.reset_for_tests()
    temp_db = Database(tmp_path / "chats.db")
    monkeypatch.setattr(main, "db", temp_db)
    main._ACK_SEEN.clear()
    main._delivered.clear()
    main._thread_bot.clear()
    w = World(tmp_path, main, temp_db)
    yield w
    w.close()
    asyncio.run(temp_db.close())
