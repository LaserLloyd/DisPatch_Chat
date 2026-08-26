"""RFC 9457 problem details for the machine-inbound API.

Why
---
Agents are the second audience for this API and the one that cannot read. A
refusal reaching a model today is a prose sentence — "Only agents may fire
reactions", "Unlock for full access", "thread not found" — and the only way to
branch on it is to match that sentence, which means the day anyone improves the
wording, every caller that was handling the case correctly starts handling it
wrongly, silently. That is the whole reason a small model retries the thing it
should not: it cannot tell "you asked for the wrong thing" apart from "you are
not allowed" apart from "try again in a moment".

So a machine gets a stable, enumerable `code` alongside the prose:

    HTTP/1.1 403 Forbidden
    Content-Type: application/problem+json

    {
      "type":   "/problems/safe-mode",
      "title":  "Unlock for full access",
      "status": 403,
      "code":   "safe_mode",
      "detail": "Unlock for full access"
    }

Additive on purpose
-------------------
`detail` keeps carrying exactly the string it carried before, in the same key,
so every existing caller — the frontend's `err.detail`, the skills that quote
these messages, the tests — is unaffected. The new fields are extra keys.

And only a MACHINE sees this shape at all. A browser-shaped request keeps the
plain `{"detail": ...}` body with `application/json`, because a browser has no
use for a problem document and changing the content type under the frontend
buys nothing but risk.

`type` is a relative URI. RFC 9457 explicitly permits that, and inventing an
absolute one would either point at a domain this self-hosted app does not own
or leak the deployment's own hostname into every error body.
"""
from __future__ import annotations

from typing import Any

MEDIA_TYPE = "application/problem+json"

# Prose refusal -> stable code. Keyed on the exact `detail` the route raises.
#
# This mapping is the contract. A route may reword its message freely — the
# code is what callers branch on — but a REWORDED message that is still in this
# table keeps its code, and one that falls out of the table degrades to the
# generic per-status code rather than inventing a new name nobody published.
_BY_DETAIL: dict[str, str] = {
    "Unlock for full access":            "safe_mode",
    "Only agents may fire reactions":    "agents_only",
    "thread not found":                  "thread_not_found",
    "Thread not found":                  "thread_not_found",
    "bot not found":                     "bot_not_found",
    "Bot not found":                     "bot_not_found",
    "message not found":                 "message_not_found",
    "Message not found":                 "message_not_found",
    "reaction not found":                "reaction_not_found",
    "Invalid API key":                   "invalid_api_key",
    "API key required":                  "api_key_required",
}

# Fallback by status, so every problem document has SOME code. Deliberately
# coarse: a generic code that says "this class of thing went wrong" is honest,
# where a guessed specific one is worse than none.
_BY_STATUS: dict[int, str] = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    409: "conflict",
    413: "payload_too_large",
    415: "unsupported_media_type",
    422: "unprocessable",
    429: "rate_limited",
    500: "internal_error",
    503: "unavailable",
}


def code_for(status: int, detail: Any) -> str:
    if isinstance(detail, str):
        hit = _BY_DETAIL.get(detail.strip())
        if hit:
            return hit
    return _BY_STATUS.get(status, f"http_{status}")


def body(status: int, detail: Any, code: str | None = None) -> dict:
    """The problem document. `detail` is passed through unchanged."""
    slug = code or code_for(status, detail)
    title = detail if isinstance(detail, str) and detail else _BY_STATUS.get(
        status, "error")
    return {
        "type": f"/problems/{slug}",
        "title": title,
        "status": status,
        "code": slug,
        # Byte-for-byte what the route raised. Backward compatibility lives
        # here and nowhere else — do not "improve" it into a formatted string.
        "detail": detail,
    }


# The OpenAPI schema for the above, so `/openapi.json` describes the machine
# surface instead of leaving every 4xx as an undocumented blank.
SCHEMA: dict = {
    "type": "object",
    "properties": {
        "type": {"type": "string", "description": "Relative URI naming the problem type."},
        "title": {"type": "string"},
        "status": {"type": "integer"},
        "code": {"type": "string",
                 "description": "Stable slug. Branch on THIS, never on the prose."},
        "detail": {"description": "The human-readable message. Unchanged from "
                                  "before problem documents existed."},
    },
    "required": ["type", "title", "status", "code", "detail"],
}


def _response(status: int, description: str) -> dict:
    return {
        "description": description,
        "content": {
            MEDIA_TYPE: {"schema": SCHEMA},
            "application/json": {
                "schema": {"type": "object",
                           "properties": {"detail": {"type": "string"}}},
            },
        },
    }


# Reusable `responses=` fragments for route decorators. Documenting what a
# route REFUSES is the half of an API reference an agent actually needs, and it
# was entirely absent: every machine-inbound route advertised a 200 and a
# validation error, and nothing else.
SAFE_MODE = {403: _response(403, "Safe Mode: this caller holds no full session.")}
NOT_FOUND = {404: _response(404, "No such thread, bot, message or reaction.")}
BAD_INPUT = {422: _response(422, "The request body failed validation.")}
UNAUTHORIZED = {401: _response(401, "A remote caller needs X-API-Key.")}
TOO_LARGE = {413: _response(413, "The upload exceeds the configured limit.")}
RATE_LIMITED = {429: _response(429, "Rate limited; the refusal is final, do not retry.")}

# The set almost every machine-inbound route can produce.
MACHINE = {**UNAUTHORIZED, **SAFE_MODE, **NOT_FOUND, **BAD_INPUT}
