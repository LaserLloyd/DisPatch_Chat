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
the dedup and the simulated streaming all still apply — see :func:`bind`.

Streaming
---------
v1 does NOT stream from the provider. The reply is fetched whole and then
delivered through the app's existing simulated-streaming seam (the same one the
agent path uses for its final reply), so on screen it looks identical. Real
token streaming would need a second delivery protocol for partial messages that
the WS layer does not have today; adding one to make a two-second reply arrive
in pieces is not worth the surface area yet.

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
import json
import logging
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse, urlunparse

import httpx

from . import config

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
    ),
    Provider(
        id="together", label="Together AI", bot_name="Together", base_url="https://api.together.xyz/v1",
        key_env="TOGETHER_API_KEY", docs="https://api.together.xyz/settings/api-keys",
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

DEFAULT_SYSTEM_PROMPT = (
    "You are {name}, a friendly assistant in a family chat app called DisPatch Chat. "
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
    """One request, following redirects by hand so each hop is re-validated.

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
        try:
            if resp.status_code in (301, 302, 303, 307, 308):
                location = resp.headers.get("location") or ""
                if not location:
                    raise ApiError("The provider sent an empty redirect",
                                   f"{url} answered {resp.status_code} with no "
                                   "Location header.")
                url = normalize_base_url(str(httpx.URL(url).join(location)))
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
                continue
            return resp.status_code, await _read_capped(resp)
        finally:
            await resp.aclose()
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


def _transport_error(e: Exception, url: str, *, local: bool) -> ApiError:
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
            f"No reply from {url} within {int(READ_TIMEOUT_S)}s. A local model "
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
    said which one they trust, and it is not the one on disk."""
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
    api = bot.api or {}
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
    system = str(api.get("system_prompt") or "").strip() or \
        DEFAULT_SYSTEM_PROMPT.format(name=bot.name)
    return Resolved(provider=provider, base_url=base_url, model=model,
                    api_key=api_key, system_prompt=system,
                    max_history_chars=max(500, budget))


# --------------------------------------------------------------------------- #
# History
# --------------------------------------------------------------------------- #


def build_history(messages: list, budget: int = DEFAULT_HISTORY_CHARS) -> list[dict]:
    """Map a thread's stored messages onto provider chat turns.

    Rules, in the order they matter:

    * ``system`` rows and "sub" rows (the collapsed working-output the agent
      path emits between tool calls) are not conversation — they are dropped.
      Sending them would teach an API bot to imitate an agent it is not.
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
    for m in reversed(messages or []):
        role = getattr(m, "role", None) or ""
        if role not in ("user", "assistant"):
            continue
        meta = getattr(m, "metadata", None) or {}
        if isinstance(meta, dict) and meta.get("sub"):
            continue
        content = (getattr(m, "content", None) or "").strip()
        if not content:
            continue
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
        return [*history[:-1], {"role": "user",
                                "content": history[-1]["content"] + "\n\n" + text}]
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
    payload = _decode_json(raw, url)
    choices = payload.get("choices") if isinstance(payload, dict) else None
    if not isinstance(choices, list) or not choices:
        raise ApiError(
            "The provider returned no reply",
            f"{url} answered {status} with no `choices`. "
            f"{_provider_message(payload) or 'Response: ' + str(payload)[:200]}")
    message = (choices[0] or {}).get("message") or {}
    text = (message.get("content") or "").strip()
    if not text:
        # A finish_reason of "length" with empty content is the classic
        # symptom of a reasoning model whose whole budget went to thinking.
        reason = (choices[0] or {}).get("finish_reason") or "unknown"
        raise ApiError(
            "The model replied with nothing",
            f"The response was empty (finish_reason: {reason}). If this is a "
            "reasoning model, it may have spent its whole output budget "
            "thinking — try a different model.")
    usage = payload.get("usage") if isinstance(payload, dict) else None
    tokens = None
    if isinstance(usage, dict) and isinstance(usage.get("total_tokens"), int):
        tokens = usage["total_tokens"]
    return Reply(text=text, model=str(payload.get("model") or cfg.model),
                 provider=cfg.provider.id, tokens=tokens)


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
            raise _transport_error(e, models_url, local=local) from e

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
                raise _transport_error(e, chat_url, local=local) from e
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


_hooks: Hooks | None = None


def bind(hooks: Hooks) -> None:
    """Register main.py's delivery plumbing. Called once, at import."""
    global _hooks
    _hooks = hooks


# How many messages of a thread to consider for context. The character budget
# does the real trimming; this is only so a 50k-message thread does not get
# fully loaded to then throw nearly all of it away.
HISTORY_ROW_LIMIT = 80


async def run_api_turn(thread_id: str, bot_id: str, text: str) -> None:
    """Answer `text` in `thread_id` using the bot's direct provider.

    Contract-identical to ``main.run_agent_turn``: the thread goes to
    "thinking", a `thinking` frame opens and closes, the reply lands through
    the shared funnel, and EVERY failure path sets the thread to "error",
    broadcasts an error frame and still stops the spinner. A silent hang here
    would leave a family member watching a thinking dot forever, which is worse
    than any error message.
    """
    hooks = _hooks
    if hooks is None:                            # pragma: no cover - wiring bug
        raise RuntimeError("llm_api.bind() was never called")

    await hooks.set_status(thread_id, "thinking")
    await hooks.broadcast({"type": "thinking", "thread_id": thread_id,
                           "bot_id": bot_id, "status": "started"})
    await hooks.thread_update(thread_id)
    try:
        bot = config.get_bot(bot_id)
        if bot is None or not bot.api:
            raise ApiError("This bot is not connected to a provider",
                           f"{bot_id!r} has no `api` block in config.yaml.")
        cfg = resolve(bot)
        rows, _ = await hooks.list_messages(thread_id, HISTORY_ROW_LIMIT)
        history = _ensure_trailing_user(
            build_history(rows, cfg.max_history_chars), text)
        if not history:
            raise ApiError("There was nothing to send",
                           "The thread had no user message to answer.")
        reply = await complete(cfg, history)
        meta: dict[str, Any] = {"model": reply.model, "provider": reply.provider}
        if reply.tokens is not None:
            meta["tokens"] = reply.tokens
        # stream=True routes through the app's simulated streaming, so a direct
        # API reply appears exactly like an agent one.
        await hooks.deliver(thread_id, reply.text, metadata=meta, stream=True)
        await hooks.set_status(thread_id, "idle")
    except ApiError as e:
        log.warning("api turn failed (%s/%s): %s — %s",
                    bot_id, thread_id, e.message, e.detail)
        await hooks.set_status(thread_id, "error")
        await hooks.broadcast({"type": "error", "thread_id": thread_id,
                               "bot_id": bot_id, "message": e.message,
                               "detail": e.detail})
    except asyncio.CancelledError:
        # Shutdown. Don't leave the thread stuck in 'thinking' for the next boot
        # to clean up, but don't pretend it was an error either.
        with contextlib.suppress(Exception):
            await hooks.set_status(thread_id, "idle")
        raise
    except Exception as e:
        log.exception("unexpected api turn error (%s/%s)", bot_id, thread_id)
        await hooks.set_status(thread_id, "error")
        await hooks.broadcast({"type": "error", "thread_id": thread_id,
                               "bot_id": bot_id, "message": "Something went wrong.",
                               "detail": str(e)[:300]})
    finally:
        # Suppressed, and only for Exception: stopping the spinner is the
        # promise this block exists to keep, but on shutdown the database is
        # closing underneath us and a failure HERE would replace the real
        # reason the turn ended with a "background task died" traceback.
        # CancelledError is deliberately not caught — it still propagates.
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
    "lmstudio": "🖥", "ollama": "🦙", "openai": "🟢", "anthropic": "🅰",
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
    `safe=False` (it is NOT offered to Safe-Mode devices until the operator
    says so in the Bot Manager) and `reactions=False` (firing images is an
    opt-in per-character trait, and a provider that has never been tested
    should not get it by default).
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
    key = (spec.api_key or "").strip()
    key_env = (spec.api_key_env or "").strip()
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
    if (spec.system_prompt or "").strip():
        api["system_prompt"] = spec.system_prompt.strip()
    if spec.max_history_chars:
        api["max_history_chars"] = int(spec.max_history_chars)

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
    # An existing API bot keeps everything the panel does not manage — its
    # avatar, its colour, its position, and any flags the operator has since
    # turned on in the Bot Manager.
    if existing is not None:
        bot = config.Bot(
            id=bot_id,
            name=(spec.name or "").strip() or existing.name,
            avatar=existing.avatar,
            emoji=existing.emoji or _EMOJI_BY_PROVIDER.get(provider.id, "🔌"),
            model_hint=model,
            order=existing.order,
            visible=existing.visible,
            safe=existing.safe,
            color=existing.color,
            reactions=existing.reactions,
            api=api,
        )
    else:
        bot = config.Bot(
            id=bot_id,
            name=(spec.name or "").strip() or provider.bot_name or provider.label,
            emoji=_EMOJI_BY_PROVIDER.get(provider.id, "🔌"),
            model_hint=model,
            visible=True,
            safe=False,
            reactions=False,
            api=api,
        )
    return config.upsert_bot(bot)
