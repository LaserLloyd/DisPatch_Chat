"""A loopback OpenAI-compatible stub for the voice-chat backend tests.

Real sockets on 127.0.0.1 (port 0), no network: the tests that use it are about
what crosses the wire — whether a discarded speculation really closes its HTTP
stream, whether a voice turn's retrieval stops waiting for a slow embedder —
which an in-process mock transport cannot show.

    with StubLLM(delay_s=0.05, words_per_s=40) as llm:
        api = {"provider": "custom", "base_url": llm.base_url, "model": "stub"}
        llm.replies = ["first reply", "second reply"]     # consumed in order
        llm.chat_requests()                                # newest last

Every reply opens with a different "Reply <n>." unless `replies` names it, so
content dedup can never be what makes a second reply vanish. A client that
hangs up mid-stream is recorded as ``aborted`` with the time it was noticed.
"""
from __future__ import annotations

import http.server
import json
import threading
import time
from typing import Self

DEFAULT_REPLY = ("You have two things this afternoon. The dentist is at three, and "
                 "the school call is at four thirty. Want a reminder before each one?")


class _State:
    def __init__(self, **cfg) -> None:
        self.delay_s: float = cfg.get("delay_s", 0.05)
        self.words_per_s: float = cfg.get("words_per_s", 40.0)
        self.embed_delay_s: float = cfg.get("embed_delay_s", 0.0)
        self.replies: list[str] = list(cfg.get("replies", []))
        self.requests: list[dict] = []
        self.counter = 0
        self.lock = threading.Lock()

    def next_reply(self) -> str:
        with self.lock:
            self.counter += 1
            if self.replies:
                return self.replies.pop(0)
            return f"Reply {self.counter}. {DEFAULT_REPLY}"


def _handler(state: _State):
    class H(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):          # quiet
            pass

        def _json(self, code: int, obj) -> None:
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            n = int(self.headers.get("content-length") or 0)
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
            except json.JSONDecodeError:
                body = {}
            path = self.path.rstrip("/")
            if path.endswith("/embeddings"):
                return self._embeddings(body)
            if path.endswith("/chat/completions"):
                return self._chat(body)
            return self._json(404, {"error": {"message": "not found"}})

        def _log(self, entry: dict) -> dict:
            entry["t"] = time.monotonic()
            with state.lock:
                state.requests.append(entry)
            return entry

        def _embeddings(self, body: dict) -> None:
            texts = body.get("input")
            texts = [texts] if isinstance(texts, str) else list(texts or [])
            self._log({"path": "embeddings", "n": len(texts)})
            time.sleep(state.embed_delay_s)
            data = [{"index": i, "embedding": [1.0] + [0.0] * 7} for i in range(len(texts))]
            try:
                self._json(200, {"data": data})
            except (BrokenPipeError, ConnectionResetError):
                pass

        def _chat(self, body: dict) -> None:
            msgs = body.get("messages") or []
            entry = self._log({
                "path": "chat", "stream": bool(body.get("stream")),
                "system": "\n".join(str(m.get("content")) for m in msgs
                                    if m.get("role") == "system"),
                "messages": msgs, "aborted": False, "finished": False})
            reply = state.next_reply()
            entry["reply"] = reply
            time.sleep(state.delay_s)
            if not body.get("stream"):
                try:
                    self._json(200, {"model": "stub", "choices": [
                        {"message": {"role": "assistant", "content": reply},
                         "finish_reason": "stop"}]})
                    entry["finished"] = True
                except (BrokenPipeError, ConnectionResetError):
                    entry["aborted"], entry["aborted_at"] = True, time.monotonic()
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            gap = 1.0 / max(1e-3, state.words_per_s)
            try:
                for i, w in enumerate(reply.split(" ")):
                    piece = w if i == 0 else " " + w
                    chunk = {"choices": [{"index": 0, "delta": {"content": piece}}]}
                    self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
                    self.wfile.flush()
                    time.sleep(gap)
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
                entry["finished"] = True
            except (BrokenPipeError, ConnectionResetError, TimeoutError):
                entry["aborted"], entry["aborted_at"] = True, time.monotonic()

    return H


class StubLLM:
    def __init__(self, **cfg) -> None:
        self.state = _State(**cfg)
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _handler(self.state))
        self.server.daemon_threads = True
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}/v1"
        self._thread: threading.Thread | None = None

    def __enter__(self) -> Self:
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.server.shutdown()
        self.server.server_close()

    def __getattr__(self, name: str):
        # delay_s, words_per_s, embed_delay_s, replies: read through to the state
        return getattr(self.state, name)

    def __setattr__(self, name: str, value) -> None:
        if name in ("delay_s", "words_per_s", "embed_delay_s", "replies"):
            with self.state.lock:
                setattr(self.state, name, list(value) if name == "replies" else value)
        else:
            super().__setattr__(name, value)

    def chat_requests(self) -> list[dict]:
        with self.state.lock:
            return [r for r in self.state.requests if r["path"] == "chat"]

    def embed_requests(self) -> list[dict]:
        with self.state.lock:
            return [r for r in self.state.requests if r["path"] == "embeddings"]
