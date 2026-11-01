"""4.3 WP-A13 (issue #24): OIDC sign-in against an in-process fake identity provider.

The provider is an ``httpx.MockTransport`` (discovery, JWKS, token endpoint) with a real RSA
key, so the whole path runs: the start redirect, the Lax ``__Host-`` state cookie, PKCE, the
token exchange, the JWS signature, every claim check and the session the callback issues. No
network, no real IdP.
"""
from __future__ import annotations

import base64
import hashlib
import json
import time
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives import hmac as chmac
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

from app import accounts, auth, invariants, oidc, policy

ISSUER = "https://idp.example"
CLIENT_ID = "dispatch-test"
SECRET = "fake-idp-" + "x" * 24     # scrub-ok: a test value for the in-process fake provider
PUBLIC = "https://testserver"


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _int_b64(n: int) -> str:
    return _b64(n.to_bytes((n.bit_length() + 7) // 8, "big"))


class FakeIdP:
    """Discovery + JWKS + token endpoint. ``next_claims`` / ``next_header`` / ``signer`` tweak
    the next ID token; ``codes`` maps an issued code to what the authorize step recorded."""

    def __init__(self, issuer: str = ISSUER):
        self.issuer = issuer
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.kid = "k1"
        self.ec_key = ec.generate_private_key(ec.SECP256R1())
        self.codes: dict[str, dict] = {}
        self.next_claims: dict = {}
        self.next_header: dict = {}
        self.signer = None            # callable(signing_input) -> signature bytes
        self.discovery_over: dict = {}
        self.requests: list[httpx.Request] = []
        self.extra_keys: list[dict] = []

    # -- documents ----------------------------------------------------------
    def jwks(self) -> dict:
        pub = self.key.public_key().public_numbers()
        keys = [{"kty": "RSA", "kid": self.kid, "use": "sig", "alg": "RS256",
                 "n": _int_b64(pub.n), "e": _int_b64(pub.e)}]
        en = self.ec_key.public_key().public_numbers()
        keys.append({"kty": "EC", "kid": "ec1", "crv": "P-256", "use": "sig",
                     "x": _b64(en.x.to_bytes(32, "big")), "y": _b64(en.y.to_bytes(32, "big"))})
        return {"keys": keys + self.extra_keys}

    def discovery(self) -> dict:
        d = {"issuer": self.issuer, "authorization_endpoint": f"{ISSUER}/authorize",
             "token_endpoint": f"{ISSUER}/token", "jwks_uri": f"{ISSUER}/jwks",
             "id_token_signing_alg_values_supported": ["RS256", "ES256"]}
        d.update(self.discovery_over)
        return d

    # -- tokens -------------------------------------------------------------
    def sign(self, header: dict, claims: dict) -> str:
        h = _b64(json.dumps(header).encode())
        p = _b64(json.dumps(claims).encode())
        si = f"{h}.{p}".encode()
        if self.signer is not None:
            sig = self.signer(si)
        elif header.get("alg") == "ES256":
            r, s = decode_dss_signature(self.ec_key.sign(si, ec.ECDSA(hashes.SHA256())))
            sig = r.to_bytes(32, "big") + s.to_bytes(32, "big")
        else:
            sig = self.key.sign(si, padding.PKCS1v15(), hashes.SHA256())
        return f"{h}.{p}.{_b64(sig)}"

    def issue_code(self, authorize_url: str, sub: str, **claims) -> str:
        q = {k: v[0] for k, v in parse_qs(urlsplit(authorize_url).query).items()}
        code = f"code-{len(self.codes)}-{sub}"
        self.codes[code] = {"sub": sub, "nonce": q["nonce"], "challenge": q["code_challenge"],
                            "redirect_uri": q["redirect_uri"], "client_id": q["client_id"], **claims}
        return code

    # -- transport ----------------------------------------------------------
    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = str(request.url)
        if url == f"{self.issuer.rstrip('/')}/.well-known/openid-configuration" or \
                url == f"{ISSUER}/.well-known/openid-configuration":
            return httpx.Response(200, json=self.discovery())
        if url == f"{ISSUER}/jwks":
            return httpx.Response(200, json=self.jwks())
        if url == f"{ISSUER}/token" and request.method == "POST":
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            rec = self.codes.pop(form.get("code", ""), None)        # codes are single use
            if rec is None:
                return httpx.Response(400, json={"error": "invalid_grant"})
            want = _b64(hashlib.sha256(form.get("code_verifier", "").encode()).digest())
            if want != rec["challenge"] or form.get("redirect_uri") != rec["redirect_uri"]:
                return httpx.Response(400, json={"error": "invalid_grant"})
            authz = request.headers.get("authorization", "")
            expect = "Basic " + base64.b64encode(f"{CLIENT_ID}:{SECRET}".encode()).decode()
            if authz != expect:
                return httpx.Response(401, json={"error": "invalid_client"})
            now = int(time.time())
            claims = {"iss": self.issuer, "aud": CLIENT_ID, "sub": rec["sub"], "nonce": rec["nonce"],
                      "iat": now, "exp": now + 300, "email": f"{rec['sub']}@example.org"}
            claims.update({k: v for k, v in rec.items()
                           if k not in ("sub", "nonce", "challenge", "redirect_uri", "client_id")})
            claims.update(self.next_claims)
            header = {"alg": "RS256", "kid": self.kid, "typ": "JWT", **self.next_header}
            return httpx.Response(200, json={"access_token": "at", "token_type": "Bearer",
                                             "id_token": self.sign(header, claims)})
        return httpx.Response(404)


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture
def idp(monkeypatch):
    fake = FakeIdP()
    monkeypatch.setattr(oidc, "_transport", httpx.MockTransport(fake.handler))
    oidc.reset_for_tests()
    yield fake
    oidc.reset_for_tests()


@pytest.fixture
def app_with_oidc(world, monkeypatch):
    """The real app (main.py mounts the OIDC router) with a public https URL and the file
    secret store."""
    monkeypatch.setenv("DISPATCH_PUBLIC_URL", PUBLIC)
    monkeypatch.setenv("DISPATCH_SECRET_STORE", "file")
    from app import secret_store
    secret_store.reset_for_tests()
    yield world
    secret_store.reset_for_tests()


def _client(world, addr=None):
    from fastapi.testclient import TestClient
    c = TestClient(world.main.app, base_url=PUBLIC, client=addr or world.LOOPBACK)
    c.__enter__()
    world._clients.append(c)
    c.headers.update({"Origin": PUBLIC})
    return c


@pytest.fixture
def owner(app_with_oidc):
    c = _client(app_with_oidc)
    r = c.post("/api/auth/setup", json={"name": "Olive", "password": "owner-pass1", "new_pin": "2468"})
    assert r.status_code == 200, r.text
    return c


def _configure(owner_client, **over):
    body = {"enabled": True, "issuer": ISSUER, "client_id": CLIENT_ID, "client_secret": SECRET,
            "label": "Example ID", **over}
    r = owner_client.put("/api/oidc/config", json=body)
    assert r.status_code == 200, r.text
    return r.json()


@pytest.fixture
def enabled(owner, idp):
    _configure(owner)
    return owner


def _start(c, **params):
    r = c.get("/api/oidc/start", params=params, follow_redirects=False)
    assert r.status_code == 303, r.text
    return r


def _callback(c, url: str, code: str, *, state: str | None = None, cookie: str | None = None):
    q = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
    headers = {"Cookie": f"{oidc.STATE_COOKIE}={cookie}"} if cookie else None
    return c.get("/api/oidc/callback", params={"state": state or q["state"], "code": code},
                 follow_redirects=False, headers=headers)


def _link(world, c, profile_id: str, idp: FakeIdP, sub: str):
    loc = _start(c, link=profile_id).headers["location"]
    r = _callback(c, loc, idp.issue_code(loc, sub))
    assert r.status_code == 200, r.text
    return r


def _me(c):
    return c.get("/api/auth/me").json()


def _signed_in_as(c):
    """The profile id of c's session, or None (no session cookie / not signed in)."""
    if not c.cookies.get(policy.SESSION_COOKIE):
        return None
    me = _me(c)
    return (me.get("profile") or {}).get("id")


def _kid(world, owner_client, name="Mia", role="user"):
    return world.person(owner_client, name, role, pin="1357")


def _signin_attempt(world, idp, sub, **claims):
    c = _client(world)
    loc = _start(c).headers["location"]
    return c, _callback(c, loc, idp.issue_code(loc, sub, **claims))


# --------------------------------------------------------------------------- #
# The round trip
# --------------------------------------------------------------------------- #


def test_full_round_trip_signs_in_the_linked_profile(app_with_oidc, enabled, idp):
    world = app_with_oidc
    kid = _kid(world, enabled)
    kc = _client(world)
    assert world.login(kc, profile_id=kid["id"], pin="1357").status_code == 200
    _link(world, kc, kid["id"], idp, "mia-sub")
    assert [x.profile_id for x in accounts.list_oidc_links()] == [kid["id"]]

    c, r = _signin_attempt(world, idp, "mia-sub")
    assert r.status_code == 200
    assert 'url=/"' in r.text                                    # lands on the app
    cookies = r.headers.get_list("set-cookie")
    sess = [x for x in cookies if x.startswith(f"{policy.SESSION_COOKIE}=")]
    assert sess and "samesite=strict" in sess[0].lower()           # the session stays Strict
    me = _me(c)
    assert me["profile"]["id"] == kid["id"]


def test_the_state_cookie_is_lax_host_prefixed_secure_and_httponly(app_with_oidc, enabled, idp):
    r = _start(_client(app_with_oidc))
    cookie = next(x for x in r.headers.get_list("set-cookie") if x.startswith(oidc.STATE_COOKIE + "="))
    low = cookie.lower()
    assert oidc.STATE_COOKIE.startswith("__Host-")
    assert "samesite=lax" in low and "secure" in low and "httponly" in low and "path=/" in low
    assert "domain=" not in low
    loc = urlsplit(r.headers["location"])
    q = parse_qs(loc.query)
    assert loc.netloc == "idp.example" and q["code_challenge_method"] == ["S256"]
    assert q["redirect_uri"] == [f"{PUBLIC}/api/oidc/callback"]


def test_bad_nonce_is_refused(app_with_oidc, enabled, idp):
    owner_id = accounts.get_owner().id
    _link(app_with_oidc, enabled, owner_id, idp, "olive-sub")
    idp.next_claims = {"nonce": "not-the-nonce"}
    c, r = _signin_attempt(app_with_oidc, idp, "olive-sub")
    assert r.status_code == 401 and r.json()["error"]["code"] == "oidc_failed"
    assert _signed_in_as(c) is None


def test_unknown_subject_is_refused_and_creates_no_profile(app_with_oidc, enabled, idp):
    before = len(accounts.list_profiles())
    _c, r = _signin_attempt(app_with_oidc, idp, "stranger-sub", email="olive@example.org")
    assert r.status_code == 401 and r.json()["error"]["code"] == "oidc_not_linked"
    assert len(accounts.list_profiles()) == before
    assert accounts.list_oidc_links() == []


def test_email_never_matches_a_profile(app_with_oidc, enabled, idp):
    """An unlinked account whose email equals a linked one's is still refused."""
    _link(app_with_oidc, enabled, accounts.get_owner().id, idp, "olive-sub")
    _c, r = _signin_attempt(app_with_oidc, idp, "other-sub", email="olive-sub@example.org")
    assert r.status_code == 401


@pytest.mark.parametrize("claims, why", [
    ({"exp": int(time.time()) - 3600}, "expired"),
    ({"aud": "someone-else"}, "wrong aud"),
    ({"aud": [CLIENT_ID, "other"]}, "several audiences, no azp"),
    ({"azp": "other"}, "azp names another client"),
    ({"iss": ISSUER + "/"}, "iss with a trailing slash"),
    ({"iat": int(time.time()) + 3600}, "issued in the future"),
])
def test_claim_checks_refuse(app_with_oidc, enabled, idp, claims, why):
    _link(app_with_oidc, enabled, accounts.get_owner().id, idp, "olive-sub")
    idp.next_claims = claims
    _c, r = _signin_attempt(app_with_oidc, idp, "olive-sub")
    assert r.status_code == 401, why


def test_replayed_state_is_refused(app_with_oidc, enabled, idp):
    _link(app_with_oidc, enabled, accounts.get_owner().id, idp, "olive-sub")
    c = _client(app_with_oidc)
    r0 = _start(c)
    loc = r0.headers["location"]
    state_cookie = c.cookies.get(oidc.STATE_COOKIE)
    assert state_cookie
    assert _callback(c, loc, idp.issue_code(loc, "olive-sub")).status_code == 200
    # The same state again, with the cookie put back (an attacker who captured both).
    c2 = _client(app_with_oidc)
    r = _callback(c2, loc, idp.issue_code(loc, "olive-sub"), cookie=state_cookie)
    assert r.status_code == 401
    assert _signed_in_as(c2) is None


def test_callback_without_the_state_cookie_is_refused(app_with_oidc, enabled, idp):
    _link(app_with_oidc, enabled, accounts.get_owner().id, idp, "olive-sub")
    c = _client(app_with_oidc)
    loc = _start(c).headers["location"]
    other = _client(app_with_oidc)                    # a different browser: no state cookie
    r = _callback(other, loc, idp.issue_code(loc, "olive-sub"))
    assert r.status_code == 401


def test_a_disabled_profile_is_refused(app_with_oidc, enabled, idp):
    world = app_with_oidc
    kid = _kid(world, enabled)
    kc = _client(world)
    world.login(kc, profile_id=kid["id"], pin="1357")
    _link(world, kc, kid["id"], idp, "mia-sub")
    assert enabled.patch(f"/api/people/{kid['id']}", json={"disabled": True}).status_code == 200
    _c, r = _signin_attempt(world, idp, "mia-sub")
    assert r.status_code == 401


def test_hs256_signed_with_the_client_secret_is_refused(app_with_oidc, enabled, idp):
    _link(app_with_oidc, enabled, accounts.get_owner().id, idp, "olive-sub")

    def hs(si: bytes) -> bytes:
        h = chmac.HMAC(SECRET.encode(), hashes.SHA256())
        h.update(si)
        return h.finalize()
    idp.next_header = {"alg": "HS256"}
    idp.signer = hs
    _c, r = _signin_attempt(app_with_oidc, idp, "olive-sub")
    assert r.status_code == 401


def test_alg_none_is_refused(app_with_oidc, enabled, idp):
    _link(app_with_oidc, enabled, accounts.get_owner().id, idp, "olive-sub")
    idp.next_header = {"alg": "none"}
    idp.signer = lambda si: b""
    _c, r = _signin_attempt(app_with_oidc, idp, "olive-sub")
    assert r.status_code == 401


def test_a_forged_signature_is_refused(app_with_oidc, enabled, idp):
    _link(app_with_oidc, enabled, accounts.get_owner().id, idp, "olive-sub")
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    idp.signer = lambda si: other.sign(si, padding.PKCS1v15(), hashes.SHA256())
    _c, r = _signin_attempt(app_with_oidc, idp, "olive-sub")
    assert r.status_code == 401


def test_es256_is_accepted(app_with_oidc, enabled, idp):
    _link(app_with_oidc, enabled, accounts.get_owner().id, idp, "olive-sub")
    idp.next_header = {"alg": "ES256", "kid": "ec1"}
    c, r = _signin_attempt(app_with_oidc, idp, "olive-sub")
    assert r.status_code == 200
    assert _me(c)["profile"]["protected"] is True


def test_an_unknown_kid_refetches_the_jwks(app_with_oidc, enabled, idp, monkeypatch):
    monkeypatch.setattr(oidc, "_JWKS_REFETCH_MIN", 0.0)
    _link(app_with_oidc, enabled, accounts.get_owner().id, idp, "olive-sub")
    idp.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)   # rotation
    idp.kid = "k2"
    _c, r = _signin_attempt(app_with_oidc, idp, "olive-sub")
    assert r.status_code == 200


def test_failures_are_throttled(app_with_oidc, enabled, idp):
    statuses = []
    for _ in range(8):
        _c, r = _signin_attempt(app_with_oidc, idp, "nobody-sub")
        statuses.append(r.status_code)
    assert 429 in statuses, statuses


# --------------------------------------------------------------------------- #
# Linking
# --------------------------------------------------------------------------- #


def test_linking_needs_a_signed_in_session(app_with_oidc, enabled, idp):
    r = _client(app_with_oidc).get("/api/oidc/start", params={"link": accounts.get_owner().id},
                                   follow_redirects=False)
    assert r.status_code == 401


def test_a_user_cannot_link_another_profile(app_with_oidc, enabled, idp):
    world = app_with_oidc
    kid = _kid(world, enabled)
    kc = _client(world)
    world.login(kc, profile_id=kid["id"], pin="1357")
    r = kc.get("/api/oidc/start", params={"link": accounts.get_owner().id}, follow_redirects=False)
    assert r.status_code == 404
    other = _kid(world, enabled, name="Noah")
    assert kc.delete(f"/api/oidc/links/{other['id']}").status_code == 404


def test_a_non_protected_admin_cannot_link_another_profile(app_with_oidc, enabled, idp):
    world = app_with_oidc
    adm = world.person(enabled, "Ada", "admin", password="ada-pass-12")
    kid = _kid(world, enabled)
    ac = _client(world)
    assert world.login(ac, profile_id=adm["id"], password="ada-pass-12").status_code == 200
    r = ac.get("/api/oidc/start", params={"link": kid["id"]}, follow_redirects=False)
    assert r.status_code == 404


def test_the_protected_admin_links_and_unlinks_a_profile(app_with_oidc, enabled, idp):
    world = app_with_oidc
    kid = _kid(world, enabled)
    _link(world, enabled, kid["id"], idp, "mia-sub")
    rows = enabled.get("/api/oidc/links").json()["links"]
    assert [r["profile_id"] for r in rows] == [kid["id"]] and "subject" not in rows[0]
    assert enabled.delete(f"/api/oidc/links/{kid['id']}").json()["removed"] == 1
    _c, r = _signin_attempt(world, idp, "mia-sub")
    assert r.status_code == 401


def test_one_outside_account_links_one_profile(app_with_oidc, enabled, idp):
    world = app_with_oidc
    kid = _kid(world, enabled)
    _link(world, enabled, kid["id"], idp, "shared-sub")
    loc = _start(enabled, link=accounts.get_owner().id).headers["location"]
    r = _callback(enabled, loc, idp.issue_code(loc, "shared-sub"))
    assert r.status_code == 409
    assert [x.profile_id for x in accounts.list_oidc_links()] == [kid["id"]]


def test_link_fails_when_the_starting_session_ended(app_with_oidc, enabled, idp):
    world = app_with_oidc
    kid = _kid(world, enabled)
    kc = _client(world)
    world.login(kc, profile_id=kid["id"], pin="1357")
    loc = _start(kc, link=kid["id"]).headers["location"]
    kc.post("/api/auth/lock")
    r = _callback(kc, loc, idp.issue_code(loc, "mia-sub"))
    assert r.status_code == 401 and accounts.list_oidc_links() == []


def test_deleting_a_profile_deletes_its_links(app_with_oidc, enabled, idp):
    world = app_with_oidc
    kid = _kid(world, enabled)
    _link(world, enabled, kid["id"], idp, "mia-sub")
    accounts.delete_profile(kid["id"])
    assert accounts.list_oidc_links() == []


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #


def test_off_by_default(app_with_oidc, owner):
    r = _client(app_with_oidc).get("/api/oidc/start", follow_redirects=False)
    assert r.status_code == 404 and r.json()["error"]["code"] == "oidc_disabled"
    assert oidc.public_info() == {"enabled": False, "label": ""}


def test_config_is_the_protected_admins_and_never_returns_the_secret(app_with_oidc, enabled, idp):
    world = app_with_oidc
    out = enabled.get("/api/oidc/config").json()
    assert out["active"] and out["has_secret"] and SECRET not in json.dumps(out)
    assert out["redirect_uri"] == f"{PUBLIC}/api/oidc/callback"
    assert SECRET not in (world.tmp / oidc.CONFIG_NAME).read_text()
    adm = world.person(enabled, "Ada", "admin", password="ada-pass-12")
    ac = _client(world)
    world.login(ac, profile_id=adm["id"], password="ada-pass-12")
    assert ac.get("/api/oidc/config").status_code == 403
    assert ac.put("/api/oidc/config", json={"enabled": False}).status_code == 403
    assert oidc.public_info() == {"enabled": True, "label": "Example ID"}


def test_enabling_needs_an_https_public_url(app_with_oidc, owner, idp, monkeypatch):
    monkeypatch.setenv("DISPATCH_PUBLIC_URL", "http://chat.example.net")
    r = owner.put("/api/oidc/config", json={"enabled": True, "issuer": ISSUER, "client_id": CLIENT_ID})
    assert r.status_code == 422 and r.json()["error"]["code"] == "oidc_public_url_required"
    monkeypatch.delenv("DISPATCH_PUBLIC_URL")
    r = owner.put("/api/oidc/config", json={"enabled": True, "issuer": ISSUER, "client_id": CLIENT_ID})
    assert r.status_code == 422


def test_an_http_issuer_is_refused(app_with_oidc, owner, idp):
    r = owner.put("/api/oidc/config", json={"enabled": True, "issuer": "http://idp.example",
                                            "client_id": CLIENT_ID})
    assert r.status_code == 422 and r.json()["error"]["code"] == "oidc_config_invalid"


def test_discovery_naming_another_issuer_is_refused(app_with_oidc, owner, idp):
    idp.discovery_over = {"issuer": "https://evil.example"}
    r = owner.put("/api/oidc/config", json={"enabled": True, "issuer": ISSUER, "client_id": CLIENT_ID})
    assert r.status_code == 502


def test_a_jwks_on_another_host_is_refused(app_with_oidc, owner, idp):
    idp.discovery_over = {"jwks_uri": "https://169.254.169.254/latest"}
    r = owner.put("/api/oidc/config", json={"enabled": True, "issuer": ISSUER, "client_id": CLIENT_ID})
    assert r.status_code == 502
    assert not any("169.254" in str(q.url) for q in idp.requests)


def test_start_from_another_origin_redirects_to_the_public_address(app_with_oidc, enabled, idp):
    from fastapi.testclient import TestClient
    c = TestClient(app_with_oidc.main.app, base_url="http://192.0.2.20:8765",
                   client=app_with_oidc.LOOPBACK)
    with c:
        r = c.get("/api/oidc/start", params={"remember": 1}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"{PUBLIC}/api/oidc/start?remember=1"
    assert oidc.STATE_COOKIE not in r.headers.get("set-cookie", "")


# --------------------------------------------------------------------------- #
# The protected Admin keeps a local credential (I11)
# --------------------------------------------------------------------------- #


def test_guard_refuses_dropping_the_last_local_credential(app_with_oidc, idp):
    world = app_with_oidc
    c = _client(world)
    assert c.post("/api/auth/setup", json={"new_pin": "2468"}).status_code == 200    # PIN-only Owner
    _configure(c)
    owner_row = accounts.get_owner()
    assert oidc.local_credential_guard(owner_row, drop_pin=True) is None             # no link yet
    _link(world, c, owner_row.id, idp, "olive-sub")
    err = oidc.local_credential_guard(accounts.get_owner(), drop_pin=True)
    assert err is not None and err.status == 422 and err.code == "oidc_last_credential"
    assert oidc.local_credential_guard(accounts.get_owner(), drop_password=True) is None


def test_owner_cannot_remove_the_last_pin_while_linked(app_with_oidc, idp):
    world = app_with_oidc
    c = _client(world)
    assert c.post("/api/auth/setup", json={"new_pin": "2468"}).status_code == 200
    _configure(c)
    _link(world, c, accounts.get_owner().id, idp, "olive-sub")
    r = c.post("/api/auth/setup", json={"new_pin": "", "current_pin": "2468"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "oidc_last_credential"
    assert auth.load().pin_set


def test_recovery_cannot_remove_the_last_pin_while_linked(app_with_oidc, idp):
    """INT-1: /api/auth/recover without a new PIN asks the same guard before clear_pin."""
    import re
    world = app_with_oidc
    c = _client(world)
    assert c.post("/api/auth/setup", json={"new_pin": "2468"}).status_code == 200
    _configure(c)
    _link(world, c, accounts.get_owner().id, idp, "olive-sub")
    code = re.search(r"LC-[A-Z0-9-]+", (world.tmp / "RECOVERY-CODE.txt").read_text()).group(0)
    r = _client(world).post("/api/auth/recover", json={"code": code})
    assert r.status_code == 422 and r.json()["error"]["code"] == "oidc_last_credential", r.text
    assert auth.load().pin_set and auth.verify_pin("2468")
    assert accounts.list_oidc_links(accounts.get_owner().id)


def test_reset_owner_drops_the_owners_outside_sign_ins(app_with_oidc, enabled, idp):
    """INT-1: a full credential reset removes the Owner's OIDC links (I11 stays quiet)."""
    from app import recovery
    owner_id = accounts.get_owner().id
    _link(app_with_oidc, enabled, owner_id, idp, "olive-sub")
    assert accounts.list_oidc_links(owner_id)
    recovery.reset_owner(via="test")
    assert accounts.list_oidc_links(owner_id) == []
    assert not [v for v in invariants.check() if v.code == "I11"]


def test_auth_status_carries_the_public_oidc_info(app_with_oidc, enabled):
    """INT-1: the signed-out sign-in screen learns {enabled, label}, never the secret."""
    r = _client(app_with_oidc).get("/api/auth/status")
    assert r.status_code == 200
    assert r.json()["oidc"] == {"enabled": True, "label": "Example ID"}
    assert SECRET not in r.text and CLIENT_ID not in r.text


def test_i11_reports_a_forced_violation():
    admin = accounts.Profile(id="adm", name="A", role="owner")          # no PIN, no password
    kid = accounts.Profile(id="kid", name="K", role="member", pin_hash="x")
    w = invariants.World(profiles=[admin, kid], admin="adm",
                         extra={"oidc_links": [{"profile_id": "adm", "issuer": ISSUER, "subject": "s"},
                                               {"profile_id": "gone", "issuer": ISSUER, "subject": "t"}]})
    vs = [v for v in invariants.check(w) if v.code == "I11"]
    assert {v.subject for v in vs} == {"adm", "gone"}
    ok = invariants.World(profiles=[accounts.Profile(id="adm", name="A", role="owner", password_hash="h"), kid],
                          admin="adm", extra={"oidc_links": [{"profile_id": "adm", "issuer": ISSUER, "subject": "s"}]})
    assert not [v for v in invariants.check(ok) if v.code == "I11"]


def test_i11_reads_live_links(app_with_oidc, enabled, idp):
    _link(app_with_oidc, enabled, accounts.get_owner().id, idp, "olive-sub")
    assert not [v for v in invariants.check() if v.code == "I11"]
    with accounts.connect() as con:                     # a hand edit / restored backup
        con.execute("PRAGMA foreign_keys=OFF")
        con.execute("INSERT INTO oidc_links (profile_id, issuer, subject, created) VALUES ('ghost', ?, 'z', 'now')",
                    (ISSUER,))
    assert [v.subject for v in invariants.check() if v.code == "I11"] == ["ghost"]


def test_rv43_a_refused_session_is_not_reported_as_signed_in(app_with_oidc, enabled, idp, monkeypatch):
    """4.3 review: when session_response refuses (429 too_many_sessions) the callback must not
    answer "Signed in" with a refresh to the app."""
    from fastapi.responses import JSONResponse

    from app import people_api
    world = app_with_oidc
    kid = _kid(world, enabled)
    kc = _client(world)
    assert world.login(kc, profile_id=kid["id"], pin="1357").status_code == 200
    _link(world, kc, kid["id"], idp, "mia-sub")
    monkeypatch.setattr(people_api, "session_response",
                        lambda *a, **k: JSONResponse({"detail": "too many"}, status_code=429))
    c, r = _signin_attempt(world, idp, "mia-sub")
    assert r.status_code == 429, r.text
    assert "Signed in" not in r.text and 'url=/"' not in r.text
    assert _signed_in_as(c) is None
