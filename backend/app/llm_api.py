"""Direct LLM-provider backend — the "Connect an AI" path.

DisPatch's original backend is an agent runtime (OpenClaw): DisPatch spawns its
CLI and tails its session files, which means the runtime has to live on this
machine and has to be installed before anything answers. That is a real barrier
for someone who just cloned the repo and wants to see the app work.

This module is the other door. A bot with an ``api`` block in config.yaml does
not go near the CLI: its turns are plain HTTPS calls to an LLM provider — LM
Studio on the same box, or OpenAI, or Anthropic, or anything OpenAI-compatible.
Two fields (a base URL and a model) and the app has an assistant in it.

What this is NOT
----------------
It is not an agent. There are no tools, no file access, no subagents, no
transcript to reconcile — so none of the machinery that exists to recover a
half-finished agent turn applies here. A turn is one request and one reply.
That simplicity is the feature: it is also why the error handling can be
exhaustive rather than best-effort.

Where it plugs in
-----------------
``main.run_agent_turn`` checks ``bot.api`` first and routes here. Replies are
persisted through the SAME funnel every other assistant message uses
(``_deliver_assistant_text``), so the sanitizers, the reaction-marker stripping,
the dedup and the reaction-marker stripping all still apply — see :func:`bind`.

Streaming
---------
OpenAI-shape providers stream for real (:func:`stream_chat`): deltas go out
through the same provisional-bubble protocol the gateway transport and the
advisor use (:class:`StreamEmitter`), and the finished row claims the bubble.
A server that ignores ``stream: true`` and answers with one JSON body still
works — that reply is delivered through the simulated-streaming seam, as is
every Anthropic reply (the SDK path is not streamed). One retry with a short
backoff covers a 429/502/503/504 that arrives BEFORE any text; nothing is ever
retried once a token has been shown.

Keys
----
``api_key_env`` names an environment variable and WINS over a key stored in
config.yaml, so an operator can keep the secret out of the data directory
entirely. When neither is set and the provider needs one, the call fails with a
message that says exactly that rather than a provider 401.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import functools
import json
import logging
import os
import re
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlparse, urlunparse

import httpx

from . import config, personas

log = logging.getLogger("local-chat.llm")


# --------------------------------------------------------------------------- #
# Provider presets
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Provider:
    """One row of the pick-list in the setup panel.

    `kind` is the wire protocol, not the vendor: everything except Anthropic
    speaks the OpenAI chat-completions shape, which is why "custom" exists at
    all — most self-hosted servers and most smaller vendors are that shape.
    """

    id: str
    label: str
    # Short display name for the bot this preset creates. The `label` above is
    # written for a pick-list ("Custom (OpenAI-compatible)"), which is exactly
    # wrong as the name of a character in a chat sidebar — that wants one or
    # two words. The operator can override either way in Advanced.
    bot_name: str = ""
    base_url: str = ""
    kind: str = "openai"            # "openai" | "anthropic"
    key_required: bool = True       # a key MUST be supplied
    key_accepted: bool = True       # show the key field at all
    key_env: str = ""               # the environment variable people expect
    docs: str = ""                  # where the operator gets an API key
    local: bool = False             # runs on the operator's own machine
    models: tuple[str, ...] = ()    # suggestions; first is the default
    editable_base_url: bool = False  # is the URL the thing you configure?
    # Merged into every chat-completions body for this preset (a bot's own
    # `api.extra_body` is applied after it and wins). Excluded from hashing:
    # the dataclass is frozen, the dict is not.
    extra_body: dict | None = field(default=None, hash=False, compare=False)


# Suggested models are only listed where the id is stable and the vendor is
# unlikely to retire it out from under a fresh install. For everyone else the
# Test button lists what the account/server actually has, which is better data
# than a guess baked into this file — an invented model id produces a 404 that
# reads like a broken app.
PROVIDERS: tuple[Provider, ...] = (
    Provider(
        id="lmstudio", label="LM Studio", bot_name="LM Studio",
        base_url="http://127.0.0.1:1234/v1",
        key_required=False, key_accepted=False, local=True,
    ),
    Provider(
        id="ollama", label="Ollama", bot_name="Ollama",
        base_url="http://127.0.0.1:11434/v1",
        key_required=False, key_accepted=False, local=True,
    ),
    Provider(
        # 2.3.7: llama.cpp's own `llama-server` (default port 8080) or
        # StudioForge (a llama.cpp wrapper that loads models just in time and
        # unloads them when idle; it listens on 1234). Either way the address
        # is the thing to set, so the field is editable and no key is needed.
        id="llamacpp", label="Local server (llama.cpp / StudioForge)",
        bot_name="Local model", base_url="http://127.0.0.1:8080/v1",
        key_required=False, local=True, editable_base_url=True,
    ),
    Provider(
        id="openai", label="OpenAI", bot_name="OpenAI", base_url="https://api.openai.com/v1",
        key_env="OPENAI_API_KEY", docs="https://platform.openai.com/api-keys",
        models=("gpt-4o-mini", "gpt-4o"),
    ),
    Provider(
        id="anthropic", label="Anthropic (Claude)", bot_name="Claude", base_url="https://api.anthropic.com",
        kind="anthropic", key_env="ANTHROPIC_API_KEY",
        docs="https://console.anthropic.com/settings/keys",
        models=("claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5"),
    ),
    Provider(
        id="deepseek", label="DeepSeek", bot_name="DeepSeek", base_url="https://api.deepseek.com/v1",
        key_env="DEEPSEEK_API_KEY", docs="https://platform.deepseek.com/api_keys",
        models=("deepseek-chat", "deepseek-reasoner"),
    ),
    Provider(
        id="groq", label="Groq", bot_name="Groq", base_url="https://api.groq.com/openai/v1",
        key_env="GROQ_API_KEY", docs="https://console.groq.com/keys",
    ),
    Provider(
        id="openrouter", label="OpenRouter", bot_name="OpenRouter", base_url="https://openrouter.ai/api/v1",
        key_env="OPENROUTER_API_KEY", docs="https://openrouter.ai/keys",
    ),
    Provider(
        id="mistral", label="Mistral", bot_name="Mistral", base_url="https://api.mistral.ai/v1",
        key_env="MISTRAL_API_KEY", docs="https://console.mistral.ai/api-keys",
        models=("mistral-large-latest", "mistral-small-latest"),
    ),
    Provider(
        id="xai", label="xAI (Grok)", bot_name="Grok", base_url="https://api.x.ai/v1",
        key_env="XAI_API_KEY", docs="https://console.x.ai/",
        # docs.x.ai/docs/models recommends Grok 4.7 for chat (2026-10-14).
        models=("grok-4.7",),
    ),
    Provider(
        id="together", label="Together AI", bot_name="Together", base_url="https://api.together.xyz/v1",
        key_env="TOGETHER_API_KEY", docs="https://api.together.xyz/settings/api-keys",
    ),
    Provider(
        id="minimax", label="MiniMax", bot_name="MiniMax", base_url="https://api.minimax.io/v1",
        key_env="MINIMAX_API_KEY", docs="https://platform.minimax.io/",
        models=("MiniMax-M3",),
        extra_body={"thinking": {"type": "disabled"}, "reasoning_split": True},
    ),
    Provider(
        # No docs link: the panel's link is worded "Where do I get a key?", and
        # for a server the operator is standing up themselves the answer is
        # "you decide". docs/llm-providers.md covers this case in prose.
        id="custom", label="Custom (OpenAI-compatible)", bot_name="Assistant",
        base_url="", key_required=False, editable_base_url=True,
    ),
)

PROVIDER_BY_ID: dict[str, Provider] = {p.id: p for p in PROVIDERS}


def providers_public() -> list[dict]:
    """The preset table as the setup panel consumes it. Contains no secrets —
    `key_env` is the NAME of an environment variable, never its value."""
    return [
        {
            "id": p.id, "label": p.label, "bot_name": p.bot_name,
            "base_url": p.base_url, "kind": p.kind,
            "key_required": p.key_required, "key_accepted": p.key_accepted,
            "key_env": p.key_env, "docs": p.docs, "local": p.local,
            "models": list(p.models), "editable_base_url": p.editable_base_url,
        }
        for p in PROVIDERS
    ]


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #


class ApiError(Exception):
    """A turn (or a probe) that failed for a reason the operator can act on.

    Mirrors ``openclaw.AgentError``'s two-part shape so main.py can broadcast it
    down the existing error-bubble path unchanged: `message` is the one-line
    headline, `detail` is the sentence that says what to do about it.
    """

    def __init__(self, message: str, detail: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail


# --------------------------------------------------------------------------- #
# Timeouts and limits
# --------------------------------------------------------------------------- #

CONNECT_TIMEOUT_S = 10.0
READ_TIMEOUT_S = 120.0
# The probe is interactive — somebody is watching a spinner in a modal — so it
# gets a much shorter leash than a real turn.
PROBE_READ_TIMEOUT_S = 25.0
# A chat completion is text. Anything past this is a misconfigured URL pointing
# at something that is not an API (a file server, a captive portal, a video),
# and reading it into memory is the only harm it could do us.
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
# Redirects are followed MANUALLY (see _request) so each hop's scheme can be
# checked. Three is more than any real API needs and stops a redirect loop.
MAX_REDIRECTS = 3

DEFAULT_HISTORY_CHARS = 24_000
DEFAULT_MAX_TOKENS = 8192

# The prompt a bot gets when its operator wrote none: no content framing of ours (4.0 has
# no Restricted tier; bots answer unrestricted for whoever may use them).
DEFAULT_SYSTEM_PROMPT = (
    "You are {name}, an assistant in a private chat app called DisPatch Chat. "
    "Keep replies conversational."
)


# --------------------------------------------------------------------------- #
# URL handling
# --------------------------------------------------------------------------- #


def normalize_base_url(url: str) -> str:
    """Validate + tidy an operator-supplied base URL.

    Raises ApiError for anything that is not http(s) with a host. This is an
    operator pointing DisPatch at a backend they chose, so reaching an
    arbitrary host is the FEATURE, not a vulnerability to be blocked — the
    check here is about schemes, not destinations: `file://`, `ftp://` and a
    bare `localhost:1234` (which urlparse reads as scheme "localhost") are
    mistakes, and one of them would have us reading the filesystem.
    """
    url = (url or "").strip().rstrip("/")
    if not url:
        raise ApiError("A base URL is required",
                       "Enter the address of the server, e.g. "
                       "http://127.0.0.1:1234/v1")
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ApiError(
            "The base URL must start with http:// or https://",
            f"Got {parsed.scheme or 'no'} scheme. Only http and https are "
            "supported — a bare host:port is read as a scheme, so write the "
            "http:// prefix out in full.")
    if not parsed.hostname:
        raise ApiError("The base URL has no host",
                       f"{url!r} does not name a server to connect to.")
    return urlunparse(parsed._replace(path=parsed.path.rstrip("/")))


def _join(base: str, path: str) -> str:
    return f"{base.rstrip('/')}/{path.lstrip('/')}"


# --------------------------------------------------------------------------- #
# HTTP plumbing (OpenAI-compatible providers)
# --------------------------------------------------------------------------- #


def http_client(read_timeout: float = READ_TIMEOUT_S) -> httpx.AsyncClient:
    """The one place an outbound client is built.

    A module-level factory rather than an inline constructor so the tests can
    swap in an ``httpx.MockTransport`` without patching httpx itself (and so
    every call site gets the same timeouts by construction).
    """
    return httpx.AsyncClient(
        timeout=httpx.Timeout(read_timeout, connect=CONNECT_TIMEOUT_S),
        # Followed by hand in _request so every hop's scheme is re-checked.
        follow_redirects=False,
    )


# Headers that carry the operator's provider credential in one shape or
# another. Lower-case; matched case-insensitively.
_CREDENTIAL_HEADERS = frozenset({
    "authorization", "proxy-authorization", "cookie",
    "x-api-key", "api-key", "x-goog-api-key", "openai-organization",
    "anthropic-api-key",
})


def _origin_of(url: str) -> tuple[str, str, int | None]:
    """(scheme, host, port) — the tuple a credential is scoped to."""
    u = httpx.URL(url)
    return (u.scheme, (u.host or "").lower(), u.port)


async def _read_capped(resp: httpx.Response) -> bytes:
    """Body bytes, refusing to buffer more than MAX_RESPONSE_BYTES."""
    chunks: list[bytes] = []
    total = 0
    async for chunk in resp.aiter_bytes():
        total += len(chunk)
        if total > MAX_RESPONSE_BYTES:
            raise ApiError(
                "The provider sent far too much data",
                f"The response passed {MAX_RESPONSE_BYTES // (1024 * 1024)}MB "
                "and was abandoned. That base URL is almost certainly not an "
                "LLM API endpoint.")
        chunks.append(chunk)
    return b"".join(chunks)


async def _request(client: httpx.AsyncClient, method: str, url: str, *,
                   headers: dict[str, str],
                   json_body: dict | None = None) -> tuple[int, bytes]:
    """One request, buffered (capped). Redirects as in :func:`_send_following`."""
    resp = await _send_following(client, method, url, headers=headers,
                                 json_body=json_body)
    try:
        return resp.status_code, await _read_capped(resp)
    finally:
        await resp.aclose()


async def _send_following(client: httpx.AsyncClient, method: str, url: str, *,
                          headers: dict[str, str],
                          json_body: dict | None = None) -> httpx.Response:
    """Send, following redirects by hand so each hop is re-validated.

    Returns the final response OPEN (streamed); the caller closes it.

    httpx's own `follow_redirects=True` would happily walk us anywhere the
    remote server points, including at a scheme we have deliberately refused to
    accept from the operator. Doing it here means a `Location:` header gets
    exactly the same scheme check the typed-in URL got.

    Credentials do NOT survive a cross-origin hop. A provider that answers
    `302 Location: https://someone-else/` would otherwise be handed the
    operator's API key, which is the classic redirect credential leak; on a
    change of scheme, host or port the Authorization (and any provider key)
    header is dropped and the next hop is made anonymously.
    """
    origin = _origin_of(url)
    hop_headers = dict(headers)
    for _ in range(MAX_REDIRECTS + 1):
        req = client.build_request(method, url, headers=hop_headers, json=json_body)
        resp = await client.send(req, stream=True)
        if resp.status_code not in (301, 302, 303, 307, 308):
            return resp
        location = resp.headers.get("location") or ""
        status = resp.status_code
        await resp.aclose()
        if not location:
            raise ApiError("The provider sent an empty redirect",
                           f"{url} answered {status} with no Location header.")
        url = normalize_base_url(str(httpx.URL(url).join(location)))
        # Every hop gets the SSRF guard the typed address got (link-local, metadata,
        # unspecified, multicast; `connections.allow_private_targets` lifts it). A
        # provider answering `302 Location: http://169.254.169.254/...` used to be
        # followed, and the Test result quoted the first bytes of what came back.
        from . import connections as _connections
        try:
            _connections.check_target(url)
        except _connections.ConnectionError_ as e:
            raise ApiError("The provider redirected to a blocked address", str(e)) from None
        if _origin_of(url) != origin:
            dropped = [h for h in list(hop_headers)
                       if h.lower() in _CREDENTIAL_HEADERS]
            for h in dropped:
                hop_headers.pop(h, None)
            if dropped:
                log.warning("dropping %s across a redirect to a new "
                            "origin (%s)", ", ".join(sorted(dropped)),
                            _origin_of(url))
            origin = _origin_of(url)
        # 303 (and, in practice, 302 on a POST) means "GET the other
        # thing" — but an API that answers a chat request that way is
        # misconfigured, and silently downgrading the method would hide
        # it. Re-issue as-is and let the next hop speak for itself.
    raise ApiError("The provider redirected too many times",
                   f"Gave up after {MAX_REDIRECTS} redirects from {url}.")


def _auth_headers(api_key: str) -> dict[str, str]:
    """Accept + bearer. No Content-Type: httpx sets it when (and only when) a
    JSON body is actually attached, and a GET carrying one confuses a few
    stricter self-hosted servers."""
    headers = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def _decode_json(body: bytes, url: str) -> Any:
    """Parse a SUCCESS body, or explain why the URL is not an API.

    Only for 2xx: on an error status the body is frequently an HTML page (a
    proxy's 502, a captive portal), and "the provider did not answer with JSON"
    would then bury the status code that actually says what went wrong. Error
    bodies go through _decode_json_lenient instead.
    """
    try:
        return json.loads(body.decode("utf-8", "replace"))
    except (ValueError, UnicodeDecodeError) as e:
        snippet = body[:200].decode("utf-8", "replace").strip()
        raise ApiError(
            "The provider did not answer with JSON",
            f"{url} returned something that is not an API response "
            f"({snippet!r}…). Check the base URL — a common mistake is "
            "pointing at the web UI instead of the /v1 endpoint.") from e


def _decode_json_lenient(body: bytes) -> Any:
    """An ERROR body as whatever it turns out to be: parsed JSON, or the text.

    Both shapes are handled by _provider_message, so an nginx 502 page still
    contributes its first line to the message the operator reads.
    """
    text = body.decode("utf-8", "replace").strip()
    try:
        return json.loads(text)
    except ValueError:
        return text[:300]


def _provider_message(payload: Any) -> str:
    """Pull the human-readable bit out of a provider's error body.

    Every OpenAI-compatible server nests it differently and some just send a
    string; the operator only ever wants the sentence.
    """
    if isinstance(payload, str):
        return payload.strip()[:300]
    if isinstance(payload, dict):
        err = payload.get("error", payload)
        if isinstance(err, str):
            return err.strip()[:300]
        if isinstance(err, dict):
            for k in ("message", "detail", "error", "msg"):
                v = err.get(k)
                if isinstance(v, str) and v.strip():
                    return v.strip()[:300]
        for k in ("message", "detail"):
            v = payload.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()[:300]
    return ""


def _status_error(status: int, payload: Any, *, url: str, model: str = "",
                  key_used: bool = False) -> ApiError:
    """Map an HTTP status onto something that names the actual fix."""
    said = _provider_message(payload)
    tail = f" The provider said: {said}" if said else ""
    if status in (401, 403):
        return ApiError(
            "The provider rejected the API key",
            ("No API key was sent — this provider requires one."
             if not key_used else
             "The key was not accepted. Check it has not been revoked, and "
             "that it belongs to the provider you selected.") + tail)
    if status == 404:
        return ApiError(
            "The provider could not find that model or endpoint",
            (f"{url} returned 404. Either the model {model!r} does not exist "
             "on this account, or the base URL is missing its version prefix "
             "(most OpenAI-compatible servers want one ending in /v1)." + tail)
            if model else
            f"{url} returned 404 — check the base URL.{tail}")
    if status == 422:
        return ApiError("The provider rejected the request",
                        f"{url} returned 422.{tail}")
    if status == 429:
        return ApiError("The provider is rate-limiting this key",
                        f"Too many requests, or the account is out of credit."
                        f"{tail}")
    if 500 <= status < 600:
        return ApiError("The provider had an error",
                        f"{url} returned {status}.{tail} This is the "
                        "provider's end — try again in a moment.")
    return ApiError(f"The provider returned HTTP {status}", f"{url}{tail}")


def _transport_error(e: Exception, url: str, *, local: bool,
                     read_timeout: float = READ_TIMEOUT_S) -> ApiError:
    """Connection-level failures, phrased for the two very different causes."""
    if isinstance(e, httpx.ConnectTimeout | httpx.ConnectError):
        return ApiError(
            "Could not reach the provider",
            (f"Nothing answered at {url}. Is the server running, and is the "
             "port right? LM Studio needs its local server started, and "
             "Ollama must be running."
             if local else
             f"Could not connect to {url}. Check the address, and that this "
             "machine has network access to it.") + f" ({type(e).__name__})")
    if isinstance(e, httpx.ReadTimeout | httpx.WriteTimeout | httpx.PoolTimeout):
        return ApiError(
            "The provider took too long to answer",
            # The number MUST be the one that actually expired: the probe path
            # runs on PROBE_READ_TIMEOUT_S, and quoting 120s for a 25s timeout
            # sends the operator looking for a slow model that is not there.
            f"No reply from {url} within {int(read_timeout)}s. A local model "
            "loading for the first time can exceed this — try again once it is "
            "warm.")
    return ApiError("The connection to the provider failed",
                    f"{type(e).__name__} talking to {url}: {str(e)[:200]}")


# --------------------------------------------------------------------------- #
# Anthropic (official SDK, not raw HTTP)
# --------------------------------------------------------------------------- #


def anthropic_client(api_key: str, base_url: str = ""):
    """Build an AsyncAnthropic. Factory-shaped for the same reason http_client
    is: the tests replace this wholesale.

    Imported lazily — the SDK pulls in a good deal of pydantic machinery, and
    an install that never touches Anthropic should not pay for it at boot.
    """
    try:
        from anthropic import AsyncAnthropic
    except ImportError as e:  # pragma: no cover - dependency is declared
        raise ApiError(
            "The Anthropic SDK is not installed",
            "Run `uv sync` in backend/ — the `anthropic` package is a "
            "declared dependency of this app.") from e
    kwargs: dict[str, Any] = {
        "api_key": api_key or None,
        "timeout": httpx.Timeout(READ_TIMEOUT_S, connect=CONNECT_TIMEOUT_S),
        "max_retries": 1,
    }
    default = PROVIDER_BY_ID["anthropic"].base_url
    if base_url and base_url.rstrip("/") != default:
        kwargs["base_url"] = base_url
    return AsyncAnthropic(**kwargs)


def _anthropic_error(e: Exception) -> ApiError:
    """Map the SDK's exception tree without importing it at module scope."""
    status = getattr(e, "status_code", None)
    name = type(e).__name__
    if name in ("APIConnectionError", "APITimeoutError"):
        return ApiError(
            "Could not reach Anthropic" if name == "APIConnectionError"
            else "Anthropic took too long to answer",
            f"{name}: {str(e)[:200]}")
    if status in (401, 403):
        return ApiError("Anthropic rejected the API key",
                        "Check the key at console.anthropic.com — it must "
                        "start with `sk-ant-` and still be active.")
    if status == 404:
        return ApiError("Anthropic could not find that model",
                        f"{str(e)[:200]} — check the model id.")
    if status == 429:
        return ApiError("Anthropic is rate-limiting this key",
                        f"{str(e)[:200]}")
    if isinstance(status, int) and status >= 500:
        return ApiError("Anthropic had an error", f"HTTP {status}: {str(e)[:200]}")
    return ApiError("The Anthropic request failed", f"{name}: {str(e)[:200]}")


def _openai_message_text(message: dict) -> str:
    """The visible text of one chat-completions message.

    `content` is USUALLY a string, but a growing number of OpenAI-compatible
    servers answer with the multimodal block list instead. `.strip()` on a list
    raises AttributeError, which escapes the ApiError ladder entirely and
    reaches the family as "Something went wrong." — no provider, no URL, no
    hint that the reply actually arrived intact.
    """
    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = [b.get("text") or "" for b in content
                 if isinstance(b, dict) and b.get("type") in (None, "text")]
        return "".join(parts).strip()
    return ""


def _anthropic_text(resp: Any) -> str:
    """Visible text from a Messages response, refusal checked FIRST.

    ``stop_reason == "refusal"`` means the model declined for safety reasons
    and the content blocks may be empty or partial. Reading them first and
    finding "" would surface as a mystery blank bubble; the operator (and the
    family member watching the screen) is owed the actual reason.
    """
    if getattr(resp, "stop_reason", None) == "refusal":
        raise ApiError(
            "The model declined to answer that",
            "Anthropic stopped the response for safety reasons "
            "(stop_reason: refusal). Nothing was wrong with your setup — "
            "rephrase and try again.")
    parts: list[str] = []
    for block in getattr(resp, "content", None) or []:
        if getattr(block, "type", None) == "text":
            parts.append(getattr(block, "text", "") or "")
    return "".join(parts).strip()


# --------------------------------------------------------------------------- #
# The bot's API configuration, resolved
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Resolved:
    """A bot's `api` block after defaults, presets and the environment."""

    provider: Provider
    base_url: str
    model: str
    api_key: str
    system_prompt: str
    max_history_chars: int


def resolve_key(api: dict) -> str:
    """The key to use: the named environment variable if it is set, else the
    stored one. The env var wins deliberately — an operator who set both has
    said which one they trust, and it is not the one on disk.

    3.0: a provider entry that names a ``connection`` takes its key from that connection
    (secret store or the connection's own environment variable) when it has one; the inline
    ``api_key`` / ``api_key_env`` remain the fallback for an entry that has no connection or
    whose connection holds no key."""
    if api.get("connection"):
        from . import connections
        from_conn = connections.credential_for_block(api)
        if from_conn:
            return from_conn
    env_name = str(api.get("api_key_env") or "").strip()
    if env_name:
        from_env = os.environ.get(env_name, "")
        if from_env.strip():
            return from_env.strip()
    return str(api.get("api_key") or "").strip()


def resolve(bot: config.Bot) -> Resolved:
    """Turn a bot's stored `api` dict into everything a call needs.

    Fails loudly here rather than half-way through a request: a missing model
    or a required-but-absent key is a configuration mistake, and the message
    should say so instead of arriving as a provider 400.
    """
    return resolve_api(bot.api or {}, bot.name, bot.id)


def resolve_api(api: dict, name: str, bot_id: str = "") -> Resolved:
    """`resolve` for one provider block that need not belong to a bot.

    A companion bot carries a CHAIN of these (cloud first, local second); each
    link resolves by exactly the rules a single "Connect an AI" bot uses.
    """
    if api.get("connection"):
        # 3.0: a Connection supplies provider, address and key; the entry keeps the model and
        # any per-character extras (connections.effective_block).
        from . import connections
        api = connections.effective_block(api)
    # The two attributes the error messages below read.
    bot = SimpleNamespace(name=name, id=bot_id or name)
    provider = PROVIDER_BY_ID.get(str(api.get("provider") or "").strip())
    if provider is None:
        raise ApiError(
            "This bot's provider is not recognised",
            f"config.yaml says provider {api.get('provider')!r} for "
            f"{bot.id!r}. Known providers: "
            f"{', '.join(sorted(PROVIDER_BY_ID))}.")
    base_url = normalize_base_url(str(api.get("base_url") or "") or provider.base_url)
    model = str(api.get("model") or "").strip() or (
        provider.models[0] if provider.models else "")
    if not model:
        raise ApiError("This bot has no model configured",
                       f"Set a model for {bot.name} in the Connect an AI panel.")
    api_key = resolve_key(api)
    if provider.key_required and not api_key:
        env_hint = (f" Set {api['api_key_env']} in the environment, or "
                    if api.get("api_key_env") else " ")
        raise ApiError(
            f"No API key configured for {provider.label}",
            f"{provider.label} requires a key.{env_hint}"
            "re-open the Connect an AI panel and paste one.")
    try:
        budget = int(api.get("max_history_chars") or DEFAULT_HISTORY_CHARS)
    except (TypeError, ValueError):
        budget = DEFAULT_HISTORY_CHARS
    # A stored prompt gets the same two placeholders a direct bot's persona
    # does: the Bots tab offers one set of presets to both kinds (2.3.6), and
    # "{name}" reaching the model literally was what a legacy bot got before.
    system = personas.fill(str(api.get("system_prompt") or "").strip(), bot.name) or \
        DEFAULT_SYSTEM_PROMPT.format(name=bot.name)
    return Resolved(provider=provider, base_url=base_url, model=model,
                    api_key=api_key, system_prompt=system,
                    max_history_chars=max(500, budget))


# --------------------------------------------------------------------------- #
# History
# --------------------------------------------------------------------------- #


#: What an interrupted voice reply ends with in the NEXT prompt: the heard
#: prefix and a dash, the way a transcript marks speech that was cut off
#: (OpenAI's `conversation.item.truncate`, done at prompt-build time here).
HEARD_CUT_MARK = " —"


def _heard_upto(meta: Any) -> int | None:
    """`metadata.voice_heard.upto` when it is a usable source offset."""
    heard = meta.get("voice_heard") if isinstance(meta, dict) else None
    if not isinstance(heard, dict):
        return None
    upto = heard.get("upto")
    if isinstance(upto, bool) or not isinstance(upto, int) or upto < 0:
        return None
    return upto


def heard_content(content: str, upto: int) -> str:
    """`content` cut back to what was heard in voice chat, plus the cut mark.

    `upto` is a source offset at a word boundary (C-§B.3 `source_offset`); an
    offset past the end means the whole reply was heard and nothing changes.
    """
    if upto >= len(content):
        return content
    return (content[:upto].rstrip() + HEARD_CUT_MARK).lstrip()


def build_history(messages: list, budget: int = DEFAULT_HISTORY_CHARS, *,
                  truncate_heard: bool = False) -> list[dict]:
    """Map a thread's stored messages onto provider chat turns.

    ``truncate_heard`` (voice chat, C-§B.2): when the NEWEST assistant row
    carries ``metadata.voice_heard.upto`` — the reply was interrupted and only
    that much of it was heard — its content is cut to ``content[:upto] + " —"``
    for this prompt. Only the newest assistant row, so the model is told once,
    on the turn right after the interruption; the stored row is never touched.

    Rules, in the order they matter:

    * ``system`` rows, "sub" rows (the collapsed working-output the agent
      path emits between tool calls) and rows the operator has HIDDEN are not
      conversation — they are dropped. Sending a sub row would teach an API
      bot to imitate an agent it is not; sending a hidden one would make the
      hide button a lie — the row stays in the chat by design, so if it also
      stayed in the request the button would change nothing at all.
    * Newest wins. The window is filled from the END until `budget` characters
      are used, because the last thing said is always the most relevant and a
      long thread must not push the actual question out of context.
    * Consecutive same-role turns are merged. Anthropic rejects them outright,
      and every other provider treats them as an oddity — merging is both the
      portable answer and the honest one (they really were one person talking).
    * A leading assistant turn is dropped: a conversation the model is asked to
      continue has to start with the user.
    """
    kept: list[dict] = []
    used = 0
    newest_assistant_seen = False
    for m in reversed(messages or []):
        role = getattr(m, "role", None) or ""
        if role not in ("user", "assistant"):
            continue
        meta = getattr(m, "metadata", None) or {}
        if isinstance(meta, dict) and (meta.get("sub") or meta.get("hidden")):
            continue
        content = (getattr(m, "content", None) or "").strip()
        if not content:
            continue
        if role == "assistant" and not newest_assistant_seen:
            newest_assistant_seen = True
            upto = _heard_upto(meta) if truncate_heard else None
            if upto is not None:
                content = heard_content(content, upto)
        if used and used + len(content) > budget:
            break
        used += len(content)
        kept.append({"role": role, "content": content})
        if used >= budget:
            break
    kept.reverse()

    merged: list[dict] = []
    for turn in kept:
        if merged and merged[-1]["role"] == turn["role"]:
            merged[-1]["content"] += "\n\n" + turn["content"]
        else:
            merged.append(dict(turn))
    while merged and merged[0]["role"] != "user":
        merged.pop(0)
    return merged


def _ensure_trailing_user(history: list[dict], text: str) -> list[dict]:
    """Guarantee the model is answering THIS message.

    The caller persists the user's message before dispatching the turn, so it
    is normally already the last row of `history` — but the retry path and any
    future caller may not have, and a request whose last turn is the assistant
    asks the model to talk to itself.
    """
    text = (text or "").strip()
    if not text:
        return history
    if history and history[-1]["role"] == "user" and history[-1]["content"] == text:
        return history
    if history and history[-1]["role"] == "user":
        # The stored row is the RAW message; `text` is what the agent should
        # read (_compose_agent_text may have prepended a quote or appended a
        # feedback note). The composed text already contains the message, so
        # it REPLACES the row — appending sent the family's message to the
        # provider twice whenever a quote or a pending vote was attached
        # (review 2026-09-24).
        return [*history[:-1], {"role": "user", "content": text}]
    return [*history, {"role": "user", "content": text}]


def append_user_turn(history: list[dict], text: str) -> list[dict]:
    """`history` plus `text` as the newest user turn, for a prompt built
    BEFORE the user's row exists (a speculation, C-§B.1).

    Unlike :func:`_ensure_trailing_user` this never replaces anything: the
    stored rows are all older than `text`, so a trailing user turn there is an
    earlier message (one that got no answer) and `text` joins it as a new
    paragraph, the same way :func:`build_history` merges consecutive turns.
    """
    text = (text or "").strip()
    if not text:
        return history
    if history and history[-1]["role"] == "user":
        last = history[-1]
        return [*history[:-1], {**last, "content": f"{last['content']}\n\n{text}"}]
    return [*history, {"role": "user", "content": text}]


# --------------------------------------------------------------------------- #
# The call
# --------------------------------------------------------------------------- #


@dataclass
class Reply:
    text: str
    model: str = ""
    provider: str = ""
    tokens: int | None = None


async def complete(cfg: Resolved, history: list[dict]) -> Reply:
    """One completion. Raises ApiError for everything an operator can fix."""
    if cfg.provider.kind == "anthropic":
        return await _complete_anthropic(cfg, history)
    return await _complete_openai(cfg, history)


async def _complete_openai(cfg: Resolved, history: list[dict]) -> Reply:
    url = _join(cfg.base_url, "chat/completions")
    body = {
        "model": cfg.model,
        "messages": [{"role": "system", "content": cfg.system_prompt}, *history],
        "stream": False,
    }
    body.update(cfg.provider.extra_body or {})
    async with http_client() as client:
        try:
            status, raw = await _request(client, "POST", url,
                                         headers=_auth_headers(cfg.api_key),
                                         json_body=body)
        except ApiError:
            raise
        except httpx.HTTPError as e:
            raise _transport_error(e, url, local=cfg.provider.local) from e
    if status >= 400:
        raise _status_error(status, _decode_json_lenient(raw), url=url,
                            model=cfg.model, key_used=bool(cfg.api_key))
    res = _whole_reply(_decode_json(raw, url), url, status, cfg)
    return _require_text(res, cfg)


def _whole_reply(payload: Any, url: str, status: int, cfg: Resolved) -> StreamResult:
    """A non-streamed chat-completions body, read. Empty text is NOT an error
    here — the caller decides (the advisor chain falls through on it)."""
    choices = payload.get("choices") if isinstance(payload, dict) else None
    if not isinstance(choices, list) or not choices:
        raise ApiError(
            "The provider returned no reply",
            f"{url} answered {status} with no `choices`. "
            f"{_provider_message(payload) or 'Response: ' + str(payload)[:200]}")
    message = (choices[0] or {}).get("message") or {}
    return StreamResult(
        text=_openai_message_text(message),
        model=str(payload.get("model") or cfg.model),
        tokens=_total_tokens(payload.get("usage")),
        finish_reason=str((choices[0] or {}).get("finish_reason") or ""),
        streamed=False,
        tool_calls=_message_tool_calls(message) if isinstance(message, dict) else [])


def _total_tokens(usage: Any) -> int | None:
    if isinstance(usage, dict) and isinstance(usage.get("total_tokens"), int):
        return usage["total_tokens"]
    return None


def _require_text(res: StreamResult, cfg: Resolved) -> Reply:
    """A Reply, or the error an empty one deserves."""
    text = res.text.strip()
    if not text:
        # A finish_reason of "length" with empty content is the classic
        # symptom of a reasoning model whose whole budget went to thinking.
        raise ApiError(
            "The model replied with nothing",
            f"The response was empty (finish_reason: {res.finish_reason or 'unknown'}). "
            "If this is a reasoning model, it may have spent its whole output "
            "budget thinking — try a different model.")
    return Reply(text=text, model=res.model or cfg.model,
                 provider=cfg.provider.id, tokens=res.tokens)


# --------------------------------------------------------------------------- #
# Real streaming (OpenAI-compatible) — shared with the advisor
# --------------------------------------------------------------------------- #


class StreamFailed(Exception):
    """A provider failed. `started` says whether the provider had produced any
    raw text (reasoning included) — informational only: whether a turn may
    still fall back is decided by what is ON SCREEN (`StreamEmitter.started`)."""

    def __init__(self, error: ApiError, started: bool) -> None:
        super().__init__(error.message)
        self.error = error
        self.started = started


@dataclass
class StreamResult:
    text: str
    model: str = ""
    tokens: int | None = None
    finish_reason: str = ""
    streamed: bool = True       # False: the server answered with one JSON body
    #: Tool calls the model made, each ``{id, name, arguments}`` with the
    #: arguments as the JSON text the model wrote (advisor tool loop,
    #: 2026-10-13). Empty for a plain reply.
    tool_calls: list = field(default_factory=list)


# Adapted from ChatForge llm/client.py (MIT, LaserLloyd), itself from CrucibleForge.
def merge_tool_call_deltas(raw_deltas: list[dict]) -> list[dict]:
    """The streamed ``delta.tool_calls`` fragments as whole calls.

    MiniMax sends each call whole in one fragment; llama.cpp-style servers
    (StudioForge) stream the arguments a few characters at a time under one
    ``index``; some servers send no index at all, where a new ``id`` starts a
    new call and an id-less fragment continues the one before it."""
    calls: dict[int, dict] = {}
    last: int | None = None
    for d in raw_deltas:
        if not isinstance(d, dict):
            continue
        idx = d.get("index")
        if not isinstance(idx, int) or isinstance(idx, bool):
            new_id = d.get("id")
            if last is None or (new_id and calls[last]["id"] and new_id != calls[last]["id"]):
                idx = max(calls) + 1 if calls else 0
            else:
                idx = last
        last = idx
        slot = calls.setdefault(idx, {"id": None, "name": "", "arguments": ""})
        if d.get("id"):
            slot["id"] = d["id"]
        fn = d.get("function") or {}
        name = fn.get("name")
        if name and name != slot["name"]:      # some servers repeat the full name
            slot["name"] += name
        if fn.get("arguments"):
            slot["arguments"] += fn["arguments"]
    return [{"id": calls[k]["id"] or f"call_{i}", "name": calls[k]["name"],
             "arguments": calls[k]["arguments"]}
            for i, k in enumerate(sorted(calls))]


def _message_tool_calls(message: dict) -> list[dict]:
    """``message.tool_calls`` of a non-streamed reply in the same shape."""
    out = []
    for i, c in enumerate(message.get("tool_calls") or []):
        if not isinstance(c, dict):
            continue
        fn = c.get("function") or {}
        name = str(fn.get("name") or "")
        if not name:
            continue
        args = fn.get("arguments")
        out.append({"id": str(c.get("id") or f"call_{i}"), "name": name,
                    "arguments": args if isinstance(args, str) else json.dumps(args or {})})
    return out


#: Statuses worth one more try: rate limit and the gateway/overload family.
#: Everything else (401, 404, 500 …) is a configuration or provider fault
#: that a second identical request will only repeat.
RETRY_STATUSES = frozenset({429, 502, 503, 504})
RETRY_DEFAULT_DELAY_S = 1.0
#: A Retry-After longer than this is honoured by NOT retrying: the turn fails
#: now with the provider's message instead of a family member watching a
#: spinner for a minute.
RETRY_MAX_DELAY_S = 5.0


def _retry_delay(headers: httpx.Headers) -> float | None:
    """Seconds to wait before the one retry, or None when the provider asked
    for longer than we are willing to keep a turn waiting."""
    value = (headers.get("retry-after") or "").strip()
    if not value:
        return RETRY_DEFAULT_DELAY_S
    try:
        delay = float(value)
    except ValueError:
        try:
            delay = parsedate_to_datetime(value).timestamp() - time.time()
        except (TypeError, ValueError, IndexError, OverflowError):
            return RETRY_DEFAULT_DELAY_S
    delay = max(0.0, delay)
    return delay if delay <= RETRY_MAX_DELAY_S else None


def _delta_text(delta: Any) -> str:
    content = delta.get("content") if isinstance(delta, dict) else None
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(b.get("text") or "" for b in content
                       if isinstance(b, dict) and b.get("type") in (None, "text"))
    return ""


async def stream_chat(cfg: Resolved, messages: list[dict],
                      on_text: Callable[[str], Awaitable[None]] | None = None, *,
                      max_tokens: int | None = None, extra_body: dict | None = None,
                      read_timeout: float = READ_TIMEOUT_S,
                      tools: list[dict] | None = None) -> StreamResult:
    """Stream one OpenAI-compatible chat completion.

    ``messages`` is the full list (system first). ``on_text`` gets the
    CUMULATIVE raw text after every delta. Failures raise :class:`StreamFailed`
    wrapping the same :class:`ApiError` a whole-reply call would have raised.

    One retry, before any text: a 429/502/503/504 answer is retried once after
    ``Retry-After`` (or ``RETRY_DEFAULT_DELAY_S``), unless the provider asked
    for longer than ``RETRY_MAX_DELAY_S``. Once a delta has been passed on
    nothing is retried — the bubble would otherwise splice two replies.
    Redirects are followed by hand with the credential rules of `_request`.
    """
    url = _join(cfg.base_url, "chat/completions")
    body: dict[str, Any] = {"model": cfg.model, "messages": messages, "stream": True}
    if max_tokens:
        body["max_tokens"] = int(max_tokens)
    if tools:
        # OpenAI tool calling; the model decides (`auto`). The calls come back
        # as `delta.tool_calls` fragments, merged in `merge_tool_call_deltas`.
        body["tools"] = list(tools)
        body["tool_choice"] = "auto"
    body.update(cfg.provider.extra_body or {})
    if isinstance(extra_body, dict):
        body.update(extra_body)
    raw = ""
    for attempt in (0, 1):
        retry_in: float | None = None
        try:
            async with http_client(read_timeout) as client:
                resp = await _send_following(client, "POST", url,
                                             headers=_auth_headers(cfg.api_key),
                                             json_body=body)
                try:
                    if resp.status_code >= 400:
                        payload = _decode_json_lenient(await _read_capped(resp))
                        if attempt == 0 and resp.status_code in RETRY_STATUSES:
                            retry_in = _retry_delay(resp.headers)
                        if retry_in is None:
                            raise StreamFailed(_status_error(
                                resp.status_code, payload, url=url, model=cfg.model,
                                key_used=bool(cfg.api_key)), started=False)
                        log.info("provider %s answered %s; retrying once in %.1fs",
                                 cfg.provider.id, resp.status_code, retry_in)
                    elif "json" in (resp.headers.get("content-type") or "").lower():
                        # The server ignored `stream: true`. Still a reply.
                        return _whole_reply(_decode_json(await _read_capped(resp), url),
                                            url, resp.status_code, cfg)
                    else:
                        model, tokens, reason, seen = cfg.model, None, "", 0
                        tool_deltas: list[dict] = []
                        async for line in resp.aiter_lines():
                            seen += len(line) + 1
                            if seen > MAX_RESPONSE_BYTES:
                                raise ApiError(
                                    "The provider sent far too much data",
                                    f"The stream passed {MAX_RESPONSE_BYTES // (1024 * 1024)}MB "
                                    "and was abandoned.")
                            if not line.startswith("data:"):
                                continue
                            data = line[5:].strip()
                            if data == "[DONE]":
                                break
                            try:
                                obj = json.loads(data)
                            except json.JSONDecodeError:
                                continue
                            if not isinstance(obj, dict):
                                continue
                            if obj.get("error"):
                                raise StreamFailed(ApiError(
                                    "The provider reported an error mid-reply",
                                    str(obj["error"])[:300]), started=bool(raw))
                            model = str(obj.get("model") or model)
                            tokens = _total_tokens(obj.get("usage")) or tokens
                            for ch in obj.get("choices") or []:
                                if not isinstance(ch, dict):
                                    continue
                                reason = str(ch.get("finish_reason") or reason)
                                delta = ch.get("delta")
                                if isinstance(delta, dict) and isinstance(
                                        delta.get("tool_calls"), list):
                                    tool_deltas.extend(delta["tool_calls"])
                                piece = _delta_text(delta)
                                if piece:
                                    raw += piece
                                    if on_text is not None:
                                        await on_text(raw)
                        return StreamResult(text=raw, model=model, tokens=tokens,
                                            finish_reason=reason,
                                            tool_calls=merge_tool_call_deltas(tool_deltas))
                finally:
                    await resp.aclose()
        except StreamFailed:
            raise
        except ApiError as e:
            raise StreamFailed(e, started=bool(raw)) from e
        except httpx.HTTPError as e:
            raise StreamFailed(_transport_error(
                e, url, local=cfg.provider.local, read_timeout=read_timeout),
                started=bool(raw)) from e
        await asyncio.sleep(retry_in or 0)
    raise AssertionError("unreachable")              # pragma: no cover


async def stream_openai(cfg: Resolved, entry: dict, messages: list[dict],
                        on_text: Callable[[str], Awaitable[None]]) -> str:
    """:func:`stream_chat` with the knobs read from a provider block (an
    advisor chain entry or a bot's `api`); returns the raw text."""
    res = await stream_chat(
        cfg, messages, on_text, max_tokens=_int_or_none(entry.get("max_tokens")),
        extra_body=entry.get("extra_body") if isinstance(entry.get("extra_body"), dict) else None,
        read_timeout=float(entry.get("read_timeout_s") or 60))
    return res.text


async def stream_round(cfg: Resolved, entry: dict, messages: list[dict],
                       on_text: Callable[[str], Awaitable[None]], *,
                       tools: list[dict] | None = None) -> StreamResult:
    """:func:`stream_openai` for one round of a tool loop: the same knobs, an
    optional ``tools`` list, and the whole :class:`StreamResult` back (the
    advisor needs ``tool_calls``, not just the text)."""
    return await stream_chat(
        cfg, messages, on_text, max_tokens=_int_or_none(entry.get("max_tokens")),
        extra_body=entry.get("extra_body") if isinstance(entry.get("extra_body"), dict) else None,
        read_timeout=float(entry.get("read_timeout_s") or 60), tools=tools)


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value) if value else None
    except (TypeError, ValueError):
        return None


#: How often a live bubble is repainted; deltas in between are coalesced.
STREAM_EMIT_INTERVAL_S = 0.05


class StreamEmitter:
    """Turns cumulative raw text into provisional-bubble frames, throttled.

    ``hooks`` must offer broadcast / open_stream / close_stream /
    sanitize_delta (main.py's provisional-bubble registry and delta sanitizer);
    subclasses may resolve them lazily (:meth:`_stream_hooks`) and decide what
    part of the raw text is visible (:meth:`_visible`).
    """

    prefix = "api"

    def __init__(self, thread_id: str, bot_id: str, hooks: Any = None) -> None:
        self.thread_id, self.bot_id = thread_id, bot_id
        self._hooks_obj = hooks
        self.prov = f"run:{self.prefix}-{uuid.uuid4().hex[:12]}"
        self.started = False
        self.sent = ""
        self.pending: str | None = None
        self.last = 0.0
        self.replace_next = False

    def _stream_hooks(self) -> Any:
        return self._hooks_obj

    def _visible(self, raw: str) -> str:
        return raw

    def _interval(self) -> float:
        return STREAM_EMIT_INTERVAL_S

    async def push(self, raw: str, *, force: bool = False) -> None:
        self.pending = raw
        now = time.monotonic()
        if force or now - self.last >= self._interval():
            self.last = now
            await self.flush()

    async def flush(self) -> None:
        if self.pending is None:
            return
        raw, self.pending = self.pending, None
        h = self._stream_hooks()
        clean = h.sanitize_delta(self._visible(raw))
        if not clean.strip() or clean == self.sent:
            return
        extra: dict = {}
        if self.replace_next or not clean.startswith(self.sent):
            chunk, extra = clean, {"replace": True}
            self.replace_next = False
        else:
            chunk = clean[len(self.sent):]
        self.sent = clean
        if not self.started:
            self.started = True
            h.open_stream(self.thread_id, self.prov)
            await h.broadcast({"type": "stream_start", "thread_id": self.thread_id,
                               "bot_id": self.bot_id, "message_id": self.prov})
        await h.broadcast({"type": "stream_chunk", "thread_id": self.thread_id,
                           "bot_id": self.bot_id, "message_id": self.prov,
                           "text": chunk, **extra})

    def restart(self) -> None:
        """A fallback provider is about to write over what is on screen."""
        self.replace_next = True
        self.pending = None

    async def retire(self) -> None:
        """Close the bubble when no row is coming (the turn failed)."""
        h = self._stream_hooks()
        if self.started and h.close_stream(self.thread_id, self.prov):
            await h.broadcast({"type": "stream_done", "thread_id": self.thread_id,
                               "bot_id": self.bot_id, "message_id": self.prov,
                               "message": None, "provisional_id": self.prov})


def _can_stream_live(hooks: Any) -> bool:
    return all(getattr(hooks, n, None) is not None
               for n in ("open_stream", "close_stream", "sanitize_delta"))


async def complete_streaming(cfg: Resolved, history: list[dict], api: dict,
                             on_text: Callable[[str], Awaitable[None]] | None
                             ) -> tuple[Reply, bool]:
    """A turn's call: streamed for OpenAI-shape providers, whole for Anthropic.
    Returns the reply and whether it actually arrived as a stream."""
    if cfg.provider.kind == "anthropic":
        return await _complete_anthropic(cfg, history), False
    try:
        res = await stream_chat(
            cfg, [{"role": "system", "content": cfg.system_prompt}, *history], on_text,
            max_tokens=_int_or_none(api.get("max_tokens")),
            extra_body=api.get("extra_body") if isinstance(api.get("extra_body"), dict) else None,
            read_timeout=float(api.get("read_timeout_s") or READ_TIMEOUT_S))
    except StreamFailed as e:
        raise e.error from e
    return _require_text(res, cfg), res.streamed


async def _complete_anthropic(cfg: Resolved, history: list[dict]) -> Reply:
    client = anthropic_client(cfg.api_key, cfg.base_url)
    try:
        resp = await client.messages.create(
            model=cfg.model,
            max_tokens=DEFAULT_MAX_TOKENS,
            system=cfg.system_prompt,
            messages=history,
        )
    except ApiError:
        raise
    except Exception as e:
        raise _anthropic_error(e) from e
    finally:
        with contextlib.suppress(Exception):
            await client.close()
    text = _anthropic_text(resp)
    if not text:
        raise ApiError(
            "The model replied with nothing",
            f"Anthropic returned no text blocks "
            f"(stop_reason: {getattr(resp, 'stop_reason', None)}).")
    usage = getattr(resp, "usage", None)
    tokens = None
    if usage is not None:
        parts = [getattr(usage, "input_tokens", None), getattr(usage, "output_tokens", None)]
        if all(isinstance(p, int) for p in parts):
            tokens = sum(p for p in parts if isinstance(p, int))
    return Reply(text=text, model=str(getattr(resp, "model", "") or cfg.model),
                 provider=cfg.provider.id, tokens=tokens)


# --------------------------------------------------------------------------- #
# The probe (Test connection)
# --------------------------------------------------------------------------- #


async def probe(provider_id: str, *, base_url: str = "", api_key: str = "",
                model: str = "") -> dict:
    """Ask a provider whether it is there, and what it can run.

    Returns ``{ok: True, models: [...]}`` or ``{ok: False, error: "..."}`` —
    never raises for a provider-side problem, because "it did not work and here
    is why" is the whole point of the button that calls this.
    """
    provider = PROVIDER_BY_ID.get((provider_id or "").strip())
    if provider is None:
        return {"ok": False,
                "error": f"Unknown provider {provider_id!r}. "
                         f"Choose one of: {', '.join(sorted(PROVIDER_BY_ID))}."}
    try:
        url = normalize_base_url(base_url or provider.base_url)
    except ApiError as e:
        return {"ok": False, "error": f"{e.message} — {e.detail}".strip(" —")}
    key = api_key.strip() if api_key else ""
    if provider.key_required and not key:
        return {"ok": False,
                "error": f"{provider.label} needs an API key. "
                         f"Paste one (or set {provider.key_env or 'the key'} "
                         "in the environment and use that option)."}
    try:
        if provider.kind == "anthropic":
            models = await _probe_anthropic(key, url, model)
        else:
            models = await _probe_openai(url, key, model, local=provider.local)
    except ApiError as e:
        return {"ok": False, "error": f"{e.message} — {e.detail}".strip(" —")}
    except Exception as e:
        log.warning("llm probe failed for %s: %r", provider_id, e)
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}
    return {"ok": True, "models": models}


async def _probe_openai(url: str, key: str, model: str, *, local: bool) -> list[str]:
    """GET /models, falling back to a one-token completion.

    Plenty of OpenAI-compatible servers implement chat/completions and nothing
    else. Reporting "not reachable" because the optional catalogue endpoint is
    missing would be wrong, so the fallback proves the endpoint that actually
    matters and returns an empty list (the UI then offers a free-text field).
    """
    models_url = _join(url, "models")
    async with http_client(PROBE_READ_TIMEOUT_S) as client:
        try:
            status, raw = await _request(client, "GET", models_url,
                                         headers=_auth_headers(key))
        except ApiError:
            raise
        except httpx.HTTPError as e:
            raise _transport_error(e, models_url, local=local,
                                   read_timeout=PROBE_READ_TIMEOUT_S) from e

        if status < 400:
            payload = _decode_json(raw, models_url)
            data = payload.get("data") if isinstance(payload, dict) else payload
            ids: list[str] = []
            for item in data if isinstance(data, list) else []:
                mid = item.get("id") if isinstance(item, dict) else item
                if isinstance(mid, str) and mid.strip():
                    ids.append(mid.strip())
            return sorted(set(ids))

        # 404/405: no catalogue here. Prove the real endpoint instead.
        if status in (404, 405):
            probe_model = model.strip()
            if not probe_model:
                raise ApiError(
                    "This server has no model list",
                    f"{models_url} returned {status}, so the models cannot be "
                    "discovered. Type the model name in and test again — the "
                    "chat endpoint will be checked directly.")
            chat_url = _join(url, "chat/completions")
            try:
                cstatus, craw = await _request(
                    client, "POST", chat_url, headers=_auth_headers(key),
                    json_body={"model": probe_model, "max_tokens": 1,
                               "messages": [{"role": "user", "content": "Hi"}]})
            except httpx.HTTPError as e:
                raise _transport_error(e, chat_url, local=local,
                                       read_timeout=PROBE_READ_TIMEOUT_S) from e
            if cstatus >= 400:
                raise _status_error(cstatus, _decode_json_lenient(craw),
                                    url=chat_url, model=probe_model,
                                    key_used=bool(key))
            _decode_json(craw, chat_url)   # a 200 that is not JSON is not an API
            return []
        raise _status_error(status, _decode_json_lenient(raw),
                            url=models_url, key_used=bool(key))


async def _probe_anthropic(key: str, url: str, model: str) -> list[str]:
    """`client.models.list()` — the SDK's own catalogue call."""
    client = anthropic_client(key, url)
    try:
        page = await client.models.list()
        ids = [str(m.id) for m in (getattr(page, "data", None) or [])
               if getattr(m, "id", None)]
        if ids:
            return ids
        # An account with no catalogue access still needs a yes/no answer.
        resp = await client.messages.create(
            model=(model.strip() or PROVIDER_BY_ID["anthropic"].models[0]),
            max_tokens=1, messages=[{"role": "user", "content": "Hi"}])
        _ = getattr(resp, "id", None)
        return []
    except ApiError:
        raise
    except Exception as e:
        raise _anthropic_error(e) from e
    finally:
        with contextlib.suppress(Exception):
            await client.close()


# --------------------------------------------------------------------------- #
# Turn orchestration
# --------------------------------------------------------------------------- #


@dataclass
class Hooks:
    """The slice of main.py this module needs, handed over explicitly.

    main imports llm_api (to route turns), so llm_api cannot import main. A
    deps object rather than a late import inside the function: it makes the
    coupling a short, readable list, and it lets the tests drive a whole turn
    with fakes and assert exactly what was persisted and broadcast.
    """

    # (thread_id, limit) -> (messages, has_more) — db.list_messages
    list_messages: Callable[[str, int], Awaitable[tuple[list, bool]]]
    # main._deliver_assistant_text — the shared persist/sanitize/dedup funnel
    deliver: Callable[..., Awaitable[Any]]
    # db.update_thread_status
    set_status: Callable[[str, str], Awaitable[None]]
    # manager.broadcast
    broadcast: Callable[[dict], Awaitable[Any]]
    # main._broadcast_thread_update
    thread_update: Callable[[str], Awaitable[None]]
    # (message_id, patch) -> db.merge_message_metadata. Optional: only the
    # voice-chat cancel path uses it, to stamp `voice_heard {interrupted}` on
    # a reply row that was already persisting when the turn was interrupted.
    merge_metadata: Callable[[str, dict], Awaitable[Any]] | None = None
    # main._track: registers a background task (a speculation's provider
    # call) so shutdown cancels it in order. Optional.
    track: Callable[[asyncio.Task], None] | None = None
    # The provisional-bubble registry and delta sanitizer (main.
    # _open_provisional / _close_provisional / _sanitize_delta). Optional: with
    # any of them missing a reply is delivered whole (simulated streaming).
    open_stream: Callable[[str, str], None] | None = None
    close_stream: Callable[[str, str], bool] | None = None
    sanitize_delta: Callable[[str], str] | None = None


_hooks: Hooks | None = None


def bind(hooks: Hooks) -> None:
    """Register main.py's delivery plumbing. Called once, at import."""
    global _hooks
    _hooks = hooks


# How many messages of a thread to consider for context. The character budget
# does the real trimming; this is only so a 50k-message thread does not get
# fully loaded to then throw nearly all of it away.
HISTORY_ROW_LIMIT = 80


def reply_source_id(kind: str) -> str:
    """A fresh delivery identity for one direct-backend reply.

    A direct turn (API bot, advisor) produces its reply ONCE, through one call:
    there is no second transport that could deliver it again. Without an
    identity the funnel falls back to CONTENT dedup — built for the gateway's
    redundant transports — and its 300 s whole-thread window silently dropped
    a reply that happened to match (or share its first 80 characters with) an
    earlier one: "Sure, I can do that." to two requests a minute apart, or any
    stub that answers the same way twice. With an identity, dedup is by
    identity, which for a fresh id means "deliver".
    """
    return f"{kind}:{uuid.uuid4().hex}"


# --------------------------------------------------------------------------- #
# Voice chat (C-§B.1/B.2): instructions, speculation, the cancel rule
# --------------------------------------------------------------------------- #


def voice_system_text(voice: Any) -> str:
    """The system-slot text of a ``VoiceInstructions``.

    Read by attribute, never ``isinstance`` (Amendment T0-2): the add-on builds
    its own frozen mirror of the type and never imports DisPatch.
    """
    if voice is None:
        return ""
    return str(getattr(voice, "system", "") or "").strip()


def with_voice_system(system: str, voice: Any) -> str:
    """`system` with the voice section appended (API bots: after the
    operator's own prompt, which is the persona; nothing comes after it)."""
    extra = voice_system_text(voice)
    return f"{system.rstrip()}\n\n{extra}" if extra else system


def prompt_chars(messages: list[dict]) -> int:
    """Characters billed for a prompt (`SpeculativeRun.prompt_chars`)."""
    return sum(len(m.get("content") or "") for m in messages
               if isinstance(m.get("content"), str))


#: Longest an un-adopted speculation may keep its provider call open. The
#: add-on's Speculator gives up after 20 s; this is the backstop for a run it
#: forgot about, so a stream can never stay open for a whole reply nobody wants.
SPECULATION_TTL_S = 60.0
#: How long `discard()` waits for the provider stream to be closed.
DISCARD_WAIT_S = 1.0

_live_runs: set[asyncio.Task] = set()


async def _shepherd(task: asyncio.Task) -> None:
    """Wait for a speculation; cancelled (shutdown), close it first."""
    try:
        await asyncio.wait({task})
    except asyncio.CancelledError:
        task.cancel()
        with contextlib.suppress(BaseException):
            await asyncio.wait({task}, timeout=DISCARD_WAIT_S)
        raise


class SpeculativeRun:
    """A model call started before the turn was committed (C-§B.1).

    Persists nothing and broadcasts nothing until a real turn ADOPTS it
    (``run_api_turn(..., prefetched=run)`` / ``advisor.run_turn``). ``discard``
    closes the provider stream and never persists — it is not an abort.

    Public fields (the contract): ``text`` (visible text buffered so far),
    ``done``, ``head_id`` (the thread's last message id when the prompt was
    built), ``prompt_chars``. The rest is this module's bookkeeping.
    """

    backend = "api"

    def __init__(self, thread_id: str, bot_id: str, text: str, *,
                 head_id: str | None, prompt_chars: int = 0) -> None:
        self.thread_id, self.bot_id, self.text_in = thread_id, bot_id, text
        self.text = ""
        self.raw = ""
        self.done = False
        self.head_id = head_id
        self.prompt_chars = prompt_chars
        self.error: BaseException | None = None
        self.adopted = False
        self.discarded = False
        self.started_at = time.monotonic()
        self._task: asyncio.Task | None = None
        self._ttl: asyncio.TimerHandle | None = None

    # -- lifecycle -------------------------------------------------------- #

    def _start(self, coro: Awaitable[Any], *,
               track: Callable[[asyncio.Task], None] | None = None) -> None:
        loop = asyncio.get_running_loop()
        task = loop.create_task(coro, name=f"speculate:{self.backend}:{self.thread_id}")
        self._task = task
        _live_runs.add(task)
        task.add_done_callback(self._finished)
        if track is not None:
            # Registered with the app's task set through a shepherd, so that
            # shutdown cancels (and awaits) the provider call in order instead
            # of leaving it to loop teardown — and a provider error, which is
            # the adopting turn's to report, is never logged as "background
            # task died".
            with contextlib.suppress(Exception):
                track(loop.create_task(_shepherd(task),
                                       name=f"speculate-watch:{self.thread_id}"))
        self._ttl = loop.call_later(SPECULATION_TTL_S, self._expire)

    def _finished(self, task: asyncio.Task) -> None:
        self.done = True
        _live_runs.discard(task)
        if self._ttl is not None:
            self._ttl.cancel()
        if task.cancelled():
            return
        exc = task.exception()          # retrieved: never "exception never retrieved"
        if exc is not None:
            self.error = exc
            log.info("speculation on %s ended with %s: %s", self.thread_id,
                     type(exc).__name__, getattr(exc, "message", exc))

    def _expire(self) -> None:
        if not self.adopted and self._task is not None and not self._task.done():
            log.info("speculation on %s expired un-adopted after %.0fs; closing it",
                     self.thread_id, SPECULATION_TTL_S)
            self.discarded = True
            self._task.cancel()

    def usable(self) -> bool:
        """Can a turn still adopt this run? (A method: the host calls
        ``run.usable()`` before adopting, C-§B.1 rule 4.)"""
        task = self._task
        if task is None or self.adopted or self.discarded or task.cancelled():
            return False
        return not (task.done() and task.exception() is not None)

    def claim(self, thread_id: str, bot_id: str) -> bool:
        """Adopt the run for a real turn in `thread_id` (once)."""
        if not self.usable() or thread_id != self.thread_id or bot_id != self.bot_id:
            return False
        self.adopted = True
        if self._ttl is not None:
            self._ttl.cancel()
        return True

    async def result(self) -> Any:
        """The call's result. Cancelling the awaiting turn cancels the call."""
        assert self._task is not None
        return await self._task

    async def discard(self) -> None:
        """Close the provider stream. Idempotent. Never persists anything.

        A run a turn has already adopted belongs to that turn: discarding it
        is a no-op (the turn's own cancel path decides what happens to it).
        """
        if self.adopted or self.discarded:
            return
        self.discarded = True
        task = self._task
        if task is not None and not task.done():
            task.cancel()
            await asyncio.wait({task}, timeout=DISCARD_WAIT_S)


async def adopt(run: Any, thread_id: str, bot_id: str,
                kind: type[SpeculativeRun]) -> SpeculativeRun | None:
    """Claim `run` for this turn, or close it and return None.

    Anything that is not a live run of this backend for this thread (another
    backend's run, a discarded or failed one, a foreign thread) is discarded
    so its stream never outlives the decision, and the turn starts its own call.
    """
    if run is None:
        return None
    if isinstance(run, kind) and run.claim(thread_id, bot_id):
        return run
    discard = getattr(run, "discard", None)
    if discard is not None:
        with contextlib.suppress(Exception):
            await discard()
    return None


class _CancelPolicy:
    """What the running direct turn of a thread does when it is cancelled."""

    __slots__ = ("keep_partial", "row_id", "task")

    def __init__(self, keep_partial: bool, task: asyncio.Task) -> None:
        self.keep_partial = keep_partial
        self.row_id: str | None = None
        self.task = task


_cancel_policy: dict[str, _CancelPolicy] = {}
_background: set[asyncio.Future] = set()
#: How long a cancelled direct turn waits for a reply row that was already
#: being written to land: an advisor partial is one instant delivery; an API
#: reply that was mid-way through its simulated stream needs at most ~2.5 s.
CANCEL_WAIT_S = 3.0
#: How long `cancel_direct_turn` waits for the cancelled turn to wind down
#: (the landed-row wait above plus the status/frames that follow it).
CANCEL_TURN_WAIT_S = CANCEL_WAIT_S + 1.0
#: The longest a cancelled turn's wind-down may hold its task once a SECOND
#: cancel arrived (see :func:`finish_uncancellable`).
WIND_DOWN_MAX_S = CANCEL_WAIT_S + 3.0


def cancel_requested(thread_id: str) -> bool:
    """Was the running turn of this thread cancelled by `cancel_direct_turn`
    (a voice-chat abort) rather than by shutdown?"""
    return thread_id in _cancel_policy


def keep_partial_requested(thread_id: str) -> bool:
    """Read by a direct backend's cancel path (the flag `abort_turn` sets)."""
    policy = _cancel_policy.get(thread_id)
    return bool(policy and policy.keep_partial)


def note_partial_row(thread_id: str, row_id: str | None) -> None:
    """A direct backend's cancel path reports the row its turn left."""
    policy = _cancel_policy.get(thread_id)
    if policy is not None and row_id:
        policy.row_id = row_id


def _drop_policy(thread_id: str, policy: _CancelPolicy, _task: asyncio.Task) -> None:
    if _cancel_policy.get(thread_id) is policy:
        del _cancel_policy[thread_id]


async def cancel_direct_turn(task: asyncio.Task, thread_id: str, *,
                             keep_partial: bool,
                             timeout: float = CANCEL_TURN_WAIT_S) -> str | None:
    """Cancel a running API/advisor turn and return the row it left, if any.

    This is the backend half of ``ExtensionContext.abort_turn`` (C-§B.1, A3):
    `task` is the turn task (``run_agent_turn``) holding the thread.

    * ``keep_partial=True`` (a real interruption): the turn persists what was
      already shown — the advisor's streamed text so far, or an API reply that
      was already being delivered — as a normal assistant row carrying
      ``metadata.voice_heard = {"interrupted": True}`` (and a
      ``message_update`` so every screen sees the stamp), and its id is
      returned. Nothing shown yet → nothing persisted → None.
    * ``keep_partial=False`` (a merge / addendum): nothing new is persisted,
      None. A reply row that was ALREADY being written when the cancel landed
      cannot be un-written by the backend: the turn waits for it so that it is
      in the thread before this returns (Amendment T2-4) — retracting the
      question it answered (``ExtensionContext.retract_message``) then takes
      it back too, instead of it landing after the retraction as an orphan.

    Idempotent per turn: a second call while the first cancel is still winding
    the turn down (barge-in, then hang-up a moment later) does NOT cancel the
    task again — that would land inside its cleanup and skip it (review T2
    #1). It waits for the same outcome and keeps the FIRST call's
    ``keep_partial``: a later ``False`` cannot un-persist what an earlier
    ``True`` is persisting, nor a later ``True`` revive what ``False`` dropped.

    Markers in a cancelled advisor reply are never acted on either way.
    """
    policy = _cancel_policy.get(thread_id)
    if policy is None or policy.task is not task:
        if task.done():
            return None
        policy = _CancelPolicy(keep_partial, task)
        _cancel_policy[thread_id] = policy
        # Dropped when the TURN ends, not when this call returns: a call that
        # times out must not let a second call cancel the task again.
        task.add_done_callback(functools.partial(_drop_policy, thread_id, policy))
        task.cancel()
    if not task.done():
        await asyncio.wait({task}, timeout=timeout)
        if not task.done():
            log.warning("cancelled turn on %s did not finish within %.1fs",
                        thread_id, timeout)
    return policy.row_id if policy.keep_partial else None


async def finish_uncancellable(coro: Awaitable[Any], *,
                               limit: float = WIND_DOWN_MAX_S) -> None:
    """Run a cancelled turn's wind-down to the end, even if the turn's task is
    cancelled AGAIN while it runs.

    ``contextlib.suppress(Exception)`` does not stop a second
    ``CancelledError``, and one landing inside the keep-partial delivery or
    ``set_status("idle")`` left the thread in "thinking" with a half-painted
    bubble (review T2 #1). The wind-down runs in its own task; further cancels
    of the caller are absorbed while it finishes, for at most `limit` seconds
    (after that it carries on in the background). A cancel absorbed here is
    re-raised once the wind-down is over, so the task still ends cancelled.
    Exceptions inside `coro` are logged, never raised: the wind-down is best
    effort by design.
    """
    loop = asyncio.get_running_loop()
    fut = asyncio.ensure_future(coro)
    _track_background(fut)
    fut.add_done_callback(_log_wind_down_error)
    deadline = loop.time() + limit
    absorbed = False
    while not fut.done():
        left = deadline - loop.time()
        if left <= 0:
            log.warning("turn wind-down still running after %.1fs; leaving it "
                        "to finish in the background", limit)
            break
        try:
            await asyncio.wait({fut}, timeout=left)
        except asyncio.CancelledError:
            absorbed = True
    if absorbed:
        raise asyncio.CancelledError


def _log_wind_down_error(fut: asyncio.Future) -> None:
    if fut.cancelled():
        return
    exc = fut.exception()
    if exc is not None:
        log.warning("turn wind-down failed: %s: %s", type(exc).__name__, exc)


async def settle_landed_reply(hooks: Any, thread_id: str, bot_id: str,
                              deliver_task: asyncio.Future) -> str | None:
    """A direct turn was cancelled while its reply row was being written.

    Let that delivery finish (at most ``CANCEL_WAIT_S``), report the row to
    the cancel policy and — for a keep-partial interruption — stamp it
    ``voice_heard {interrupted: true}`` and broadcast a ``message_update``, so
    connected screens learn about the interruption even when no ``note_heard``
    follows (review T2 #4). `hooks` is either backend's Hooks (both carry
    ``merge_metadata`` and ``broadcast``). Returns the row id or None.
    """
    try:
        msg = await asyncio.wait_for(asyncio.shield(deliver_task), CANCEL_WAIT_S)
    except (Exception, asyncio.CancelledError):
        return None
    row_id = getattr(msg, "id", None) if msg is not None else None
    # An all-marker reply comes back as an unpersisted placeholder: no row.
    if not row_id or not (getattr(msg, "content", "") or "").strip():
        return None
    note_partial_row(thread_id, row_id)
    merge = getattr(hooks, "merge_metadata", None)
    if keep_partial_requested(thread_id) and merge is not None:
        with contextlib.suppress(Exception):
            fresh = await merge(row_id, {"voice_heard": {"interrupted": True}})
            dump = getattr(fresh, "model_dump", None)
            if dump is not None:
                await hooks.broadcast({"type": "message_update", "thread_id": thread_id,
                                       "bot_id": bot_id, "message_id": row_id,
                                       "message": dump()})
    return row_id


def _track_background(fut: asyncio.Future) -> None:
    _background.add(fut)
    fut.add_done_callback(_background.discard)


class _ApiSpeculation(SpeculativeRun):
    backend = "api"
    reply: Reply | None = None


async def speculate(thread_id: str, bot_id: str, text: str, *,
                    voice: Any = None) -> SpeculativeRun:
    """Start the API call a voice turn WILL make, before the turn exists.

    Same system prompt (with the voice section), same history and the same
    provider as :func:`run_api_turn`; the call runs in the background and
    nothing is persisted or broadcast. A speculation fetches the reply whole
    (no live bubble exists to stream into), so ``text`` stays empty until the
    whole reply is in and then holds it; adoption gets the reply the instant
    it exists.
    """
    hooks = _hooks
    if hooks is None:                            # pragma: no cover - wiring bug
        raise RuntimeError("llm_api.bind() was never called")
    bot = config.get_bot(bot_id)
    if bot is None or not bot.api:
        raise ApiError("This bot is not connected to a provider",
                       f"{bot_id!r} has no `api` block in config.yaml.")
    base = resolve(bot)
    cfg = dataclasses.replace(base, system_prompt=with_voice_system(base.system_prompt, voice))
    rows, _ = await hooks.list_messages(thread_id, HISTORY_ROW_LIMIT)
    head_id = getattr(rows[-1], "id", None) if rows else None
    history = append_user_turn(
        build_history(rows, cfg.max_history_chars, truncate_heard=True), text)
    if not history:
        raise ApiError("There was nothing to send", "The speculation had no text.")
    run = _ApiSpeculation(thread_id, bot_id, text, head_id=head_id, prompt_chars=prompt_chars(
        [{"content": cfg.system_prompt}, *history]))

    async def call() -> Reply:
        reply = await complete(cfg, history)
        run.reply = reply
        run.raw = run.text = reply.text
        return reply

    run._start(call(), track=hooks.track)
    return run


async def run_api_turn(thread_id: str, bot_id: str, text: str, *,
                       voice: Any = None,
                       prefetched: SpeculativeRun | None = None) -> None:
    """Answer `text` in `thread_id` using the bot's direct provider.

    Contract-identical to ``main.run_agent_turn``: the thread goes to
    "thinking", a `thinking` frame opens and closes, the reply lands through
    the shared funnel, and EVERY failure path sets the thread to "error",
    broadcasts an error frame and still stops the spinner. A silent hang here
    would leave a family member watching a thinking dot forever, which is worse
    than any error message.

    Voice chat (C-§B.2): ``voice.system`` is appended to the system prompt and
    the user turn stays the clean transcript; ``prefetched`` is a run started
    by :func:`speculate` that this turn adopts instead of making a new call.
    Cancelled with the keep-partial flag set (:func:`cancel_direct_turn`), a
    reply that was already being delivered is finished and its row reported.
    """
    hooks = _hooks
    if hooks is None:                            # pragma: no cover - wiring bug
        raise RuntimeError("llm_api.bind() was never called")

    deliver_task: asyncio.Future | None = None
    run = None
    emitter: StreamEmitter | None = None
    await hooks.set_status(thread_id, "thinking")
    await hooks.broadcast({"type": "thinking", "thread_id": thread_id,
                           "bot_id": bot_id, "status": "started"})
    await hooks.thread_update(thread_id)
    try:
        bot = config.get_bot(bot_id)
        if bot is None or not bot.api:
            raise ApiError("This bot is not connected to a provider",
                           f"{bot_id!r} has no `api` block in config.yaml.")
        run, prefetched = await adopt(prefetched, thread_id, bot_id, _ApiSpeculation), None
        if run is not None:
            reply = await run.result()
        else:
            base = resolve(bot)
            cfg = dataclasses.replace(
                base, system_prompt=with_voice_system(base.system_prompt, voice))
            rows, _ = await hooks.list_messages(thread_id, HISTORY_ROW_LIMIT)
            history = _ensure_trailing_user(
                build_history(rows, cfg.max_history_chars, truncate_heard=True), text)
            if not history:
                raise ApiError("There was nothing to send",
                               "The thread had no user message to answer.")
            if _can_stream_live(hooks):
                emitter = StreamEmitter(thread_id, bot_id, hooks)
            reply, _streamed = await complete_streaming(
                cfg, history, bot.api, emitter.push if emitter else None)
            if emitter is not None and emitter.started:
                await emitter.push(reply.text, force=True)
        meta: dict[str, Any] = {"model": reply.model, "provider": reply.provider}
        if reply.tokens is not None:
            meta["tokens"] = reply.tokens
        # A reply that streamed live lands as the row that claims ITS bubble
        # (only that bubble); one that arrived whole goes through the app's
        # simulated streaming (stream=True), so it appears like an agent one.
        # Shielded: once the row is being written, an interruption must not
        # leave it half-delivered (the cancel path decides whether to wait).
        live = emitter.prov if emitter is not None and emitter.started else None
        deliver_task = asyncio.ensure_future(hooks.deliver(
            thread_id, reply.text, metadata=meta, stream=True,
            source_id=reply_source_id("api"),
            **({"provisional": live} if live else {})))
        _track_background(deliver_task)
        await asyncio.shield(deliver_task)
        if emitter is not None:
            # No-op when the row claimed the bubble; otherwise (deduplicated,
            # all-marker reply) the only thing that closes it.
            await emitter.retire()
        await hooks.set_status(thread_id, "idle")
    except ApiError as e:
        log.warning("api turn failed (%s/%s): %s — %s",
                    bot_id, thread_id, e.message, e.detail)
        if emitter is not None:
            with contextlib.suppress(Exception):
                await emitter.retire()
        await hooks.set_status(thread_id, "error")
        await hooks.broadcast({"type": "error", "thread_id": thread_id,
                               "bot_id": bot_id, "message": e.message,
                               "detail": e.detail})
    except asyncio.CancelledError:
        # An interruption (voice chat) or shutdown. Don't leave the thread
        # stuck in 'thinking' for the next boot to clean up, but don't pretend
        # it was an error either. Uncancellable: a second abort landing in
        # here must not skip the status reset (review T2 #1).
        async def wind_down() -> None:
            # A voice abort (not shutdown) waits for a reply that was already
            # being delivered, whatever keep_partial says: it is in the thread
            # either way, and the caller must know it is there before this
            # turn counts as stopped (Amendment T2-4).
            if deliver_task is not None and cancel_requested(thread_id):
                await settle_landed_reply(hooks, thread_id, bot_id, deliver_task)
            if emitter is not None:
                # A half-streamed reply that never became a row: close the
                # bubble rather than leave it half-typed until a reload.
                with contextlib.suppress(Exception):
                    await emitter.retire()
            with contextlib.suppress(Exception):
                await hooks.set_status(thread_id, "idle")
        await finish_uncancellable(wind_down())
        raise
    except Exception as e:
        log.exception("unexpected api turn error (%s/%s)", bot_id, thread_id)
        if emitter is not None:
            with contextlib.suppress(Exception):
                await emitter.retire()
        await hooks.set_status(thread_id, "error")
        await hooks.broadcast({"type": "error", "thread_id": thread_id,
                               "bot_id": bot_id, "message": "Something went wrong.",
                               "detail": str(e)[:300]})
    finally:
        # A run handed to a turn that failed before it could adopt it must
        # not keep its provider call open.
        if prefetched is not None:
            with contextlib.suppress(Exception):
                await prefetched.discard()
        # Suppressed, and only for Exception: stopping the spinner is the
        # promise this block exists to keep, but on shutdown the database is
        # closing underneath us and a failure HERE would replace the real
        # reason the turn ended with a "background task died" traceback.
        # CancelledError is deliberately not caught — it still propagates.
        await finish_uncancellable(_stop_thinking(hooks, thread_id, bot_id))


async def _stop_thinking(hooks: Any, thread_id: str, bot_id: str) -> None:
    """The ``thinking: stopped`` frame + thread update every direct turn ends
    with, run through :func:`finish_uncancellable` so a cancel landing at the
    very end cannot leave a typing dot on every screen."""
    with contextlib.suppress(Exception):
        await hooks.broadcast({"type": "thinking", "thread_id": thread_id,
                               "bot_id": bot_id, "status": "stopped"})
        await hooks.thread_update(thread_id)


# --------------------------------------------------------------------------- #
# Create-or-update a connected bot
# --------------------------------------------------------------------------- #


# Ids are derived, not typed: `llm-openai`, `llm-lmstudio`. Re-running the panel
# for the same provider therefore UPDATES that bot rather than growing a second
# one, which is what an operator correcting a typo'd key expects.
def default_bot_id(provider_id: str) -> str:
    return f"llm-{provider_id}"


_EMOJI_BY_PROVIDER = {
    "lmstudio": "🖥", "ollama": "🦙", "llamacpp": "🖥", "openai": "🟢", "anthropic": "🅰",
    "deepseek": "🐋", "groq": "⚡", "openrouter": "🧭", "mistral": "🌬",
    "xai": "✖", "together": "🤝", "custom": "🔌",
}


@dataclass
class ConnectSpec:
    provider: str
    model: str
    base_url: str = ""
    api_key: str = ""
    api_key_env: str = ""
    name: str = ""
    system_prompt: str = ""
    bot_id: str = ""
    max_history_chars: int = 0


def connect(spec: ConnectSpec) -> config.Bot:
    """Validate a setup-panel submission and persist it as a bot.

    Deliberately conservative about what the new bot is allowed to do:
    `reactions=False` (firing images is an opt-in per-character trait, and a
    provider that has never been tested should not get it by default).
    """
    provider = PROVIDER_BY_ID.get((spec.provider or "").strip())
    if provider is None:
        raise ApiError("Unknown provider",
                       f"{spec.provider!r} is not one of: "
                       f"{', '.join(sorted(PROVIDER_BY_ID))}.")
    base_url = normalize_base_url(spec.base_url or provider.base_url)
    model = (spec.model or "").strip() or (provider.models[0] if provider.models else "")
    if not model:
        raise ApiError("A model is required",
                       "Run Test connection to list the models this provider "
                       "offers, or type the model name in.")
    bot_id = (spec.bot_id or "").strip() or default_bot_id(provider.id)
    existing = config.get_bot(bot_id)
    if existing is not None and not existing.api:
        # Refusing rather than silently converting: `assistant` is an agent bot
        # with real history behind it, and turning it into an API bot from a
        # setup panel would change where its replies come from without saying so.
        raise ApiError(
            "That bot already exists and is not an API bot",
            f"{bot_id!r} is configured for the agent backend. Choose a "
            "different name, or remove it from config.yaml first.")
    # WHAT THE PANEL DID NOT SEND, IT DID NOT MEAN TO ERASE. A saved key never
    # comes back out of the API (deliberately — see the routes), so the panel
    # re-submits an EMPTY key for a bot that has one. `upsert_bot` replaces the
    # entry wholesale, so rebuilding `api` from the submission alone deleted
    # the stored key, system prompt and history cap: editing a bot's name broke
    # its ability to answer at all, and the only symptom was an auth error on
    # the next turn.
    # Only from the SAME provider: a re-save that switches provider must not
    # carry the old provider's key across.
    stored: dict[str, Any] = (
        dict(existing.api)
        if existing is not None and existing.api.get("provider") == provider.id
        else {})

    key = (spec.api_key or "").strip() or str(stored.get("api_key") or "")
    key_env = (spec.api_key_env or "").strip() or str(
        stored.get("api_key_env") or "")
    if provider.key_required and not key and not (
            key_env and os.environ.get(key_env, "").strip()):
        raise ApiError(f"{provider.label} needs an API key",
                       "Paste a key, or name an environment variable that "
                       "holds one and make sure it is set for this process.")

    api: dict[str, Any] = {"provider": provider.id, "base_url": base_url,
                           "model": model}
    if key:
        api["api_key"] = key
    if key_env:
        api["api_key_env"] = key_env
    prompt = (spec.system_prompt or "").strip() or str(
        stored.get("system_prompt") or "")
    if prompt:
        api["system_prompt"] = prompt
    history_chars = spec.max_history_chars or stored.get("max_history_chars")
    if history_chars:
        api["max_history_chars"] = int(history_chars)
    # An existing API bot keeps everything the panel does not manage — its
    # avatar, its colour, its position, and any flags the operator has since
    # turned on in the Bot Manager.
    if existing is not None:
        # dataclasses.replace, not a rebuild from a field subset: the rebuild
        # used to drop everything it did not spell out -- agent, advisor,
        # avatar_pool, the image_* fields, voice, reaction_autopilot -- on
        # every re-save of a connected bot.
        bot = dataclasses.replace(
            existing,
            name=(spec.name or "").strip() or existing.name,
            emoji=existing.emoji or _EMOJI_BY_PROVIDER.get(provider.id, "🔌"),
            model_hint=model,
            api=api,
        )
    else:
        bot = config.Bot(
            id=bot_id,
            name=(spec.name or "").strip() or provider.bot_name or provider.label,
            emoji=_EMOJI_BY_PROVIDER.get(provider.id, "🔌"),
            model_hint=model,
            safe=False,
            reactions=False,
            api=api,
        )
    return config.upsert_bot(bot)


# --------------------------------------------------------------------------- #
# Connect -> a DIRECT-API bot (the advisor backend)
# --------------------------------------------------------------------------- #

#: Ids the API mints for a new bot. An existing bot may carry any id.
_NEW_BOT_ID_RE = re.compile(r"^[a-z0-9_-]{1,32}$")
DEFAULT_DIRECT_TARGET = "atlas"
#: Provider-entry keys a re-connect to the same provider carries over from the
#: stored entry when the panel did not send them (they are not in the form).
_KEEP_ON_RECONNECT = ("max_tokens", "read_timeout_s", "extra_body")
#: A system prompt longer than this is a document, not a persona.
MAX_PERSONA_CHARS = 8000


@dataclass
class DirectConnectSpec:
    provider: str
    model: str = ""
    base_url: str = ""
    api_key: str = ""
    api_key_env: str = ""
    target: str = DEFAULT_DIRECT_TARGET
    name: str = ""
    emoji: str = ""
    #: The panel's "System prompt" -> ``advisor.persona`` (replaces the default
    #: persona text). Blank = keep whatever the bot has; it never clears one.
    persona: str = ""
    tools: bool = True
    max_tokens: int = 0


def direct_provider_entry(spec: DirectConnectSpec,
                          stored: dict | None = None) -> dict[str, Any]:
    """The ``advisor.providers[0]`` entry for a setup-panel submission.

    Built from the preset exactly the way the advisor reads one
    (docs/advisor.md: ``provider, base_url, model, api_key | api_key_env,
    max_tokens, extra_body``). The preset's own ``extra_body`` is written into
    the entry -- the MiniMax preset's thinking-off switch -- so the file says
    what the request will carry. ``stored`` is the bot's current first entry:
    for the SAME provider its key (and extras) survive a re-save whose form sent
    none, because a saved key never comes back out of the API.
    """
    provider = PROVIDER_BY_ID.get((spec.provider or "").strip())
    if provider is None:
        raise ApiError("Unknown provider",
                       f"{spec.provider!r} is not one of: "
                       f"{', '.join(sorted(PROVIDER_BY_ID))}.")
    base_url = normalize_base_url(spec.base_url or provider.base_url)
    model = (spec.model or "").strip() or (provider.models[0] if provider.models else "")
    if not model:
        raise ApiError("A model is required",
                       "Run Test connection to list the models this provider "
                       "offers, or type the model name in.")
    same = bool(stored) and (stored or {}).get("provider") == provider.id
    kept: dict[str, Any] = dict(stored) if same else {}
    key = (spec.api_key or "").strip() or str(kept.get("api_key") or "")
    key_env = (spec.api_key_env or "").strip() or str(kept.get("api_key_env") or "")
    if provider.key_required and not key and not (
            key_env and os.environ.get(key_env, "").strip()):
        raise ApiError(f"{provider.label} needs an API key",
                       "Paste a key, or name an environment variable that "
                       "holds one and make sure it is set for this process.")
    entry: dict[str, Any] = {"provider": provider.id, "base_url": base_url,
                             "model": model}
    if key:
        entry["api_key"] = key
    if key_env:
        entry["api_key_env"] = key_env
    for k in _KEEP_ON_RECONNECT:
        if kept.get(k):
            entry[k] = kept[k]
    if spec.max_tokens and int(spec.max_tokens) > 0:
        entry["max_tokens"] = int(spec.max_tokens)
    if provider.extra_body and "extra_body" not in entry:
        entry["extra_body"] = dict(provider.extra_body)
    return entry


def connect_direct(spec: DirectConnectSpec) -> config.Bot:
    """Point a bot at a provider through the advisor backend ("Direct API").

    ``advisor.providers`` becomes ``[entry]`` and ``advisor.tools`` follows
    ``spec.tools``; every other key of an existing advisor block (owner,
    knowledge_dir, lookup, agents, ...) and every other field of the bot is
    kept. A new id creates the bot (never Safe Mode, no reactions -- the
    operator opts in). An id that is an agent or legacy-API bot is refused
    rather than silently converted.
    """
    target = (spec.target or "").strip() or DEFAULT_DIRECT_TARGET
    # Ids are unique case-insensitively (session keys are lowercased), so a
    # case-only difference names the existing bot rather than a new one.
    existing = config.resolve_bot(target)
    if existing is None:
        if not _NEW_BOT_ID_RE.match(target):
            raise ApiError("That bot id is not allowed",
                           "Use 1-32 characters from a-z, 0-9, '-' and '_'.")
    elif not config.is_direct(existing):
        raise ApiError(
            "That bot already exists and is not a direct-API bot",
            f"{target!r} uses the "
            f"{'legacy API' if existing.api else 'agent'} backend. Choose a "
            "different name, or remove it first.")
    stored_first = None
    if existing is not None:
        stored_first = next((p for p in (existing.advisor or {}).get("providers") or []
                             if isinstance(p, dict)), None)
    entry = direct_provider_entry(spec, stored_first)
    tools_on = bool(spec.tools)
    # The same field the Bots tab's Personality editor writes (app/personas):
    # blank keeps the bot's persona; the shipped default text means "no
    # custom persona", stored as no key so a later default reaches the bot.
    raw = (spec.persona or "").strip()[:MAX_PERSONA_CHARS]
    try:
        persona = personas.normalise(raw)
    except ValueError as e:
        raise ApiError("That system prompt cannot be saved", str(e)) from e
    reset = bool(raw) and not persona
    if existing is not None:
        # "Lookups on" keeps a hand-picked subset (config.set_direct_tools
        # parks it while off); only a plain on/off flips.
        adv = config.set_direct_tools(existing.advisor, tools_on)
        if persona:
            adv["persona"] = persona
        elif reset:
            adv.pop("persona", None)
        # Only the FIRST provider is the connection the panel edits; a fallback
        # chain behind it (a local model after the API) stays as it was.
        rest = [p for p in ((existing.advisor or {}).get("providers") or [])[1:] if isinstance(p, dict)]
        adv["providers"] = [entry, *rest]
        hint = existing.model_hint
        bot = dataclasses.replace(
            existing,
            name=(spec.name or "").strip() or existing.name,
            emoji=(spec.emoji or "").strip() or existing.emoji
            or _EMOJI_BY_PROVIDER.get(entry["provider"], "🌍"),
            model_hint=entry["model"] if hint in ("", "Direct API") else hint,
            advisor=adv)
    else:
        adv = {"providers": [entry], "tools": tools_on}
        if persona:
            adv["persona"] = persona
        bot = config.Bot(
            id=target,
            name=(spec.name or "").strip() or target,
            emoji=(spec.emoji or "").strip() or "🌍",
            model_hint=entry["model"],
            safe=False, reactions=False,
            advisor=adv)
    return config.upsert_bot(bot)
