#!/usr/bin/env python3
"""A scratch DisPatch for the e2e / axe / perf / smoke harnesses (and the release matrix).

One class, :class:`Scratch`, and a small CLI around it. It starts the app from THIS
checkout on a port you name, against a data directory you name, and seeds it the way
a person would (the first-run API path, not by writing databases):

* the protected Admin **Owner** (install PIN), an **Admin**, a **User**, a **Basic**
  profile and the PIN-less default profile **DisPatch** (``POST /api/auth/setup``, then
  ``/api/wizard/family`` + ``/api/wizard/finish``);
* three agent bots (Nova, Scribe, Helper) written into ``config.yaml`` before the first
  boot, so the claim adopts them as the Owner's own, exactly like a real install;
* one model connection pointing at :class:`LLMStub`, a local OpenAI-compatible server
  that answers every chat completion with a fixed sentence (``/api/wizard/connection``
  assigns it to Atlas and to every profile's own Atlas copy);
* optionally the fake voice add-on (``tests/e2e/fakevoice``) that registers
  ``bots.rowExtra`` and a chat-header Voice button;
* a machine API key (``POST /api/auth/api-token``) for the keyed-caller rows.

Everything it made is described in ``<data>/e2e-seed.json`` (test PINs and the test
key: the data dir is throwaway test data, never a real install).

Safety (load-bearing; this runs on a box with a live install and a live agent gateway):

* the child gets a CLEAN environment: every ``DISPATCH_*``, ``OPENCLAW_*`` and
  ``LOCAL_CHAT_*`` variable of the caller is dropped, ``HOME`` is ``<data>/home``;
* the OpenClaw transport is pointed at nothing: ``DISPATCH_GATEWAY_WS=0``,
  ``DISPATCH_MIRROR=0``, ``OPENCLAW_BIN`` = a path that does not exist,
  ``OPENCLAW_GATEWAY_URL`` = a closed loopback port, ``OPENCLAW_GATEWAY_TOKEN`` = a
  dummy (so ``~/.openclaw/openclaw.json`` is never read). The smoke test overrides
  the URL with its own fake gateway via ``gateway_url=``;
* :func:`guard_data_dir` refuses the live data directory and anything outside a
  directory the caller owns;
* it binds 127.0.0.1 only and kills only the process it started.

Static-tree override (the service-worker upgrade-path check): ``static_dir=`` makes the
child serve another ``frontend/static`` tree (e.g. ``git archive <base> frontend/static``)
by patching ``app.config.FRONTEND_DIR`` before ``app.main`` is imported; the backend is
always this checkout's. ``Scratch.restart(static_dir=None)`` switches back to this tree.

CLI::

    python tests/e2e/_scratch.py up --port 18856 --data ~/.cache/dispatch-e2e/run1 [--fresh]
           [--static DIR] [--no-voice] [--bots N] [--threads N]
    # prints the seed JSON on one line, then serves until Ctrl-C / SIGTERM

Library::

    from _scratch import Scratch
    with Scratch(18856, data_dir, fresh=True) as s:
        seed = s.seed()                  # dict, also in <data>/e2e-seed.json
        api = s.api("User")              # a signed-in urllib client
        api.call("GET", "/api/auth/me")

Fixed argv everywhere; no ``shell=True``. Python 3.11+ standard library only in the
parent; the child runs with the backend's own venv.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import http.cookiejar
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
BACKEND = REPO / "backend"
FAKE_VOICE_DIR = Path(__file__).resolve().parent / "fakevoice"
DEFAULT_DATA_ROOT = Path(os.environ.get("DISPATCH_E2E_DATA") or (Path.home() / ".cache" / "dispatch-e2e"))

#: The five seeded profiles. name -> (role, pin). The Owner's PIN is the install PIN.
PROFILES: dict[str, tuple[str, str]] = {
    "Owner": ("admin", "2468"),
    "Admin": ("admin", "1357"),
    "User": ("user", "1122"),
    "Basic": ("basic", "3344"),
    "DisPatch": ("user", ""),         # the default PIN-less profile (wizard ensure_default)
}
ROLE_ORDER = ["Owner", "Admin", "User", "Basic", "DisPatch"]

#: Agent bots written into config.yaml before the first boot (the claim adopts them).
#: Generic names on purpose (test data in a public repo): Nova plays the lead agent bot,
#: Scribe the second reaction bot, Helper the third (a mixed-case id, like real rosters).
AGENT_BOTS = [
    {"id": "main", "name": "Nova", "emoji": "⚡", "agent": "main", "reactions": True},
    {"id": "scribe", "name": "Scribe", "emoji": "📝", "agent": "scribe", "reactions": True},
    {"id": "Helper", "name": "Helper", "emoji": "🦊", "agent": "helper"},
]

STUB_REPLY = "Hello from the test model. Everything here is test data."
STUB_MODEL = "e2e-stub-model"

_STRIP_ENV_PREFIXES = ("DISPATCH_", "OPENCLAW_", "LOCAL_CHAT_", "SF_", "STUDIOFORGE_")
_CLOSED_PORT_URL = "ws://127.0.0.1:9"


# --------------------------------------------------------------------------- #
# Safety
# --------------------------------------------------------------------------- #

def guard_data_dir(path: Path) -> Path:
    """Refuse a data dir that could be a real install. Returns the resolved path."""
    p = Path(path).expanduser().resolve()
    live = (Path.home() / ".local" / "share" / "local-chat").resolve()
    if p == live or live in p.parents or p in live.parents:
        raise SystemExit(f"refusing the live data directory: {p}")
    if p == Path.home().resolve() or p == Path("/") or len(p.parts) < 3:
        raise SystemExit(f"refusing a data directory that is too broad: {p}")
    if p.exists() and any(p.iterdir()) and not (p / ".e2e-scratch").exists():
        raise SystemExit(f"{p} exists, is not empty and was not made by this harness "
                         "(no .e2e-scratch marker): refusing to use it")
    return p


def free_port(start: int = 18856, end: int = 18869) -> int:
    for port in range(start, end + 1):
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise SystemExit(f"no free port in {start}-{end}")


def find_headless_shell() -> str | None:
    """The Playwright chromium-headless-shell already on this machine (never downloads)."""
    env = os.environ.get("DISPATCH_E2E_CHROME")
    if env:
        return env
    root = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or (Path.home() / ".cache" / "ms-playwright"))
    for d in sorted(root.glob("chromium_headless_shell-*"), reverse=True):
        for name in ("chrome-headless-shell", "chrome-headless-shell.exe", "headless_shell"):
            hits = sorted(d.rglob(name))
            if hits:
                return str(hits[0])
    return None


# --------------------------------------------------------------------------- #
# The fake LLM (OpenAI-compatible)
# --------------------------------------------------------------------------- #

class _StubHandler(BaseHTTPRequestHandler):
    server: _StubServer
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):           # quiet
        pass

    def _json(self, code: int, body: Any) -> None:
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        p = self.path.split("?")[0].rstrip("/")
        if p in ("/v1/models", "/models"):
            return self._json(200, {"object": "list", "data": [
                {"id": STUB_MODEL, "object": "model", "owned_by": "e2e"}]})
        if p in ("", "/health", "/v1"):
            return self._json(200, {"ok": True})
        return self._json(404, {"error": {"message": "not found"}})

    def do_POST(self):
        p = self.path.split("?")[0].rstrip("/")
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except json.JSONDecodeError:
            body = {}
        if p not in ("/v1/chat/completions", "/chat/completions"):
            if p in ("/v1/embeddings", "/embeddings"):
                inputs = body.get("input") or [""]
                inputs = inputs if isinstance(inputs, list) else [inputs]
                return self._json(200, {"object": "list", "data": [
                    {"object": "embedding", "index": i, "embedding": [0.0] * 8} for i in range(len(inputs))]})
            return self._json(404, {"error": {"message": "not found"}})
        with self.server.lock:
            self.server.calls += 1
        reply = self.server.reply
        if self.server.delay:
            time.sleep(self.server.delay)
        if not body.get("stream"):
            return self._json(200, {
                "id": "e2e-1", "object": "chat.completion", "model": STUB_MODEL,
                "choices": [{"index": 0, "finish_reason": "stop",
                             "message": {"role": "assistant", "content": reply}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20}})
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        words = reply.split(" ")
        for i, w in enumerate(words):
            chunk = {"id": "e2e-1", "object": "chat.completion.chunk", "model": STUB_MODEL,
                     "choices": [{"index": 0, "delta": {"content": (w if i == 0 else " " + w)},
                                  "finish_reason": None}]}
            self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
            self.wfile.flush()
        end = {"id": "e2e-1", "object": "chat.completion.chunk", "model": STUB_MODEL,
               "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
        self.wfile.write(f"data: {json.dumps(end)}\n\ndata: [DONE]\n\n".encode())
        self.wfile.flush()
        self.close_connection = True


class _StubServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, addr):
        super().__init__(addr, _StubHandler)
        self.lock = threading.Lock()
        self.calls = 0
        self.reply = STUB_REPLY
        self.delay = 0.0


class LLMStub:
    """A local OpenAI-compatible server on 127.0.0.1 (thread). ``.url`` is its /v1 base."""

    def __init__(self, port: int = 0):
        self._srv = _StubServer(("127.0.0.1", port))
        self.port = self._srv.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}/v1"
        self._t = threading.Thread(target=self._srv.serve_forever, name="e2e-llm-stub", daemon=True)

    @property
    def calls(self) -> int:
        return self._srv.calls

    def start(self) -> LLMStub:
        self._t.start()
        return self

    def stop(self) -> None:
        self._srv.shutdown()
        self._srv.server_close()


# --------------------------------------------------------------------------- #
# A tiny cookie-keeping JSON client (what a browser tab is, minus the DOM)
# --------------------------------------------------------------------------- #

class Api:
    def __init__(self, base: str, *, key: str | None = None):
        self.base = base.rstrip("/")
        self.jar = http.cookiejar.CookieJar()
        self.op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
        self.key = key

    def call(self, method: str, path: str, body: Any = None, *, headers: dict | None = None,
             raw: bytes | None = None, timeout: float = 30) -> tuple[int, Any]:
        # A tab sends Origin + Sec-Fetch-Site (CSRF rule); a machine (cron, dispatch-api)
        # sends neither, only its key, or the server reads it as a browser.
        h = {} if self.key else {"Origin": self.base, "Sec-Fetch-Site": "same-origin"}
        data = raw
        if body is not None:
            data = json.dumps(body).encode()
            h["Content-Type"] = "application/json"
        if self.key:
            h["X-API-Key"] = self.key
        h.update(headers or {})
        req = urllib.request.Request(self.base + path, method=method, data=data, headers=h)
        try:
            with self.op.open(req, timeout=timeout) as r:
                txt = r.read()
                code = r.status
        except urllib.error.HTTPError as e:
            txt, code = e.read(), e.code
        try:
            return code, json.loads(txt) if txt else None
        except (json.JSONDecodeError, UnicodeDecodeError):
            return code, txt[:500]

    def ok(self, method: str, path: str, body: Any = None, **kw) -> Any:
        code, out = self.call(method, path, body, **kw)
        if not 200 <= code < 300:
            raise RuntimeError(f"{method} {path} -> {code}: {str(out)[:400]}")
        return out

    def cookie_header(self) -> str:
        return "; ".join(f"{c.name}={c.value}" for c in self.jar)

    def cookies_for_playwright(self, url: str) -> list[dict]:
        return [{"name": c.name, "value": c.value, "url": url} for c in self.jar]


# --------------------------------------------------------------------------- #
# The scratch instance
# --------------------------------------------------------------------------- #

def backend_python() -> str:
    """The backend venv's interpreter (made by `uv sync` / a first `uv run`)."""
    for rel in (".venv/bin/python", ".venv/Scripts/python.exe"):
        p = BACKEND / rel
        if p.exists():
            return str(p)
    out = subprocess.run(["uv", "run", "python", "-c", "import sys; print(sys.executable)"],
                         cwd=BACKEND, capture_output=True, text=True, check=True)
    return out.stdout.strip().splitlines()[-1]


class Scratch:
    def __init__(self, port: int, data_dir: Path | str, *, static_dir: Path | str | None = None,
                 fake_voice: bool = True, fresh: bool = False, stub: LLMStub | None = None,
                 extra_bots: int = 0, gateway_url: str | None = None,
                 agent_bots: list[dict] | None = None,
                 env: dict[str, str] | None = None, log_path: Path | None = None):
        self.port = int(port)
        self.data = guard_data_dir(Path(data_dir))
        self.static_dir = Path(static_dir).resolve() if static_dir else None
        self.fake_voice = fake_voice
        self.fresh = fresh
        self._own_stub = stub is None
        self.stub = stub
        self.extra_bots = int(extra_bots)
        self.gateway_url = gateway_url
        self.agent_bots = AGENT_BOTS if agent_bots is None else list(agent_bots)
        self.extra_env = dict(env or {})
        self.base = f"http://127.0.0.1:{self.port}"
        self.log_path = log_path or (self.data.parent / f"{self.data.name}.server.log")
        self.proc: subprocess.Popen | None = None
        self._seed: dict | None = None

    # ---- lifecycle -------------------------------------------------------
    def __enter__(self) -> Scratch:
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()
        if self._own_stub and self.stub is not None:
            self.stub.stop()
            self.stub = None

    def _env(self) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if not k.startswith(_STRIP_ENV_PREFIXES)}
        home = self.data / "home"
        home.mkdir(parents=True, exist_ok=True)
        env.update({
            "HOME": str(home), "USERPROFILE": str(home),
            "XDG_CONFIG_HOME": str(home / ".config"), "XDG_DATA_HOME": str(home / ".local" / "share"),
            "XDG_CACHE_HOME": str(home / ".cache"), "XDG_STATE_HOME": str(home / ".local" / "state"),
            "TMPDIR": str(self.data / "tmp"),
            "DISPATCH_DATA_DIR": str(self.data),
            "DISPATCH_GATEWAY_WS": "1" if self.gateway_url else "0",
            "DISPATCH_MIRROR": "0",
            "OPENCLAW_BIN": str(self.data / "no-openclaw-here"),
            "OPENCLAW_GATEWAY_URL": self.gateway_url or _CLOSED_PORT_URL,
            "OPENCLAW_GATEWAY_TOKEN": "e2e-fake-gateway-token",
            "PYTHONUNBUFFERED": "1",
        })
        (self.data / "tmp").mkdir(exist_ok=True)
        if self.fake_voice:
            env["DISPATCH_EXTENSIONS"] = "e2e_fakevoice:EXTENSION"
            env["PYTHONPATH"] = os.pathsep.join([str(FAKE_VOICE_DIR)] + (
                [env["PYTHONPATH"]] if env.get("PYTHONPATH") else []))
        else:
            env["DISPATCH_EXTENSIONS"] = "off"
        env.update(self.extra_env)
        return env

    def start(self, *, timeout: float = 90) -> Scratch:
        if self.fresh and self.data.exists():
            shutil.rmtree(self.data)
        self.fresh = False
        self.data.mkdir(parents=True, exist_ok=True)
        (self.data / ".e2e-scratch").write_text("made by tests/e2e/_scratch.py\n")
        if self.stub is None:
            old_port = 0
            with contextlib.suppress(Exception):
                old_port = int(json.loads(self.seed_path.read_text())["stub_url"].split(":")[2].split("/")[0])
            try:
                self.stub = LLMStub(old_port).start()
            except OSError:
                self.stub = LLMStub().start()
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", self.port)) == 0:
                raise SystemExit(f"port {self.port} is already in use")
        argv = [backend_python(), str(Path(__file__).resolve()), "_child", "--port", str(self.port),
                "--extra-bots", str(self.extra_bots), "--agent-bots", json.dumps(self.agent_bots)]
        if self.static_dir:
            argv += ["--static", str(self.static_dir)]
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        log = open(self.log_path, "ab")
        kw: dict[str, Any] = {}
        if os.name == "posix":
            kw["start_new_session"] = True
        self.proc = subprocess.Popen(argv, cwd=BACKEND, env=self._env(), stdout=log,
                                     stderr=subprocess.STDOUT, **kw)
        log.close()
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout:
            if self.proc.poll() is not None:
                raise RuntimeError(f"scratch server exited ({self.proc.returncode}); see {self.log_path}")
            try:
                with urllib.request.urlopen(self.base + "/api/health", timeout=2) as r:
                    if r.status == 200:
                        self._repoint_stub()
                        return self
            except (urllib.error.URLError, OSError):
                pass
            time.sleep(0.3)
        self.stop()
        raise RuntimeError(f"scratch server did not answer /api/health in {timeout}s; see {self.log_path}")

    def _repoint_stub(self) -> None:
        """A restart on an existing data dir: point the seeded connection at this run's stub."""
        if not self.seed_path.exists():
            return
        seed = json.loads(self.seed_path.read_text())
        if seed.get("stub_url") == self.stub.url:
            return
        self._seed = seed
        owner = self.api("Owner")
        conns = owner.ok("GET", "/api/connections")
        rows = conns.get("connections", conns) if isinstance(conns, dict) else conns
        for c in rows:
            if c.get("name") == "Test model (stub)":
                owner.ok("PATCH", f"/api/connections/{c['id']}", {"base_url": self.stub.url})
        seed["stub_url"] = self.stub.url
        self.seed_path.write_text(json.dumps(seed, indent=2))

    def stop(self) -> None:
        p, self.proc = self.proc, None
        if p is None or p.poll() is not None:
            return
        # Only the process we started (and its own session/group, POSIX).
        with contextlib.suppress(ProcessLookupError):
            if os.name == "posix":
                os.killpg(p.pid, signal.SIGTERM)
            else:
                p.terminate()
        try:
            p.wait(timeout=20)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError):
                if os.name == "posix":
                    os.killpg(p.pid, signal.SIGKILL)
                else:
                    p.kill()
            p.wait(timeout=10)

    def restart(self, *, static_dir: Path | str | bool | None = False) -> Scratch:
        """Stop and start again on the same data and port. ``static_dir=None`` = this
        checkout's tree; a path = that tree; False (default) = keep the current choice."""
        self.stop()
        if static_dir is not False:
            self.static_dir = Path(static_dir).resolve() if static_dir else None
        return self.start()

    # ---- clients ---------------------------------------------------------
    def api(self, profile: str | None = None, *, remember: bool = False) -> Api:
        a = Api(self.base)
        if profile:
            self.login(a, profile, remember=remember)
        return a

    def machine(self, key: str | None = None) -> Api:
        return Api(self.base, key=key if key is not None else self.seed()["machine_key"])

    def login(self, a: Api, profile: str, *, remember: bool = False) -> dict:
        seed = self.seed()
        p = seed["profiles"][profile]
        body: dict[str, Any] = {"profile_id": p["id"], "remember": remember}
        if p["pin"]:
            body["pin"] = p["pin"]
        return a.ok("POST", "/api/auth/login", body)

    def set_machine(self, *, loopback_trust: bool | None = None,
                    scopes: list[str] | None = None) -> dict:
        """Change how machines are recognised (as the Owner, through the API). The key stays."""
        body: dict[str, Any] = {}
        if loopback_trust is not None:
            body["machine_loopback_trust"] = loopback_trust
        if scopes is not None:
            body["machine_scopes"] = scopes
        return self.api("Owner").ok("POST", "/api/auth/api-token", body)

    # ---- seeding ---------------------------------------------------------
    @property
    def seed_path(self) -> Path:
        return self.data / "e2e-seed.json"

    def seed(self) -> dict:
        if self._seed is not None:
            return self._seed
        if self.seed_path.exists():
            self._seed = json.loads(self.seed_path.read_text())
            return self._seed
        owner = Api(self.base)
        role, pin = PROFILES["Owner"]
        # Resumable: a run that died half way re-enters as the Owner and adds what is missing.
        code, out = owner.call("POST", "/api/auth/setup", {"name": "Owner", "new_pin": pin})
        if code != 200:
            owner.ok("POST", "/api/auth/login", {"name": "Owner", "pin": pin})
        conn = owner.ok("POST", "/api/wizard/connection", {
            "type": "openai_compatible", "name": "Test model (stub)", "base_url": self.stub.url,
            "model": STUB_MODEL, "character": "atlas"})
        if not (conn.get("test") or {}).get("ok"):
            raise RuntimeError(f"the stub connection did not test ok: {conn.get('test')}")
        have = {p["name"] for p in owner.ok("GET", "/api/people")["profiles"]}
        # The wizard's family step makes Users and Basics (and the default profile); a second
        # Admin goes through People, as an Admin would add one.
        people = [{"name": n, "role": PROFILES[n][0], "pin": PROFILES[n][1]}
                  for n in ("User", "Basic") if n not in have]
        fam = owner.ok("POST", "/api/wizard/family", {"people": people})
        if "Admin" not in have:
            owner.ok("POST", "/api/people", {"name": "Admin", "role": "admin", "pin": PROFILES["Admin"][1]})
        owner.ok("POST", "/api/wizard/network", {"exposure": "local"})
        owner.ok("POST", "/api/wizard/finish")
        # The live posture since R1.5: loopback is not trusted, every named scope but the two
        # the live box leaves out (openapi, guide_seed). ``set_machine`` changes it per test.
        scopes = [s for s in owner.ok("GET", "/api/auth/api-token")["all_scopes"]
                  if s not in ("machine:openapi", "machine:guide_seed")]
        key = owner.ok("POST", "/api/auth/api-token", {
            "rotate": True, "machine_loopback_trust": False, "machine_scopes": scopes})
        listing = owner.ok("GET", "/api/people")
        by_name = {p["name"]: p for p in listing["profiles"]}
        seed = {
            "base": self.base, "port": self.port, "data_dir": str(self.data),
            "stub_url": self.stub.url, "stub_reply": STUB_REPLY,
            "fake_voice": self.fake_voice,
            "default_profile": fam.get("default_profile"),
            "machine_key": key.get("token") or key.get("api_token") or key.get("key"),
            "profiles": {
                n: {"id": by_name[n]["id"], "role": by_name[n]["role"], "pin": PROFILES[n][1],
                    "protected": bool(by_name[n].get("protected"))}
                for n in ROLE_ORDER},
        }
        if not seed["machine_key"]:
            raise RuntimeError(f"no machine key in the api-token answer: {sorted(key)}")
        self.seed_path.write_text(json.dumps(seed, indent=2))
        self._seed = seed
        return seed


# --------------------------------------------------------------------------- #
# Static-tree helpers (SW upgrade path)
# --------------------------------------------------------------------------- #

def export_static(commit: str, dest: Path, repo: Path = REPO) -> Path:
    """``git archive <commit> frontend/static`` into ``dest``; returns ``dest/frontend/static``."""
    dest = Path(dest)
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    arch = subprocess.run(["git", "-C", str(repo), "archive", "--format=tar", commit, "frontend/static"],
                          capture_output=True, check=True)
    subprocess.run(["tar", "-x", "-C", str(dest)], input=arch.stdout, check=True)
    return dest / "frontend" / "static"


def tree_hashes(root: Path) -> dict[str, str]:
    out = {}
    for p in sorted(Path(root).rglob("*")):
        if p.is_file():
            out[p.relative_to(root).as_posix()] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


# --------------------------------------------------------------------------- #
# The child (runs inside the backend venv)
# --------------------------------------------------------------------------- #

def _child(args) -> None:
    sys.path.insert(0, str(BACKEND))
    from app import config
    if args.static:
        static = Path(args.static).resolve()
        if not (static / "index.html").exists():
            raise SystemExit(f"--static {static}: no index.html")
        config.FRONTEND_DIR = static
        from app import settings_schema
        settings_schema._frontend_dir = lambda: static
    cfg_path = Path(os.environ["DISPATCH_DATA_DIR"]) / "config.yaml"
    marker = Path(os.environ["DISPATCH_DATA_DIR"]) / ".e2e-bots-seeded"
    if not marker.exists():
        import yaml
        config.load_bots()                         # materialise the shipped roster
        doc = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
        bots = doc.setdefault("bots", [])
        have = {b.get("id") for b in bots}
        order = max([int(b.get("order") or 0) for b in bots] + [0]) + 1
        for b in json.loads(args.agent_bots):
            if b["id"] not in have:
                bots.append({**b, "order": order})
                order += 1
        for i in range(args.extra_bots):
            bid = f"bench{i:02d}"
            if bid not in have:
                # A plain household direct bot (not a template: no copies per profile).
                bots.append({"id": bid, "name": f"Bench {i:02d}", "emoji": "🤖", "order": order,
                             "audience": "all", "advisor": {"providers": []}})
                order += 1
        cfg_path.write_text(yaml.safe_dump(doc, allow_unicode=True, sort_keys=False), encoding="utf-8")
        config._invalidate_bots_cache()
        marker.write_text("1\n")
    import uvicorn
    uvicorn.run("app.main:app", host="127.0.0.1", port=args.port, log_level="warning",
                ws_ping_interval=None)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    ch = sub.add_parser("_child", help=argparse.SUPPRESS)
    ch.add_argument("--port", type=int, required=True)
    ch.add_argument("--static")
    ch.add_argument("--extra-bots", type=int, default=0)
    ch.add_argument("--agent-bots", default=json.dumps(AGENT_BOTS))
    up = sub.add_parser("up", help="start + seed a scratch instance and serve until interrupted")
    up.add_argument("--port", type=int, default=0, help="default: the first free one in 18856-18869")
    up.add_argument("--data", default=str(DEFAULT_DATA_ROOT / "scratch"))
    up.add_argument("--fresh", action="store_true", help="delete the data dir first")
    up.add_argument("--static", help="serve this frontend/static tree instead of the checkout's")
    up.add_argument("--no-voice", action="store_true", help="no fake voice add-on")
    up.add_argument("--bots", type=int, default=0, help="extra household direct bots (perf)")
    args = ap.parse_args(argv)
    if args.cmd == "_child":
        _child(args)
        return 0
    port = args.port or free_port()
    s = Scratch(port, args.data, static_dir=args.static, fake_voice=not args.no_voice,
                fresh=args.fresh, extra_bots=args.bots)
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    with s:
        seed = s.seed()
        print(json.dumps({**seed, "log": str(s.log_path)}), flush=True)
        while not stop.is_set() and s.proc and s.proc.poll() is None:
            stop.wait(1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
