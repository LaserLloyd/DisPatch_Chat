"""OIDC sign-in routes (4.3, issue #24). The protocol is ``oidc.py``; the policy rows are in
``route_policy.py`` between the ``4.3 A13`` anchors.

    GET  /api/oidc/start            public: begin a sign-in (or, with ``link=<profile id>``, a
                                    link for a signed-in profile) - sets the Lax state cookie
                                    and redirects to the provider
    GET  /api/oidc/callback         public: the provider's redirect back; signs in the LINKED
                                    profile (or saves the link) and lands on the app
    GET  /api/oidc/config           the protected Admin: the provider settings (never the secret)
    PUT  /api/oidc/config           the protected Admin: save them (secret -> secret store)
    GET  /api/oidc/links            the caller's own link; the protected Admin sees every link
    DELETE /api/oidc/links/{profile_id}   unlink: the profile itself or the protected Admin

Who may sign in is a host-level decision, so the config routes are the protected Admin's
alone (``host=True`` in the policy row, and checked here until WP-A14 enforces ``host``).
"""

from __future__ import annotations

import asyncio
import html
import logging
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from . import accounts, auth, auth_attempts, oidc, people_api, policy
from .contracts import errors

log = logging.getLogger("local-chat.oidc")
router = APIRouter()


# --------------------------------------------------------------------------- #
# Plumbing
# --------------------------------------------------------------------------- #


def _problem(status: int, code: str, message: str | None = None, **extra: Any) -> JSONResponse:
    env = errors.envelope(code, status=status, message=message)
    return JSONResponse({"detail": env["message"], "error": env, **extra}, status_code=status)


def _wants_html(request: Request) -> bool:
    return "text/html" in (request.headers.get("accept") or "")


def _page(status: int, title: str, body: str, *, refresh_to: str | None = None) -> HTMLResponse:
    """A tiny self-contained page for the browser that followed the provider's redirect."""
    meta = f'<meta http-equiv="refresh" content="0;url={html.escape(refresh_to)}">' if refresh_to else ""
    doc = (f'<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" '
           f'content="width=device-width,initial-scale=1">{meta}<title>{html.escape(title)}</title></head>'
           f'<body style="font-family:system-ui,sans-serif;max-width:32rem;margin:3rem auto;padding:0 1rem">'
           f'<p>{html.escape(body)}</p><p><a href="/">DisPatch</a></p></body></html>')
    resp = HTMLResponse(doc, status_code=status)
    resp.headers["Content-Security-Policy"] = "default-src 'none'; frame-ancestors 'none'"
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["Referrer-Policy"] = "no-referrer"
    return resp


def _drop_state_cookie(resp: Response) -> None:
    resp.delete_cookie(oidc.STATE_COOKIE, path="/", secure=True, httponly=True, samesite="lax")


def _callback_fail(request: Request, code: str, status: int = 401, **extra: Any) -> Response:
    spec = errors.get(code)
    msg = spec.message if spec else "Sign-in with the outside provider did not work."
    if _wants_html(request):
        resp: Response = _page(status, "DisPatch", msg)
    else:
        resp = _problem(status, code, **extra)
    _drop_state_cookie(resp)
    if extra.get("retry_after"):
        resp.headers["Retry-After"] = str(extra["retry_after"])
    return resp


def _principal(request: Request) -> policy.Principal:
    return policy.of(request).principal


def _session_principal(request: Request) -> policy.Principal | None:
    """The caller when it is a real signed-in profile session (not a lock-target landing, a
    machine or an open install)."""
    pr = _principal(request)
    if pr.kind != "profile" or pr.via != "cookie" or not pr.session_token or not pr.profile_id:
        return None
    return pr


def _same_origin_as_public(request: Request, base: str) -> bool:
    from urllib.parse import urlsplit
    pub = urlsplit(base)
    host = (request.headers.get("host") or "").lower()
    pub_host = (pub.netloc or "").lower()
    scheme_ok = (pub.scheme == "https") == policy.request_is_https(request)
    return host == pub_host and scheme_ok


def _protected_only(request: Request) -> JSONResponse | None:
    pr = _principal(request)
    if pr.kind == "profile" and pr.is_protected:
        return None
    return _problem(403, "permission_denied", required_permission="admin_system")


# --------------------------------------------------------------------------- #
# Start and callback (public)
# --------------------------------------------------------------------------- #


@router.get("/api/oidc/start")
async def oidc_start(request: Request, link: str | None = None, remember: int = 0):
    cfg = oidc.load_config()
    base = oidc.public_base()
    if not oidc.is_enabled(cfg) or base is None or not policy.claimed():
        return _problem(404, "oidc_disabled")
    if not _same_origin_as_public(request, base):
        # The state cookie must be set on the origin the provider redirects back to.
        q = request.url.query
        return RedirectResponse(f"{base}/api/oidc/start" + (f"?{q}" if q else ""), status_code=303)

    # Public and secret-free, but each call fills a slot in the 512-entry state table:
    # one address may start 30 sign-ins a minute (4.3 Fable). "This computer" is exempt.
    client = getattr(request, "client", None)
    if not policy.is_loopback_origin(client.host if client else "", policy.proxied_request(request)):
        wait = auth_attempts.rate_ok("oidc_start:" + policy.request_quota_ip(request))
        if wait:
            secs = int(wait) + 1
            resp = _problem(429, "too_many_attempts", f"Too many sign-in tries. Wait {secs} seconds, then try again.",
                            retry_after=secs)
            resp.headers["Retry-After"] = str(secs)
            return resp

    mode, target, initiator, token = "signin", None, None, None
    if link is not None:
        pr = _session_principal(request)
        if pr is None:
            return _problem(401, "signin_required")
        prof = accounts.get_profile(link)
        allowed = prof is not None and (pr.profile_id == prof.id or pr.is_protected)
        if not allowed:
            return _problem(404, "profile_not_found")       # never say whether it exists
        assert prof is not None
        if prof.disabled or policy.is_lock_target(prof):
            return _problem(403, "oidc_link_forbidden")
        mode, target, initiator, token = "link", prof.id, pr.profile_id, pr.session_token

    rec = oidc.new_state(mode=mode, issuer=cfg.issuer, profile_id=target, initiator=initiator,
                         initiator_token=token, remember=bool(remember))
    try:
        url = await asyncio.to_thread(oidc.authorize_url, rec, cfg)
    except oidc.OidcError as e:
        oidc.take_state(rec.id)
        log.warning("oidc: start failed: %s", e.reason)
        return _problem(502, "oidc_provider_unreachable")
    resp = RedirectResponse(url, status_code=303)
    resp.set_cookie(oidc.STATE_COOKIE, rec.id, max_age=oidc.STATE_TTL, path="/", secure=True,
                    httponly=True, samesite="lax")
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["Referrer-Policy"] = "no-referrer"
    return resp


class _Outcome:
    """What the verifier found, filled inside guarded_verify's callback."""

    def __init__(self) -> None:
        self.rec: oidc.StateRecord | None = None
        self.claims: dict | None = None
        self.profile: accounts.Profile | None = None
        self.code = "oidc_failed"
        self.reason = ""


def _verify(outcome: _Outcome, state_id: str | None, returned_state: str | None,
            code: str | None, idp_error: str | None, cfg: oidc.OidcConfig) -> bool:
    rec = oidc.take_state(state_id)                    # single use, whatever happens next
    outcome.rec = rec
    if rec is None:
        outcome.reason = "no state record (missing cookie, replayed or expired)"
        return False
    if not oidc.state_matches(rec, returned_state):
        outcome.reason = "state mismatch"
        return False
    if idp_error:
        outcome.reason = f"provider said {idp_error[:40]!r}"
        return False
    try:
        claims = oidc.exchange_and_verify(rec, code or "", cfg)
    except oidc.OidcError as e:
        outcome.reason = e.reason
        return False
    outcome.claims = claims
    if rec.mode == "link":
        # The session that started the link must still be alive and still be that profile.
        sess = auth.get_session(rec.initiator_token)
        if sess is None or sess.profile_id != rec.initiator:
            outcome.reason = "the session that started the link has ended"
            return False
        prof = accounts.get_profile(rec.profile_id)
        initiator = accounts.get_profile(rec.initiator)
        if prof is None or prof.disabled or initiator is None or initiator.disabled:
            outcome.reason = "profile gone or disabled"
            return False
        if initiator.id != prof.id and not initiator.protected:
            outcome.reason = "initiator may not link this profile"
            return False
        outcome.profile = prof
        return True
    found = accounts.find_oidc_link(cfg.issuer, claims["sub"])
    if found is None:
        outcome.code = "oidc_not_linked"
        outcome.reason = "no profile is linked to this subject"
        return False
    prof = accounts.get_profile(found.profile_id)
    if prof is None or prof.disabled:
        outcome.reason = "linked profile is gone or disabled"
        return False
    outcome.profile = prof
    return True


@router.get("/api/oidc/callback")
async def oidc_callback(request: Request, state: str | None = None, code: str | None = None,
                        error: str | None = None):
    cfg = oidc.load_config()
    if not oidc.is_enabled(cfg) or not policy.claimed():
        return _callback_fail(request, "oidc_disabled", 404)
    state_id = request.cookies.get(oidc.STATE_COOKIE)
    # The throttle subject: the profile being linked, or the install-wide OIDC sign-in key.
    # A failing provider subject is then throttled exactly like a wrong PIN (per device,
    # per address, global ceiling: auth_attempts).
    peek = oidc.peek_mode(state_id)
    subject = f"oidc-link:{peek[1]}" if peek is not None and peek[0] == "link" else "oidc"
    outcome = _Outcome()
    try:
        ok = await asyncio.to_thread(
            auth_attempts.guarded_verify, request, scope="oidc", subject=subject,
            verify=lambda: _verify(outcome, state_id, state, code, error, cfg))
    except auth_attempts.AttemptRefused as e:
        return _callback_fail(request, e.code, e.status, retry_after=max(1, int(e.retry_after)))
    ip = policy.request_quota_ip(request)
    if not ok or outcome.profile is None or outcome.rec is None:
        log.info("oidc: refused (%s)", outcome.reason)
        accounts.log_activity("oidc_failed", subject=ip, detail=outcome.reason[:80], category="audit")
        return _callback_fail(request, outcome.code)

    rec, prof, claims = outcome.rec, outcome.profile, outcome.claims or {}
    if rec.mode == "link":
        email = claims.get("email") if isinstance(claims.get("email"), str) else ""
        try:
            accounts.add_oidc_link(prof.id, cfg.issuer, claims["sub"], email)
        except accounts.AccountError as e:
            accounts.log_activity("oidc_link_refused", profile_id=rec.initiator, subject=prof.id,
                                  detail=e.code, category="audit")
            return _callback_fail(request, e.code, e.status)
        accounts.log_activity("oidc_linked", profile_id=rec.initiator, subject=prof.id, category="audit")
        resp: Response = _page(200, "DisPatch", "Linked. Taking you back to DisPatch.",
                               refresh_to="/?oidc=linked")
        _drop_state_cookie(resp)
        return resp

    # Sign-in: a normal session for the linked profile (SameSite=Strict, as every session).
    cfg_auth = auth.load()
    persistent = rec.remember and cfg_auth.remember_days > 0
    body = {"ok": True, "profile": people_api.public_profile(prof), "role": prof.role.value,
            "protected": prof.protected}
    issued = people_api.session_response(request, body, prof, persistent=persistent)
    if issued.status_code != 200:
        # No session was minted (e.g. 429 too_many_sessions): say so instead of "Signed in".
        resp = _page(issued.status_code, "DisPatch",
                     "Could not sign in: this profile has too many devices signed in. Sign out "
                     "on another device, then try again.")
        _drop_state_cookie(resp)
        return resp
    accounts.log_activity("login", profile_id=prof.id, subject=ip, detail="oidc")
    # Land through a same-origin page: the browser then sends the Strict session cookie on
    # the app's own requests (a redirect straight from the provider's chain may not).
    resp = _page(200, "DisPatch", "Signed in. Taking you to DisPatch.", refresh_to="/")
    for k, v in issued.raw_headers:
        if k.lower() == b"set-cookie":
            resp.raw_headers.append((k, v))
    _drop_state_cookie(resp)
    return resp


# --------------------------------------------------------------------------- #
# Config (the protected Admin)
# --------------------------------------------------------------------------- #


def _config_out() -> dict:
    cfg = oidc.load_config()
    return {"enabled": cfg.enabled, "active": oidc.is_enabled(cfg), "issuer": cfg.issuer,
            "client_id": cfg.client_id, "scopes": cfg.scopes, "label": cfg.label,
            "display_label": cfg.display_label(), "has_secret": oidc.has_client_secret(),
            "redirect_uri": oidc.redirect_uri(), "public_url_ok": oidc.public_base() is not None,
            "links": len(accounts.list_oidc_links())}


class OidcConfigIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    issuer: str = Field(default="", max_length=512)
    client_id: str = Field(default="", max_length=512)
    #: None or "" keeps the stored secret; ``clear_secret`` removes it.
    client_secret: str | None = Field(default=None, max_length=4096)
    clear_secret: bool = False
    scopes: str = Field(default=oidc.DEFAULT_SCOPES, max_length=512)
    label: str = Field(default="", max_length=60)


@router.get("/api/oidc/config")
async def oidc_config_get(request: Request):
    refused = _protected_only(request)
    if refused is not None:
        return refused
    return _config_out()


@router.put("/api/oidc/config")
async def oidc_config_put(request: Request, payload: OidcConfigIn):
    refused = _protected_only(request)
    if refused is not None:
        return refused
    issuer = payload.issuer.strip()
    client_id = payload.client_id.strip()
    scopes = " ".join(payload.scopes.split()) or oidc.DEFAULT_SCOPES
    label = " ".join(payload.label.split())
    if issuer:
        why = oidc.validate_issuer(issuer)
        if why:
            return _problem(422, "oidc_config_invalid", field="issuer", reason=why)
    if "openid" not in scopes.split(" "):
        return _problem(422, "oidc_config_invalid", field="scopes", reason="the scopes must include openid")
    if any(c in client_id for c in "\r\n\t "):
        return _problem(422, "oidc_config_invalid", field="client_id", reason="no spaces in a client id")
    if payload.enabled:
        if not issuer or not client_id:
            return _problem(422, "oidc_config_invalid", field="issuer" if not issuer else "client_id",
                            reason="an issuer and a client id are needed to turn it on")
        if oidc.public_base() is None:
            return _problem(422, "oidc_public_url_required")
        try:
            await asyncio.to_thread(oidc.discovery, issuer, refresh=True)
        except oidc.OidcError as e:
            return _problem(502, "oidc_provider_unreachable", reason=e.reason[:200])
    if payload.clear_secret:
        oidc.set_client_secret(None)
    elif payload.client_secret:
        try:
            oidc.set_client_secret(payload.client_secret.strip())
        except Exception:
            log.warning("oidc: the client secret could not be stored", exc_info=True)
            return _problem(500, "oidc_config_invalid", field="client_secret",
                            reason="the secret store did not keep the value")
    oidc.save_config(oidc.OidcConfig(enabled=payload.enabled, issuer=issuer, client_id=client_id,
                                     scopes=scopes, label=label))
    accounts.log_activity("oidc_config", profile_id=_principal(request).profile_id,
                          subject="on" if payload.enabled else "off", category="audit")
    return _config_out()


# --------------------------------------------------------------------------- #
# Links
# --------------------------------------------------------------------------- #


def _link_out(link: accounts.OidcLink) -> dict:
    return {"profile_id": link.profile_id, "issuer": link.issuer,
            "email_at_link": link.email_at_link, "created": link.created}


@router.get("/api/oidc/links")
async def oidc_links(request: Request):
    pr = _session_principal(request)
    if pr is None:
        return _problem(401, "signin_required")
    rows = accounts.list_oidc_links(None if pr.is_protected else pr.profile_id)
    return {"enabled": oidc.is_enabled(), "links": [_link_out(x) for x in rows]}


@router.delete("/api/oidc/links/{profile_id}")
async def oidc_unlink(request: Request, profile_id: str):
    pr = _session_principal(request)
    if pr is None:
        return _problem(401, "signin_required")
    prof = accounts.get_profile(profile_id)
    if prof is None or not (pr.profile_id == prof.id or pr.is_protected):
        return _problem(404, "profile_not_found")
    n = accounts.delete_oidc_links(prof.id)
    if n:
        accounts.log_activity("oidc_unlinked", profile_id=pr.profile_id, subject=prof.id, category="audit")
    return {"ok": True, "removed": n}
