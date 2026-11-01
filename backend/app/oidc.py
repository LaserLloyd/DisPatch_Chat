"""OIDC sign-in (4.3, issue #24): the protocol half. Routes live in ``oidc_api.py``.

Off until the protected Admin saves a config (``<data>/oidc.yaml``; the client secret goes to
the secret store under :data:`SECRET_REF`). What it does, and what it never does:

* Authorization code flow with PKCE (S256). ``state`` and ``nonce`` live in a short-lived
  (10 min), single-use, server-side record keyed by the ``__Host-`` state cookie. That cookie is
  ``SameSite=Lax`` on purpose - a Strict cookie is not sent on the IdP's cross-site redirect
  back to the callback - and carries nothing but the record id. The session cookie the callback
  sets keeps the app's normal ``SameSite=Strict``.
* Enabling needs an ``https`` ``DISPATCH_PUBLIC_URL`` (a ``__Host-`` cookie needs TLS); the
  redirect URI is computed from it. ``http://localhost`` / ``127.0.0.1`` is accepted only under
  pytest.
* The ID token: signature against the issuer's JWKS (cached, refetched on an unknown ``kid``,
  RSA / RSA-PSS / ECDSA only - ``alg: none`` and every HMAC algorithm are refused), ``iss``
  equal to the configured issuer byte for byte (a trailing slash is a different issuer), ``aud``
  containing the client id, ``azp`` equal to it when present (required with several audiences),
  ``exp`` / ``nbf`` / ``iat`` with one minute of leeway, and ``nonce``.
* Discovery, JWKS and the token endpoint are fetched only from the issuer's own host, over
  https, with redirects off and a size cap (no SSRF through a discovery document).
* Only an EXISTING link signs in (``accounts.oidc_links``): no auto-provisioning, no matching by
  email. Linking needs the profile's own session (or the protected Admin's) plus a completed IdP
  round trip. A disabled profile is refused.
* OIDC never replaces the protected Admin's local credential: every action that asks for the
  current PIN or password still asks for it, and the last local credential cannot be removed
  while a link exists (:func:`local_credential_guard`, invariant I11).
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import hmac
import json
import logging
import os
import secrets
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlsplit

import httpx
import yaml
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

from . import accounts, config

log = logging.getLogger("local-chat.oidc")

CONFIG_NAME = "oidc.yaml"
SECRET_REF = "oidc.client_secret"
STATE_COOKIE = "__Host-dispatch-oidc"
STATE_TTL = 600                    # seconds a started sign-in may take
LEEWAY = 60                        # clock skew allowed on exp / nbf / iat
CALLBACK_PATH = "/api/oidc/callback"
DEFAULT_SCOPES = "openid email profile"
_MAX_DOC_BYTES = 512 * 1024
_CACHE_TTL = 3600
_JWKS_REFETCH_MIN = 30.0           # an unknown kid refetches at most this often
_MAX_STATES = 512

#: JWS algorithm -> (key type, hash). Nothing else is ever accepted.
_ALGS: dict[str, tuple[str, Any]] = {
    "RS256": ("RSA", hashes.SHA256), "RS384": ("RSA", hashes.SHA384), "RS512": ("RSA", hashes.SHA512),
    "PS256": ("RSA-PSS", hashes.SHA256), "PS384": ("RSA-PSS", hashes.SHA384), "PS512": ("RSA-PSS", hashes.SHA512),
    "ES256": ("EC", hashes.SHA256), "ES384": ("EC", hashes.SHA384), "ES512": ("EC", hashes.SHA512),
}
_EC_CURVES = {"P-256": (ec.SECP256R1, 32), "P-384": (ec.SECP384R1, 48), "P-521": (ec.SECP521R1, 66)}
_ES_CURVE = {"ES256": "P-256", "ES384": "P-384", "ES512": "P-521"}


class OidcError(Exception):
    """A refused sign-in or config. ``code`` is an error-catalog code; ``reason`` is for the
    log and the audit line only (never shown to the person: it may name a claim)."""

    def __init__(self, code: str, reason: str, status: int = 401) -> None:
        super().__init__(reason)
        self.code = code
        self.reason = reason
        self.status = status


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #


@dataclass
class OidcConfig:
    enabled: bool = False
    issuer: str = ""
    client_id: str = ""
    scopes: str = DEFAULT_SCOPES
    label: str = ""                # the provider's name on the sign-in button

    def display_label(self) -> str:
        if self.label.strip():
            return self.label.strip()
        return urlsplit(self.issuer).hostname or "your provider"


def _config_path() -> Path:
    return Path(config.DATA_DIR) / CONFIG_NAME


def load_config() -> OidcConfig:
    try:
        raw = yaml.safe_load(_config_path().read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return OidcConfig()
    if not isinstance(raw, dict):
        return OidcConfig()
    return OidcConfig(enabled=raw.get("enabled") is True, issuer=str(raw.get("issuer") or ""),
                      client_id=str(raw.get("client_id") or ""),
                      scopes=str(raw.get("scopes") or DEFAULT_SCOPES), label=str(raw.get("label") or ""))


def save_config(cfg: OidcConfig) -> None:
    path = _config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    body = yaml.safe_dump({"enabled": bool(cfg.enabled), "issuer": cfg.issuer, "client_id": cfg.client_id,
                           "scopes": cfg.scopes, "label": cfg.label}, sort_keys=False)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write("# DisPatch outside sign-in (OIDC). Written by Settings; the client secret is in the\n"
                 "# secret store, never here.\n")
        fh.write(body)
    os.replace(tmp, path)
    _discovery_cache.clear()
    _jwks_cache.clear()


def client_secret() -> str | None:
    from . import secret_store
    try:
        return secret_store.get(SECRET_REF)
    except Exception:
        log.warning("oidc: the client secret could not be read", exc_info=True)
        return None


def set_client_secret(value: str | None) -> None:
    from . import secret_store
    if value:
        secret_store.put(SECRET_REF, value)
    else:
        secret_store.delete(SECRET_REF)


def has_client_secret() -> bool:
    return bool(client_secret())


def _under_pytest() -> bool:
    return "PYTEST_CURRENT_TEST" in os.environ


def public_base() -> str | None:
    """``DISPATCH_PUBLIC_URL`` without a trailing slash, when it can carry a ``__Host-`` cookie
    (https; plain http only for a loopback host under pytest). None otherwise."""
    raw = (config.env("PUBLIC_URL") or "").strip().rstrip("/")
    if not raw:
        return None
    try:
        u = urlsplit(raw)
    except ValueError:
        return None
    if not u.hostname or u.query or u.fragment or u.username or u.password:
        return None
    if u.scheme == "https":
        return raw
    if u.scheme == "http" and u.hostname in ("localhost", "127.0.0.1", "::1") and _under_pytest():
        return raw
    return None


def redirect_uri() -> str | None:
    base = public_base()
    return f"{base}{CALLBACK_PATH}" if base else None


def is_enabled(cfg: OidcConfig | None = None) -> bool:
    c = cfg or load_config()
    return bool(c.enabled and c.issuer and c.client_id and public_base())


def public_info() -> dict:
    """What the sign-in screen needs (``GET /api/auth/status`` carries it): no secret, no id."""
    cfg = load_config()
    on = is_enabled(cfg)
    return {"enabled": on, "label": cfg.display_label() if on else ""}


def validate_issuer(issuer: str) -> str | None:
    """None when ``issuer`` is acceptable, else why not."""
    try:
        u = urlsplit(issuer)
    except ValueError:
        return "not a URL"
    if u.scheme != "https" or not u.hostname:
        return "the issuer must be an https:// address"
    if u.query or u.fragment or u.username or u.password:
        return "the issuer must not carry a query, fragment or credentials"
    return None


# --------------------------------------------------------------------------- #
# Fetching (issuer host only, https, no redirects)
# --------------------------------------------------------------------------- #

#: Tests hand in an ``httpx.MockTransport`` here; None = the network.
_transport: httpx.BaseTransport | None = None
_discovery_cache: dict[str, tuple[float, dict]] = {}
_jwks_cache: dict[str, tuple[float, dict]] = {}
_cache_lock = threading.Lock()


def _origin(url: str) -> tuple[str, str, int]:
    u = urlsplit(url)
    return (u.scheme, (u.hostname or "").lower(), u.port or (443 if u.scheme == "https" else 80))


def _same_host(url: str, issuer: str) -> bool:
    o = _origin(url)
    return o[0] == "https" and o == _origin(issuer)


def _client() -> httpx.Client:
    return httpx.Client(transport=_transport, follow_redirects=False, timeout=httpx.Timeout(10.0),
                        headers={"Accept": "application/json", "User-Agent": "DisPatch-OIDC"})


def _read_json(resp: httpx.Response, what: str) -> dict:
    if resp.status_code != 200:
        raise OidcError("oidc_failed", f"{what}: HTTP {resp.status_code}", 401)
    body = resp.content
    if len(body) > _MAX_DOC_BYTES:
        raise OidcError("oidc_failed", f"{what}: response too large", 401)
    try:
        doc = json.loads(body)
    except ValueError as e:
        raise OidcError("oidc_failed", f"{what}: not JSON", 401) from e
    if not isinstance(doc, dict):
        raise OidcError("oidc_failed", f"{what}: not a JSON object", 401)
    return doc


def _get_json(url: str, issuer: str, what: str) -> dict:
    if not _same_host(url, issuer):
        raise OidcError("oidc_failed", f"{what}: {url!r} is not on the issuer's https host", 401)
    try:
        with _client() as c:
            resp = c.get(url)
    except httpx.HTTPError as e:
        raise OidcError("oidc_failed", f"{what}: {type(e).__name__}", 401) from e
    return _read_json(resp, what)


def discovery(issuer: str, *, refresh: bool = False) -> dict:
    """The issuer's OpenID configuration, checked: its ``issuer`` equals ours exactly, the
    authorization endpoint is https, and the token endpoint and JWKS sit on the issuer's host."""
    now = time.monotonic()
    with _cache_lock:
        hit = _discovery_cache.get(issuer)
    if hit and not refresh and now - hit[0] < _CACHE_TTL:
        return hit[1]
    url = issuer.rstrip("/") + "/.well-known/openid-configuration"
    doc = _get_json(url, issuer, "discovery")
    if doc.get("issuer") != issuer:
        raise OidcError("oidc_failed", "discovery: the document names a different issuer", 401)
    auth_ep = str(doc.get("authorization_endpoint") or "")
    if urlsplit(auth_ep).scheme != "https" or not urlsplit(auth_ep).hostname:
        raise OidcError("oidc_failed", "discovery: authorization_endpoint is not https", 401)
    for key in ("token_endpoint", "jwks_uri"):
        if not _same_host(str(doc.get(key) or ""), issuer):
            raise OidcError("oidc_failed", f"discovery: {key} is not on the issuer's https host", 401)
    with _cache_lock:
        _discovery_cache[issuer] = (now, doc)
    return doc


def _jwks(issuer: str, jwks_uri: str, *, refresh: bool = False) -> dict:
    now = time.monotonic()
    with _cache_lock:
        hit = _jwks_cache.get(jwks_uri)
    if hit and (now - hit[0] < _CACHE_TTL) and (not refresh or now - hit[0] < _JWKS_REFETCH_MIN):
        return hit[1]
    doc = _get_json(jwks_uri, issuer, "jwks")
    if not isinstance(doc.get("keys"), list):
        raise OidcError("oidc_failed", "jwks: no keys", 401)
    with _cache_lock:
        _jwks_cache[jwks_uri] = (now, doc)
    return doc


# --------------------------------------------------------------------------- #
# JWS verification (cryptography only; no JOSE library)
# --------------------------------------------------------------------------- #


def _b64d(s: str) -> bytes:
    if not isinstance(s, str) or any(c in s for c in "=+/ \n"):
        raise ValueError("not base64url")
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _b64int(s: str) -> int:
    return int.from_bytes(_b64d(s), "big")


def _public_key(jwk: dict, alg: str):
    kty, _ = _ALGS[alg]
    if jwk.get("use") not in (None, "sig"):
        raise ValueError("key is not for signatures")
    if jwk.get("alg") not in (None, alg):
        raise ValueError("key is for another algorithm")
    if kty in ("RSA", "RSA-PSS"):
        if jwk.get("kty") != "RSA":
            raise ValueError("not an RSA key")
        pub = rsa.RSAPublicNumbers(_b64int(jwk["e"]), _b64int(jwk["n"])).public_key()
        if pub.key_size < 2048:
            raise ValueError("RSA key too small")
        return pub
    if jwk.get("kty") != "EC" or jwk.get("crv") != _ES_CURVE[alg]:
        raise ValueError("not an EC key on the algorithm's curve")
    curve, _size = _EC_CURVES[jwk["crv"]]
    return ec.EllipticCurvePublicNumbers(_b64int(jwk["x"]), _b64int(jwk["y"]), curve()).public_key()


def _verify_signature(alg: str, key, signing_input: bytes, sig: bytes) -> bool:
    kty, h = _ALGS[alg]
    try:
        if kty == "RSA":
            key.verify(sig, signing_input, padding.PKCS1v15(), h())
        elif kty == "RSA-PSS":
            key.verify(sig, signing_input, padding.PSS(mgf=padding.MGF1(h()), salt_length=h.digest_size), h())
        else:
            size = _EC_CURVES[_ES_CURVE[alg]][1]
            if len(sig) != 2 * size:
                return False
            der = encode_dss_signature(int.from_bytes(sig[:size], "big"), int.from_bytes(sig[size:], "big"))
            key.verify(der, signing_input, ec.ECDSA(h()))
    except InvalidSignature:
        return False
    return True


def _pick_keys(jwks_doc: dict, kid: str | None, alg: str) -> list:
    out = []
    for jwk in jwks_doc.get("keys") or []:
        if not isinstance(jwk, dict):
            continue
        if kid is not None and jwk.get("kid") != kid:
            continue
        with contextlib.suppress(Exception):
            out.append(_public_key(jwk, alg))
    return out


def verify_id_token(token: str, *, issuer: str, client_id: str, nonce: str, jwks_uri: str,
                    now: float | None = None) -> dict:
    """The verified claims of ``token``, or :class:`OidcError` (code ``oidc_failed``)."""
    def bad(reason: str) -> OidcError:
        return OidcError("oidc_failed", f"id_token: {reason}", 401)

    if not isinstance(token, str) or token.count(".") != 2 or len(token) > 16384:
        raise bad("not a compact JWS")
    h64, p64, s64 = token.split(".")
    try:
        header = json.loads(_b64d(h64))
        claims = json.loads(_b64d(p64))
        sig = _b64d(s64)
    except (ValueError, TypeError) as e:
        raise bad("malformed") from e
    if not isinstance(header, dict) or not isinstance(claims, dict):
        raise bad("malformed")
    alg = header.get("alg")
    if not isinstance(alg, str) or alg not in _ALGS:
        raise bad(f"algorithm {alg!r} refused")       # none, HS*, anything unknown
    if header.get("crit") is not None:
        raise bad("critical header parameters are not supported")
    kid = header.get("kid") if isinstance(header.get("kid"), str) else None
    signing_input = f"{h64}.{p64}".encode("ascii")

    keys = _pick_keys(_jwks(issuer, jwks_uri), kid, alg)
    if not keys and kid is not None:
        keys = _pick_keys(_jwks(issuer, jwks_uri, refresh=True), kid, alg)   # key rotation
    if not keys:
        raise bad("no matching signing key")
    if not any(_verify_signature(alg, k, signing_input, sig) for k in keys):
        raise bad("bad signature")

    t = time.time() if now is None else now
    if claims.get("iss") != issuer:
        raise bad("iss differs from the configured issuer")
    aud = claims.get("aud")
    auds = [aud] if isinstance(aud, str) else aud if isinstance(aud, list) else None
    if not auds or not all(isinstance(a, str) for a in auds) or client_id not in auds:
        raise bad("aud does not name this client")
    azp = claims.get("azp")
    if (len(auds) > 1 or azp is not None) and azp != client_id:
        raise bad("azp does not name this client")
    exp = claims.get("exp")
    if not isinstance(exp, (int, float)) or isinstance(exp, bool) or t > exp + LEEWAY:
        raise bad("expired")
    nbf = claims.get("nbf")
    if nbf is not None and (not isinstance(nbf, (int, float)) or t + LEEWAY < nbf):
        raise bad("not yet valid")
    iat = claims.get("iat")
    if iat is not None and (not isinstance(iat, (int, float)) or iat > t + LEEWAY):
        raise bad("issued in the future")
    got = claims.get("nonce")
    if not isinstance(got, str) or not hmac.compare_digest(got.encode(), nonce.encode()):
        raise bad("nonce mismatch")
    sub = claims.get("sub")
    if not isinstance(sub, str) or not sub or len(sub) > 255:
        raise bad("no subject")
    return claims


# --------------------------------------------------------------------------- #
# The state record (server side; the cookie carries only its id)
# --------------------------------------------------------------------------- #


@dataclass
class StateRecord:
    id: str
    state: str
    nonce: str
    verifier: str
    mode: str                          # "signin" | "link"
    issuer: str
    profile_id: str | None = None      # link: the profile being linked
    initiator: str | None = None       # link: the profile whose session started it
    initiator_token: str | None = field(default=None, repr=False)   # link: that session
    remember: bool = False
    created: float = field(default_factory=time.monotonic)

    def expired(self, now: float | None = None) -> bool:
        return ((time.monotonic() if now is None else now) - self.created) > STATE_TTL


_states: OrderedDict[str, StateRecord] = OrderedDict()
_states_lock = threading.Lock()


def new_state(*, mode: str, issuer: str, profile_id: str | None = None, initiator: str | None = None,
              initiator_token: str | None = None, remember: bool = False) -> StateRecord:
    rec = StateRecord(id=secrets.token_urlsafe(32), state=secrets.token_urlsafe(32),
                      nonce=secrets.token_urlsafe(32), verifier=secrets.token_urlsafe(48), mode=mode,
                      issuer=issuer, profile_id=profile_id, initiator=initiator,
                      initiator_token=initiator_token, remember=bool(remember))
    with _states_lock:
        now = time.monotonic()
        for k in [k for k, r in _states.items() if r.expired(now)]:
            _states.pop(k, None)
        while len(_states) >= _MAX_STATES:
            _states.popitem(last=False)
        _states[rec.id] = rec
    return rec


def peek_mode(state_id: str | None) -> tuple[str, str | None] | None:
    """(mode, profile id) of a live record without using it up (the throttle's subject)."""
    with _states_lock:
        rec = _states.get(state_id or "")
    return (rec.mode, rec.profile_id) if rec is not None else None


def take_state(state_id: str | None) -> StateRecord | None:
    """Pop the record (single use). None when unknown, already used or expired."""
    if not state_id or len(state_id) > 128:
        return None
    with _states_lock:
        rec = _states.pop(state_id, None)
    if rec is None or rec.expired():
        return None
    return rec


def reset_for_tests() -> None:
    with _states_lock:
        _states.clear()
    with _cache_lock:
        _discovery_cache.clear()
        _jwks_cache.clear()


def _challenge(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode()


def authorize_url(rec: StateRecord, cfg: OidcConfig) -> str:
    doc = discovery(cfg.issuer)
    redirect = redirect_uri()
    if not redirect:
        raise OidcError("oidc_disabled", "no usable public URL", 404)
    q = {"response_type": "code", "client_id": cfg.client_id, "redirect_uri": redirect,
         "scope": cfg.scopes, "state": rec.state, "nonce": rec.nonce,
         "code_challenge": _challenge(rec.verifier), "code_challenge_method": "S256"}
    if rec.mode == "link":
        q["prompt"] = "login"          # linking proves the account now, not a stale IdP session
    ep = str(doc["authorization_endpoint"])
    return ep + ("&" if "?" in ep else "?") + urlencode(q)


def exchange_and_verify(rec: StateRecord, code: str, cfg: OidcConfig) -> dict:
    """Trade ``code`` for tokens at the issuer and return the verified ID token claims."""
    if rec.issuer != cfg.issuer:
        raise OidcError("oidc_failed", "the issuer changed while signing in", 401)
    if not code or len(code) > 4096:
        raise OidcError("oidc_failed", "no authorization code", 401)
    doc = discovery(cfg.issuer)
    token_ep = str(doc["token_endpoint"])
    form = {"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri() or "",
            "code_verifier": rec.verifier}
    auth_hdr = None
    secret = client_secret()
    methods = doc.get("token_endpoint_auth_methods_supported") or ["client_secret_basic"]
    if secret and "client_secret_basic" in methods:
        auth_hdr = (cfg.client_id, secret)
    else:
        form["client_id"] = cfg.client_id
        if secret:
            form["client_secret"] = secret
    if not _same_host(token_ep, cfg.issuer):
        raise OidcError("oidc_failed", "token endpoint is not on the issuer's host", 401)
    try:
        with _client() as c:
            resp = c.post(token_ep, data=form, auth=auth_hdr)
    except httpx.HTTPError as e:
        raise OidcError("oidc_failed", f"token: {type(e).__name__}", 401) from e
    body = _read_json(resp, "token")
    id_token = body.get("id_token")
    if not isinstance(id_token, str):
        raise OidcError("oidc_failed", "token: no id_token", 401)
    return verify_id_token(id_token, issuer=cfg.issuer, client_id=cfg.client_id, nonce=rec.nonce,
                           jwks_uri=str(doc["jwks_uri"]))


def state_matches(rec: StateRecord, returned: str | None) -> bool:
    return bool(returned) and hmac.compare_digest(rec.state.encode(), str(returned).encode())


# --------------------------------------------------------------------------- #
# The protected Admin keeps a local credential
# --------------------------------------------------------------------------- #


def local_credential_guard(profile: accounts.Profile | None, *, drop_pin: bool = False,
                           drop_password: bool = False) -> accounts.AccountError | None:
    """The refusal (422 ``oidc_last_credential``) when removing a credential would leave the
    protected Admin with no PIN and no password while an outside sign-in is linked to it; None
    when the change may go ahead. Callers that remove the protected Admin's PIN or password
    (``POST /api/auth/setup`` with an empty ``new_pin``, ``/api/auth/recover`` without one) ask
    this first. A full credential reset (``recovery.reset_owner``) drops the links instead."""
    if profile is None or not profile.protected:
        return None
    keeps_pin = profile.has_pin and not drop_pin
    keeps_password = profile.has_password and not drop_password
    if keeps_pin or keeps_password:
        return None
    try:
        linked = bool(accounts.list_oidc_links(profile.id))
    except Exception:
        log.warning("oidc: could not read the links (refusing the credential removal)", exc_info=True)
        linked = True
    if not linked:
        return None
    return accounts.AccountError(
        "oidc_last_credential",
        "Keep a PIN or password on the Owner profile while an outside sign-in is linked to it.", 422)
