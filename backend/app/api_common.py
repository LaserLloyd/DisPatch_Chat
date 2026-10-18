"""Small helpers the W5 routers (settings, connections, characters, backups, wizard) share.

``fail`` builds an HTTPException that carries a catalogued ``error.code`` (the handler in
``main.py`` turns ``exc.code`` into the ``error`` envelope), ``problem`` returns the same
envelope as a response for a handler that wants a body of its own, and ``audit`` writes the
admin-action row the People tab shows.
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

from . import accounts, policy
from .contracts import errors


def fail(status: int, code: str, message: str) -> HTTPException:
    exc = HTTPException(status, message)
    exc.code = code                       # type: ignore[attr-defined]
    return exc


def problem(status: int, code: str, message: str, **extra: Any) -> JSONResponse:
    body = {"detail": message, "error": errors.envelope(code, message=message), **extra}
    return JSONResponse(body, status_code=status)


def caller(request: Request) -> policy.Principal:
    return policy.of(request).principal


def audit(request: Request, kind: str, target: str, detail: str = "") -> None:
    accounts.log_activity(kind, profile_id=caller(request).profile_id, subject=target,
                          detail=detail, category="audit")


def actor(request: Request) -> str:
    p = caller(request)
    return p.profile_id or ("machine" if p.is_machine else "")
