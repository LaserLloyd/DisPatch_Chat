# Remote Access & Authentication Design

**Target:** open-source, self-hosted chat app — FastAPI + vanilla-JS PWA, single process, SQLite.
**Status:** design proposal for the public-GitHub release.
**Date:** 2026-08-03.
**Scope:** how a stranger reaches their box, how they prove who they are, what is encrypted, and what the software forces them to do on first run.

[← Back to the README](../../README.md) · Companion document: [Multi-User Architecture Design](multi-user.md)

---

## 0. The one-paragraph answer

Ship the app bound to **`127.0.0.1` with no TLS of its own**, and make **Caddy on the same host the one and only TLS terminator**. Reachability then becomes a transport question with three answers that do not change the security model: **port-forward 443** if the operator has a routable IP, a **€4/mo VPS running WireGuard with a raw TCP/443 passthrough** if they are behind CGNAT, or **nothing at all** for LAN-only. In every case plaintext exists only on hardware the operator owns, and the public hostname — hence the **WebAuthn RP ID** — stays identical across all three, which is what makes passkeys survivable for a self-hoster. Authentication is **per-user accounts with Argon2id passwords plus a mandatory second factor**, where **TOTP is the floor** (works on any origin, any browser, forever) and a **user-verifying passkey is the recommended primary** (phishing-resistant, and equivalent to 2FA on its own). Account recovery is **a CLI on the host box**, not a network endpoint — shell access is the ownership proof for self-hosted software. First run refuses to serve anything until the operator has created an owner account, enrolled a second factor, saved recovery codes, and explicitly chosen an exposure mode.

**Do not claim end-to-end encryption.** This is a server-side-storage chat app with server-side search and server-side bots. E2EE is architecturally incompatible with those features and unachievable in a browser-delivered client anyway. Say so in the README.

---

## 1. Threat model — what we are actually defending against

Ranked by realistic likelihood for a home self-hoster.

| # | Adversary | Capability | Primary defence |
|---|---|---|---|
| T1 | **Internet background radiation** — Shodan/Censys sweeps, mass exploit scanners, credential stuffers | Finds anything on a public IP within hours. Tries default creds, known CVEs, path traversal. | No fail-open state; MFA mandatory; rate limiting; nothing sensitive on an unauthenticated path |
| T2 | **Targeted credential attack** — phishing, AitM proxy (Evilginx-class), password reuse from a breach | Can relay a TOTP code in real time; cannot forge a WebAuthn assertion for the wrong origin | Passkeys (phishing-resistant); Argon2id; breach-list screening; session binding |
| T3 | **Passive network observer** — hostile Wi-Fi, ISP, hotel captive portal | Reads plaintext HTTP, steals a non-`Secure` cookie | TLS everywhere including LAN; `Secure` + `__Host-` cookies; HSTS |
| T4 | **Third-party infrastructure** — the tunnel/CDN/mesh provider | Can read plaintext if it terminates TLS; can be subpoenaed; can change its ToS | Terminate TLS on the operator's own box; never make a commercial provider load-bearing |
| T5 | **Stolen/lost device** with a live session | Full access as that user until revoked | Absolute session expiry; per-session revocation UI; step-up re-auth for security settings |
| T6 | **Local network peer** — a compromised IoT device, a guest on the Wi-Fi | Reaches `192.168.x.x:8765` directly | Bind loopback only; auth applies on LAN identically to the internet |
| T7 | **Host compromise** | Game over | Out of scope. Be honest: root on the box reads the SQLite file |

**Explicitly out of scope:** protecting users *from the operator*. The person running the server can read every message. That is inherent to the architecture and must be stated plainly in the README rather than papered over (see §5).

**The current design fails T1, T3, T6 badly.** Today the app binds `0.0.0.0`, serves plain HTTP, sets a session cookie without `Secure`, and — with no PIN configured — grants an unauthenticated PTY WebSocket to anyone on the network. That last one is a remote-code-execution hole in the default configuration. It is survivable only because a private Tailscale tailnet has been doing all the actual security work. Remove Tailscale and the app is naked.

---

## 2. Part 1 — Reaching the box from the internet

### 2.1 The architectural invariant

> **TLS terminates on hardware the operator owns. Nothing else is negotiable.**

Everything below is judged against that. It is the property that distinguishes "self-hosted" from "hosted by Cloudflare with extra steps," and it is what the operator is actually buying by not using a SaaS chat app.

A useful corollary: because Caddy always terminates TLS locally and always presents the same hostname, **the transport can change without invalidating a single credential**. An operator who starts on DuckDNS + port-forward and later moves to a VPS + WireGuard keeps every passkey, every session, and every bookmark. Designs that bind the identity to the transport (Tailscale MagicDNS names, `*.trycloudflare.com`, LAN IPs) cannot make that promise.

### 2.2 Option comparison

Setup difficulty is rated for a non-expert following a written guide.

#### A. Reverse proxy + Let's Encrypt, direct port-forward — **the recommended default when the IP is routable**

| | |
|---|---|
| **Difficulty** | Medium. One router change (forward 443, and 80 if using HTTP-01), one DNS record, one 5-line Caddyfile. The router UI is the hard part and it is different on every router. |
| **CGNAT** | **Completely broken.** No inbound port to forward. This is the single biggest failure mode and it must be detected, not discovered. |
| **Cost** | Domain ~$8–12/yr, or **$0 with DuckDNS**. Certs free. |
| **Threat model** | Strong on T3/T4 — you own every hop and the only plaintext is on your box. Weak on T1: your home IP is now in every scanner's database within hours, and your residential IP is publicly linked to your family chat. |
| **Gotchas** | Many residential ISPs block inbound 80 and/or 443 outright. Dynamic IP needs a DDNS updater. HTTP-01 needs port 80 reachable; **DNS-01 needs neither port**, which is why it is the recommendation below. |

#### B. Cloudflare Tunnel

| | |
|---|---|
| **Difficulty** | **Easy.** Outbound-only connection, zero router config, works first try. Genuinely the lowest-friction option and that is not nothing. |
| **CGNAT** | **Works.** `cloudflared` dials out; there is no inbound port. |
| **Cost** | $0 for the tunnel. Needs a domain **on Cloudflare's nameservers** for a named tunnel (~$10/yr). Zero Trust Access is free to 50 users. |
| **Threat model** | Good against T1 (origin IP hidden, DDoS absorbed, and Cloudflare Access can put SSO+MFA *in front* of the app). **Fails T4 outright: Cloudflare terminates your TLS and sees every message in plaintext.** For a family chat app that is a meaningful privacy regression, and it is the exact thing a self-hoster is trying to avoid. |
| **Gotchas** | **100 MB max request body on Free and Pro** — this app has a file server; uploads over 100 MB will fail and the error will be opaque. The CDN-specific terms still restrict serving video and large files not hosted on Cloudflare's own storage. The old blanket "non-HTML content" clause (§2.8) was removed in the late-2025 ToS rewrite, but the large-file restriction moved rather than vanished. Also a commercial dependency whose free tier is a business decision, not a contract. |

**Verdict:** document it as tier 2, be blunt about the plaintext and the 100 MB cap. Do not make it the default. The brief said no dependency on a commercial mesh VPN; Cloudflare Tunnel is the same class of dependency wearing a different hat.

#### C. Self-hosted WireGuard mesh (wg-easy / Headscale / NetBird)

| | |
|---|---|
| **Difficulty** | wg-easy: **easy** for hub-and-spoke. Headscale/NetBird: **hard** — you are now operating a control plane, a relay, and a client fleet. |
| **CGNAT** | **wg-easy on the home box is broken behind CGNAT** — same inbound problem as port-forwarding. Headscale and NetBird work because they fall back to relays (DERP / TURN), but *self-hosting the relay* means renting a VPS with a public IP, at which point see option D. Headscale defaults to Tailscale's public DERP relays — which quietly reintroduces the commercial dependency you were removing. |
| **Cost** | $0 if routable; $4–6/mo VPS if not. |
| **Threat model** | **Best available.** The app is never on the public internet at all; T1 disappears entirely. |
| **Gotchas** | Every client device needs a VPN app installed, configured, and kept running. That is a hard sell for the non-technical family members who are the users of a family chat app. Battery impact on phones. And a PWA behind a VPN gets no notifications when the VPN drops. |

**Verdict:** correct answer for a paranoid single operator, wrong answer as a default for an app whose users include people who will not install a VPN profile.

#### D. VPS + self-hosted tunnel (Pangolin, or plain WireGuard + TCP passthrough) — **the recommended default under CGNAT**

| | |
|---|---|
| **Difficulty** | Medium-hard, but scriptable. Rent a VPS, run one install script, run one connector at home. |
| **CGNAT** | **Works.** Home dials out to the VPS. |
| **Cost** | $4–6/mo. This is the honest price of being behind CGNAT and refusing a third-party tunnel. |
| **Threat model** | You own every hop. **Whether the VPS sees plaintext depends on where TLS terminates** — and this is the design decision most guides get wrong. Pangolin terminates TLS at the VPS (convenient, but the VPS is now a plaintext hop). **Configure the VPS as a dumb L4 forwarder instead** — `iptables`/`socat`/nginx `stream` moving raw TCP/443 across the WireGuard link — and Caddy at home terminates TLS. The VPS then sees only ciphertext and cannot read messages even if it is seized or compromised. |
| **Gotchas** | Bandwidth caps and egress billing on cheap VPSes. One more box to patch. Pangolin is young — evaluate before betting on it. |

#### E. Plain port-forward, no TLS

Not an option. Sends session cookies in cleartext, breaks WebAuthn entirely (no secure context), and makes the app a trivial target. **The installer should refuse to configure this.**

#### F. mDNS / LAN-only

| | |
|---|---|
| **Difficulty** | Easy. |
| **CGNAT** | Irrelevant — no internet reach at all. |
| **Cost** | $0. |
| **Threat model** | Excellent against T1–T4; still needs full auth for T6. |
| **Gotchas** | It is not remote access; framing it as such is dishonest. `.local` is owned by mDNS/Bonjour and behaves unpredictably as a WebAuthn RP ID. Use **`.internal`**, which ICANN formally reserved for private use in July 2024 and will never delegate. Local TLS requires a private CA whose root must be installed *and explicitly trusted* on every client — and iOS requires a second, non-obvious toggle in Settings → General → About → Certificate Trust Settings that a normal user will not find without a screenshot. |

### 2.3 The recommendation

**Default paved road, in priority order:**

```
                    ┌─────────────────────────────────────┐
                    │  Caddy (operator's box)             │
   internet ──────► │  · terminates TLS  · HSTS           │ ──► 127.0.0.1:8765
                    │  · Let's Encrypt DNS-01             │     (the app)
                    │  · the ONLY plaintext hop           │
                    └─────────────────────────────────────┘
                                    ▲
              ┌─────────────────────┼─────────────────────┐
              │                     │                     │
      T1: forward :443      T2: VPS + WireGuard    T3: nothing
        (routable IP)        L4 passthrough         (LAN-only,
                              (CGNAT)                tls internal)
```

**1. Caddy, always.** Not nginx, not Traefik.
   - Caddy makes HTTPS the default rather than a project. nginx requires certbot, a renewal timer, a reload hook, and a hand-written TLS block, and every one of those is a place a non-expert silently ends up on an expired cert. Traefik's value is Docker label auto-discovery, which is irrelevant for a single-process app and costs a steep concept tax (providers, routers, middlewares, entrypoints) to learn.
   - Caddy handles WebSocket upgrades transparently with no config, which this app needs for `/ws`.
   - Caddy sets `X-Forwarded-For` / `X-Forwarded-Proto` / `X-Forwarded-Host` correctly by default.
   - Caddy has **no request body size limit by default** — unlike Cloudflare's 100 MB — so the file server keeps working.
   - Current stable is **2.11.x** (2.11.4, June 2026). *(Uncertain: exact latest patch — pin whatever `caddyserver.com/download` serves at release time.)*

**2. A DNS name the operator controls, chosen once, changed never.** This is the highest-leverage decision in the whole design because it is what the WebAuthn RP ID binds to permanently (§3.4). Two supported paths:
   - **Own a domain** (~$8–12/yr). Best. Use a dedicated subdomain, e.g. `chat.example.com`, and set the RP ID to that subdomain — **not** the apex — so that a future unrelated service on the apex cannot mint credentials for the chat app.
   - **DuckDNS** ($0). `yourname.duckdns.org`. This works for WebAuthn specifically because **`duckdns.org` is on the Public Suffix List**, which makes `yourname.duckdns.org` an eTLD+1 — a legal RP ID, with cookie scoping isolated from every other DuckDNS user. It also gets its own Let's Encrypt rate-limit bucket. Include a first-run warning that a free DDNS name is a *provider* dependency and that migrating off it later kills all passkeys.

**3. Let's Encrypt via DNS-01, not HTTP-01.** One mechanism that works in all three transports and needs no inbound port at all — so the same Caddyfile is correct whether the operator is port-forwarding, behind a VPS, or LAN-only. It also survives an ISP that blocks port 80, which is common on residential lines. Cost: it needs a DNS provider API token, and a Caddy build that includes the provider module (`caddyserver.com/download` has a plugin picker, which keeps this a download rather than an `xcaddy` build for most people).

**4. Transport, chosen by the installer after actually testing reachability:**
   - **Routable public IP** → forward TCP/443 only.
   - **CGNAT** → VPS + WireGuard + **L4 passthrough** (TLS still terminates at home).
   - **Neither / doesn't want internet exposure** → LAN-only with `tls internal`.

**The installer must detect CGNAT rather than let the operator discover it.** Compare the router's WAN address (UPnP/NAT-PMP if available) against the address reported by an external echo service, and check whether the WAN address falls in `100.64.0.0/10`. If they differ, print the CGNAT branch of the guide instead of the port-forward branch. Getting this wrong wastes hours of a beginner's evening and is the number-one reason people give up and install Tailscale.

**Documented ladder, in the README, in this order:**

| Tier | Setup | Who it's for | Third party sees plaintext? |
|---|---|---|---|
| 0 | LAN-only, Caddy `tls internal` | "I only use this at home" | No |
| **1** | **Caddy + DNS-01 + port-forward 443** | **Routable IP — the default** | **No** |
| **1b** | **Caddy + DNS-01 + VPS/WireGuard L4** | **CGNAT — the default** | **No** |
| 2 | Cloudflare Tunnel (+ Access) | "I want it working in 10 minutes" | **Yes — Cloudflare** |
| 3 | WireGuard mesh, app never public | Paranoid single operator | No |
| ✗ | Plain HTTP port-forward | Nobody. Installer refuses. | n/a |

---

## 3. Part 2 — Authentication

### 3.1 Prerequisite: kill the shared PIN

A single shared PIN cannot support TOTP enrolment (whose phone?), passkeys (whose authenticator?), per-device revocation, or any audit trail. **Move to per-user accounts.** Minimum viable model:

- `owner` — full control, can manage users and security settings. Exactly one at install.
- `member` — normal chat access.
- `guest` — the existing Safe-Mode tier, now expressed as a role instead of an absence of credentials.

The current two-tier "locked vs unlocked" behaviour maps cleanly onto `guest` vs `member`/`owner`, so the redaction logic survives; only the gate in front of it changes.

**Delete the fail-open.** Today, no PIN configured means *everything* is open, including the terminal WebSocket. In the new design there is no state in which the app serves data without an authenticated principal. Before setup completes, every route except the setup flow returns `503`.

### 3.2 Passwords — Argon2id

```python
# argon2-cffi >= 25.1.0
from argon2 import PasswordHasher, profiles

ph = PasswordHasher.from_parameters(profiles.RFC_9106_LOW_MEMORY)
# t=3, m=65536 KiB (64 MiB), p=4, hash_len=32, salt_len=16
```

- `argon2-cffi` has defaulted to the RFC 9106 "SECOND RECOMMENDED" low-memory profile since 21.2.0; it lands around **~50 ms** per verify on ordinary hardware, which is the right place on the security/UX curve.
- This is comfortably above the **OWASP minimum of m=19 MiB, t=2, p=1**. Ship a `DISPATCH_ARGON2_PROFILE=low_end` escape hatch that drops to exactly the OWASP minimum for Raspberry Pi class hardware, and document the tradeoff instead of silently degrading.
- 64 MiB × concurrent verifications is the memory exposure. Rate limiting (§3.6) bounds it; also cap concurrent password verifications with a semaphore (4) so a burst cannot OOM a small box.
- **Rehash on login** when parameters change: `ph.check_needs_rehash(stored)`.
- **Password policy, per NIST SP 800-63B-4 (final, July 2025):** minimum **15 characters** for password-only flows, **8** when a second factor is enforced (which it always is here, so **12** is a reasonable house minimum); maximum at least 64; accept all Unicode and spaces; **no composition rules**; **no forced rotation** absent evidence of compromise. Screen against a breach list — ship a bundled top-100k list for offline installs and make the HIBP k-anonymity range API an explicit opt-in, since a self-hosted app must not phone home by default.
- Peppering: OWASP suggests considering it. **Skip it.** For single-operator self-hosted software the pepper inevitably lives in a file next to the database, so it buys nothing against the only realistic threat (whole-host compromise) while adding a key-rotation failure mode that will lock people out. Say so in a code comment so the next reviewer does not "fix" it.

### 3.3 TOTP — the floor, not the ceiling

TOTP is the second factor that **always works**: any origin, any browser, no secure context requirement, no RP ID, no vendor. It is the reason a self-hoster can never be locked out by an infrastructure change. It is also **not phishing-resistant** — an AitM proxy relays the code in real time — which is why it is the floor and passkeys are the recommendation.

**Parameters:** RFC 6238 defaults — **SHA-1, 6 digits, 30 s period**. Do not get clever. Google Authenticator, Aegis, 1Password, and Apple's built-in generator all handle SHA-1/6/30; several handle SHA-256 or 8 digits badly or not at all, and the failure mode is a user who cannot enrol and has no idea why.

**Secret:** 160 bits from `secrets.token_bytes(20)`, base32-encoded (32 characters).

**Verification window:** **±1 step** (accepts the code from 30 s ago through 30 s from now, ~90 s total). Do not go wider; every extra step is a linear increase in the online-guessing surface.

**Replay prevention (frequently omitted, non-negotiable):** store `last_used_timestep` per user and reject any code whose timestep is `<=` the last accepted one. Without this a code captured from a shoulder-surf or a proxy is reusable for the rest of its window. Library note: `pyotp.TOTP.verify()` does **not** do this for you.

**Secret storage:** encrypt at rest with a key in a `0600` file outside the SQLite database, so a leaked database backup does not hand over every 2FA seed. Be honest in the docs about the limit: this defends against *backup exfiltration*, not host compromise, because the key is on the same machine.

**Enrolment UX:**

1. Step-up: require the current password (re-auth), even if the session is live.
2. Server generates the secret, stores it as `pending` (never active until proven).
3. Render the `otpauth://` URI as a QR code **rendered locally** — no external QR API, ever, because that leaks the shared secret to a third party. Generate the SVG server-side (`segno`, pure Python, no C deps) or client-side from a vendored library. Given the project's "vendored libs, no CDN" rule, server-side SVG is the cleaner fit.
   ```
   otpauth://totp/DisPatch:alice?secret=BASE32&issuer=DisPatch&algorithm=SHA1&digits=6&period=30
   ```
   The `issuer` label must be the app name, not the hostname — otherwise every operator's authenticator entry reads `chat.example.com` and moving domains orphans it visually.
4. **Also show the secret as text**, chunked in groups of 4, with a copy button. QR scanning fails on desktop-only setups and on cracked phone screens more often than anyone plans for.
5. **Require one valid code before activating.** This single step prevents the most common 2FA lockout in the wild: enrolling against a phone whose clock is wrong.
6. Immediately show recovery codes (§3.5) with a mandatory "I have saved these" confirmation.
7. On success: invalidate all *other* sessions for that user, and surface a notification in-app.

### 3.4 WebAuthn / passkeys — the recommended primary, with the sharp edges named

**Library:** `webauthn` (duo-labs `py_webauthn`) — current **3.0.0**, released 2026-06-29, requires Python ≥3.10. The project already requires ≥3.12. v3.0.0 adds post-quantum (ML-DSA) algorithm support; it is a major version bump, so pin it and read the changelog rather than floating.

#### Where passkeys are strictly better than TOTP

- **Phishing-resistant by construction.** The assertion is bound to the origin; an AitM proxy on a lookalike domain gets an assertion the real server rejects. This is the T2 defence and TOTP simply does not have it. NIST, the FIDO Alliance and the UK NCSC all classify it this way.
- **A user-verifying passkey is already two factors** — possession of the authenticator plus the biometric/PIN that unlocks it. The NCSC states that a FIDO2 credential which verifies the user before authenticating is equivalent to 2FA. So `userVerification: "required"` + a discoverable credential is a legitimate *single-step* login, not a first factor needing a second.
- **No shared secret on the server.** A database leak yields public keys. Compare TOTP, where a leaked seed is a permanent second-factor bypass.
- **No typing, no clock skew, no 30-second panic.** Materially better UX, which matters because the users of a family chat app are not security enthusiasts.

#### Where passkeys hurt a self-hoster — say all of this out loud in the docs

**(a) Secure context is mandatory.** `navigator.credentials` is unavailable on `http://192.0.2.50:8765`. Not degraded — absent. The only HTTP exception is `localhost` (and `*.localhost` in some browsers). **Consequence: LAN-IP-over-HTTP access and passkeys are mutually exclusive.** This is the single biggest reason the whole design insists on TLS even on the LAN.

**(b) The RP ID must be a domain, never an IP — even with a valid certificate.** The spec forbids IP addresses and public suffixes as RP IDs. Note the trap this creates in 2026: Let's Encrypt now issues **IP address certificates** (generally available January 2026, 160-hour lifetime, `shortlived` ACME profile), so an operator can have a perfectly valid padlock on `https://203.0.113.10` **and still find passkeys silently unavailable**. Detect this case and show a real explanation instead of a generic WebAuthn error.

**(c) The RP ID is permanent and invisible.** The authenticator stores a SHA-256 of the RP ID at credential creation. It cannot be edited. **Change the domain, and every passkey on every family device is dead simultaneously, with no warning and no migration path.** Self-hosters change domains constantly — DuckDNS to a real domain, moving house, switching ISP, renaming a subdomain. This is the failure mode that will generate the project's angriest GitHub issues, and it is entirely preventable by design:

  - Make RP ID a **first-run decision**, presented as such, with the words "you cannot change this later without re-enrolling every device."
  - Store it in config as `webauthn_rp_id`. **Refuse to serve WebAuthn ceremonies if the request Origin does not match the configured RP ID** — a mismatch means the operator moved domains, and a clear error beats a browser-level `NotAllowedError` that nobody can debug.
  - Set RP ID to the **specific subdomain in use** (`chat.example.com`), not the apex, so a future service on the apex cannot assert for the chat app.
  - **Never allow a passkey to be an account's only credential.** Password + TOTP stays enrolled underneath. This turns "I changed my domain" from a catastrophe into an inconvenience.

**(d) Related Origin Requests help less than they look like they will.** ROR (WebAuthn L3) lets one RP ID work across multiple origins via a `/.well-known/webauthn` allowlist served from the RP ID domain. Support is now broad — Chrome/Edge 128+ and Safari 18 shipped it in 2024, Firefox 152 added it on desktop and Android in May 2026. Two limits matter here: browsers are only required to honour **five unique labels**, and none exceed that minimum; and **the file is served from the old RP ID domain**, so ROR lets you *add* origins while the old domain still resolves — it does not rescue you after that domain is gone. Useful for "I want `chat.example.com` and `chat.example.net` to share credentials." Useless for "I let my DuckDNS name lapse."

**(e) Private-CA LAN setups technically work, practically bite.** WebAuthn requires a *secure context*, which a browser-trusted private CA satisfies — so Caddy `tls internal` + `https://chat.internal` can support passkeys. But every client must install and trust the root CA, and iOS additionally requires the separate Certificate Trust Settings toggle that is genuinely hidden. Prefer `.internal` (ICANN-reserved since July 2024, never delegated) over `.local` (owned by mDNS/Bonjour). *(Uncertain — **not independently verified**: no authoritative statement could be found that Chrome and Safari accept a non-PSL private TLD such as `chat.internal` as an RP ID. The spec's rule — the RP ID must be a registrable domain suffix of the origin's effective domain and must not itself be a public suffix — implies it should work, and community reports of hosts-file-aliased private domains working support that. **Test on Chrome, Firefox and Safari before documenting it as supported.**)*

**(f) Installed-PWA quirks.** On Android and desktop an installed PWA shares the browser's credential store and passkeys work normally. On iOS, home-screen web apps do support WebAuthn, but Apple shipped a regression in **iOS 26.2** where `isUserVerifyingPlatformAuthenticatorAvailable()` returned `false` inside `WKWebView`, breaking passkey detection in Chrome/Edge/Firefox on iPhone; fixed in **26.3**. Separately, EU DMA changes in the iOS 17.4 era forced home-screen sites to open in Safari tabs for a period. Practical guidance: never gate the login UI on `isUserVerifyingPlatformAuthenticatorAvailable()` alone — always render the password+TOTP path as a visible fallback, so an OS bug degrades the experience instead of locking the family out.

#### On "the key on the host machine for the two factor"

This needs a direct answer because the mental model is off by one:

> **A WebAuthn credential physically cannot live on the server.** The private key is generated by and never leaves the *authenticator*, which is attached to the *client*. There is no configuration in which the machine running the chat app acts as the second factor for a browser somewhere else. The server only ever stores a public key.

What the request most likely means, and what to build:

- **If "the host machine" is the machine you browse from** — a laptop or desktop — then this is exactly right and is the recommended setup. Windows Hello, macOS Touch ID, and Linux TPM-backed platform authenticators create a **device-bound** passkey sealed to that machine's secure element. Request it with `authenticatorAttachment: "platform"` and `residentKey: "required"`. It never syncs to a cloud keychain, which is often precisely what a self-hoster wants.
- **If you want a factor that is physically at the server** — keep a **FIDO2 hardware key** (YubiKey, Nitrokey, SoloKey) plugged into that machine and register it as a cross-platform authenticator. Same effect, and it is portable.
- **If you want the secret stored server-side** — that is TOTP, and you already have it. Encrypted seed in the database, code typed by the human. Note the asymmetry honestly: server-side storage means a host compromise yields the second factor, which is exactly the property WebAuthn was designed to eliminate.

**Recommended registration options:**

```python
generate_registration_options(
    rp_id=cfg.webauthn_rp_id,            # e.g. "chat.example.com" — permanent
    rp_name="DisPatch",
    user_id=user.webauthn_handle,         # 32 random bytes; NOT the username, NOT a rowid
    user_name=user.username,
    user_display_name=user.display_name,
    authenticator_selection=AuthenticatorSelectionCriteria(
        resident_key=ResidentKeyRequirement.REQUIRED,      # discoverable => usernameless login
        user_verification=UserVerificationRequirement.REQUIRED,  # makes it 2FA on its own
        # authenticator_attachment: leave unset — let the user pick phone, laptop or key
    ),
    attestation=AttestationConveyancePreference.NONE,      # nothing useful to verify; adds privacy cost
    exclude_credentials=[...],            # prevents silent duplicate registration
)
```

`user_id` must be an opaque random handle, not a username or an autoincrement id — it is stored on the authenticator forever and leaks to anyone who gains the device.

**Enforce `user_verification == True` in the assertion response, server-side.** Requesting it in the options is a preference; only the server check makes it a guarantee.

### 3.5 Recovery — the part most designs get wrong

The current implementation has an **unauthenticated network endpoint** that accepts a **~40-bit** code and, on success, **removes the PIN entirely**. Under a global 5-attempt throttle that is the weakest remotely reachable credential in the system and it should not survive into a public release.

**Replacement, two layers:**

**Layer 1 — recovery codes.** Ten single-use codes, each **10 characters** from a 32-character unambiguous alphabet (Crockford base32 minus `I L O U`) = **~50 bits each**, formatted `xxxxx-xxxxx`. Presented once at MFA enrolment, with a mandatory acknowledgement and a "download as .txt" button.

Storage: a **single SHA-256** per code, constant-time compared, plus a random per-install salt. This deliberately departs from "always use a slow KDF" and the reasoning belongs in a code comment: slow KDFs exist to defend *low-entropy human-chosen* secrets against offline brute force. A 50-bit uniformly random code is not brute-forceable offline at any realistic cost, and hashing ten of them with Argon2id on every login attempt is a self-inflicted DoS. Mark used codes rather than deleting them, so the UI can show "3 of 10 remaining," and prompt regeneration below 3.

A recovery code substitutes for the **second factor only** — the password is still required. It must never, as today, dissolve the lock entirely.

**Layer 2 — a host CLI, replacing the network recovery endpoint entirely.**

```
$ dispatch-admin reset-password alice
$ dispatch-admin reset-mfa alice
$ dispatch-admin unlock alice          # clears rate-limit state
$ dispatch-admin list-sessions alice
```

This is the correct recovery model for self-hosted software: **shell access to the box is the ownership proof.** It removes an entire class of remotely reachable attack surface, cannot be brute-forced from the internet, and is honest about who is actually in charge. It also happens to be what every self-hoster expects.

**Delete `POST /api/auth/recover`.** No network endpoint should be able to remove the lock.

Email-based recovery is deliberately excluded: it makes account security equal to the user's inbox, and most self-hosted installs have no working outbound mail anyway.

### 3.6 Rate limiting and lockout

Current state: a single **process-global, in-memory** counter, not per-IP and not per-account, cleared by a restart. This is simultaneously too weak (one attacker exhausts nothing; a restart resets it) and too strong (one attacker locks out the entire household — a trivial DoS).

**Replacement:** persist attempt state in SQLite (the app is single-process, so a table with a lock is sufficient; no Redis).

```sql
CREATE TABLE auth_attempts (
  scope      TEXT NOT NULL,   -- 'pw' | 'totp' | 'recovery' | 'webauthn'
  subject    TEXT NOT NULL,   -- user_id, or 'ip:<addr>'
  window_start REAL NOT NULL,
  count      INTEGER NOT NULL,
  locked_until REAL,
  PRIMARY KEY (scope, subject)
);
```

Two independent dimensions, both must pass:

| Scope | Limit | Backoff |
|---|---|---|
| Password, per (account, IP) | 5 failures | 30 s, doubling → 15 min cap |
| Password, per IP across all accounts | 20 failures/hour | hard block 1 h — stops credential spraying |
| TOTP, per account | 5 failures | 60 s lock; hard cap 30/hour |
| Recovery code, per account | 5/hour | then 1/hour |
| WebAuthn assertion | 20/min per IP | cheap to verify; this is DoS protection, not guessing protection |

NIST SP 800-63B-4 requires verifiers to rate-limit failed attempts and caps online OTP guessing at **no more than 100 consecutive failures**; 30/hour is comfortably inside that.

**Never permanently lock an account** — that converts a nuisance into a denial of service against a family's only chat app. Escalating delay plus `dispatch-admin unlock` as the release valve.

**Client IP must come from a trusted proxy only.** Uvicorn must run with `--proxy-headers --forwarded-allow-ips 127.0.0.1` (never `*`, which lets any client spoof `X-Forwarded-For` and walk straight past every per-IP limit above). When no trusted proxy is configured, use the raw peer address and ignore forwarding headers entirely.

Log every failure with timestamp, account, source IP, and factor, and surface the last 50 in the owner's security panel. Emit an in-app notification on: successful login from a new device, any MFA change, any password change, any API token creation.

### 3.7 Session management

**Cookie:**

```
Set-Cookie: __Host-dispatch_session=<48 random bytes, base64url>;
            Secure; HttpOnly; SameSite=Lax; Path=/; Max-Age=<n>
```

- `__Host-` prefix per OWASP: forces `Secure`, forbids `Domain`, mandates `Path=/`. Blocks subdomain cookie-forcing — which matters on shared suffixes like `duckdns.org`.
- **384 bits of entropy**, far above OWASP's 64-bit floor, from `secrets.token_urlsafe(48)`.
- `SameSite=Lax`, not `Strict`. Strict logs the user out on their first click from a notification or a shared link, which for a chat app is a constant papercut. Lax is compensated by mandatory origin enforcement below.
- **Localhost caveat:** browsers disagree about `Secure`/`__Host-` over `http://localhost`. Firefox allows `Secure` *and* the prefixes; Chrome allows `Secure` but **rejects `__Host-`/`__Secure-`**; Safari rejects all of them. So the `__Host-` prefix and `Secure` must be **conditional on the app knowing it is behind TLS** (config flag, not header sniffing), with a plain `dispatch_session` name for the `http://localhost` development path. Getting this wrong produces a login loop that only reproduces in one browser.

**Server-side session store:** a SQLite `sessions` table storing the **SHA-256 of the token**, never the token. Fixes three current problems at once: sessions survive a restart, sessions are enumerable for the revocation UI, and a database read does not yield usable cookies.

```sql
CREATE TABLE sessions (
  token_hash   BLOB PRIMARY KEY,
  user_id      INTEGER NOT NULL,
  created_at   REAL NOT NULL,
  last_seen    REAL NOT NULL,
  absolute_exp REAL NOT NULL,
  device_label TEXT,        -- parsed UA, for the UI
  ip_created   TEXT,
  remembered   INTEGER NOT NULL DEFAULT 0,
  reauth_at    REAL          -- last step-up, for sensitive actions
);
```

**Expiry:**

| | Idle | Absolute |
|---|---|---|
| Normal session | 8 hours | 30 days |
| "Remember this device" | 30 days | 90 days |
| Step-up (sensitive actions) | — | 15 minutes |

These are **deliberately laxer than OWASP's 15–30 min idle / 4–8 h absolute guidance**, and the reason should be documented rather than hidden: a family chat PWA that demands re-authentication twice a day will have its 2FA disabled by the operator within a week, which is strictly worse. The absolute cap and one-click revocation are what carry the risk. Both values are configurable, and the *sensitive-action* window stays tight regardless.

**Rotation.** Issue a new token and delete the old on: login, step-up re-auth, password change, MFA enrolment/removal, and role change. OWASP requires renewal on any privilege-level change.

**Revocation.** A "Devices & Sessions" panel listing device label, IP, created, last seen, current-session marker, with "Sign out" and "Sign out everywhere." Password change and MFA change revoke all sessions except the acting one.

**"Remember this device"** — use the classic **selector + validator with rotation**, not a long-lived bearer token:

- Cookie is `<selector>.<validator>`; the table stores `selector` in plaintext (for an indexed lookup, avoiding a full scan) and `SHA-256(validator)`.
- **Rotate the validator on every use.** If a *stale but valid* selector ever appears, the token was cloned: **revoke the entire device chain for that user and notify.** This is the only mechanism in the design that can actually *detect* a stolen cookie, and it is cheap.
- Cap at 10 devices, evict least-recently-seen, and expose per-device forget.
- Note the improvement over today's design, where the session token *is* the trusted-device token, so any leak is permanent until manual intervention and there is no theft signal.

**CSRF.** `SameSite=Lax` is a mitigation, not a defence, and the app currently has no CSRF token at all. Require, on **every** state-changing request:

1. `Origin` (or `Referer`) exactly matches a configured canonical origin — enforced server-side against config, not against the attacker-supplied `Host` header; **and**
2. a custom header (`X-Dispatch-CSRF`) carrying a per-session token, which a cross-origin form post cannot set.

**WebSocket auth.** Cookies ride the handshake, so cookie auth works — but WebSockets are exempt from CORS and from `SameSite` on the handshake in some paths, so the origin check must be inline and mandatory:

- Reject if `Origin` is **absent** or does not exactly match a configured canonical origin. The current guard already rejects positively — a present `Origin` whose host does not match the request `Host`, an empty origin host (`Origin: null`), or a missing `Host` header all close the socket rather than falling through open. What remains is that the guard allows an *absent* `Origin` (the documented non-browser path); the recommendation is to remove that allowance and move machine clients to API tokens.
- Compare against **configured origins**, not `Host` (which the client controls).
- Re-check session validity per frame, as the app already does — that part is right and should be kept.
- `/ws/terminal` must additionally require the `owner` role and a **fresh step-up** (within the 15-minute window). It is remote code execution; it should feel like it.

### 3.8 Machine access — replacing the loopback bypass

Today, any process on the box can `POST /api/inject` with no credential, and the "is this a browser?" heuristic (`Sec-Fetch-Site`/`Origin` presence) is trivially satisfiable by any HTTP client. That is acceptable for a single-user tailnet box; it is not acceptable in software strangers install, where "anything on localhost" includes every browser tab, every npm postinstall script, and every container on a shared host.

**Replace with scoped API tokens:**

- Format `dsp_<8-char id>_<32-char secret>`; store `SHA-256(secret)` keyed by id, so lookup is indexed and comparison is constant-time.
- Per-token scopes: `inject`, `read`, `reactions`, `admin`.
- Shown once at creation; listed thereafter by id, label, scopes, created, last-used.
- Revocable individually; per-token rate limits; every use logged.
- The installer generates one `inject`-scoped token and writes it to `~/.config/dispatch/agent-token` mode `0600` so the existing on-box agent integrations keep working with a one-line change.
- **Loopback grants nothing on its own.** Optional `allow_loopback_token_free` config for people who genuinely want the old behaviour, **defaulting to `false`**, with the risk spelled out in the comment.

---

## 4. Part 3 — Encryption

### 4.1 TLS termination

| Scenario | Mechanism | Notes |
|---|---|---|
| Public hostname, ports open | Let's Encrypt **HTTP-01** (port 80) or **TLS-ALPN-01** (port 443) | Simplest; Caddy does it with zero config. Fails if the ISP blocks 80. |
| **Public hostname, any transport** | **Let's Encrypt DNS-01** ← **recommended** | Needs no inbound port. Works behind CGNAT, behind a VPS, and LAN-only. One mechanism for every tier. Needs a provider API token and a Caddy build with the DNS module. |
| Public IP, no domain | Let's Encrypt **IP certificate** | GA since Jan 2026, 160-hour lifetime, `shortlived` profile. Valid TLS — but **passkeys still will not work**, because an IP cannot be an RP ID. Document as a stopgap only. |
| LAN-only | **Caddy `tls internal`** | Caddy generates its own CA and tries to install the root locally, prompting for a password; it explicitly warns this "isn't guaranteed to work, especially if containers are being used or if Caddy is being run as an unprivileged system service." Use `caddy trust` for the local box; every *other* device needs the root installed manually. |
| LAN-only, alternative | `mkcert` | Same model, friendlier CLI, but a second tool to install and keep in sync. Prefer `tls internal` — one less dependency. |
| Anything else | Self-signed + pinning | **Do not ship this.** Browsers cannot pin; the user is trained to click through a warning; HSTS makes it worse rather than better. |

**Caddy's automatic behaviour is a real advantage here:** it serves public DNS names with a public ACME CA and serves IP addresses and internal hostnames with its internal CA, choosing correctly with no operator input. That is exactly the kind of decision a non-expert should not have to make.

**HSTS:** enable `max-age=31536000; includeSubDomains` at the proxy once TLS is confirmed working. **Do not add `preload`** and do not suggest it — preload is effectively irreversible, and self-hosters change domains.

### 4.2 End-to-end encryption — the honest section

**This app does not offer end-to-end encryption, and the README must say so in those words.**

What E2EE would actually require, and what it would cost:

| Feature | Survives E2EE? |
|---|---|
| Server-side full-text search | **No** — server holds ciphertext. Client-side index only, per device. |
| Bot/agent participants reading messages | **No, by definition.** Bots are server-side; the entire agent architecture is incompatible. |
| New device sees full history | **No** without a key-transfer/backup protocol — the hard part of Signal and Matrix, and the part that generates support load. |
| Server-side thumbnails, media transcoding, link previews | **No.** |
| Web push with message content | **No** — content-free pushes only. |
| Message-content moderation / export | **No.** |

And the structural objection that applies regardless of effort: **a browser-delivered client cannot offer meaningful E2EE**, because the server ships the JavaScript that holds the keys. A server that turns hostile — or is compromised — serves modified JS on the next reload and exfiltrates the plaintext, and no user can detect it. This is why Signal and Matrix ship native clients with reproducible builds. Implementing E2EE in this app would produce the *marketing* of E2EE with none of the security, which is worse than not claiming it, because users would change their behaviour based on a false belief.

**What to offer instead, and describe accurately:**

1. **TLS in transit, terminating only on the operator's hardware.** This is the real win of the §2 design and it is worth stating prominently: with tier 0/1/1b/3, no third party can read messages, full stop.
2. **Encryption at rest**, via honest options:
   - **LUKS / full-disk encryption on the host** — recommended, covers everything (database, media, backups, WAL) and requires no application code. This is the right answer for almost everyone.
   - **SQLCipher** for the database — flag as available but note it does not cover the `media/` directory or the WAL sidecar unless configured carefully, and that the key has to live on the same box.
   - **Encrypted backups** — the backup tarball leaves the machine; encrypt it (age or gpg) with a passphrase not stored on the box.
3. **Data minimisation** — configurable message retention and automatic media expiry. Not encryption, but it reduces what a compromise yields, and it is achievable.
4. **A plain statement in the README:**
   > Messages are stored on the server in a form the server can read. The person who runs the server can read every message, every file, and every reaction. That is inherent to how this app works. If you need messages the operator cannot read, use Signal or a Matrix client with E2EE enabled — not this.

---

## 5. Part 4 — Concrete defaults

### 5.1 What ships enabled

```yaml
# config.yaml — shipped defaults
server:
  bind: "127.0.0.1"          # CHANGED from 0.0.0.0. Loopback only, always.
  port: 8765
  trusted_proxy: "127.0.0.1" # X-Forwarded-* honoured from here only
  canonical_origins: []      # populated by first-run; empty = setup incomplete
  behind_tls: false          # set true by first-run; gates Secure/__Host- cookies

auth:
  require_mfa: true          # cannot be disabled for owner/member
  argon2_profile: "rfc9106_low_memory"
  password_min_length: 12
  breach_check: "bundled"    # bundled | hibp | off — hibp is opt-in, never default
  session_idle_seconds: 28800        # 8h
  session_absolute_seconds: 2592000  # 30d
  remember_idle_seconds: 2592000     # 30d
  remember_absolute_seconds: 7776000 # 90d
  stepup_window_seconds: 900         # 15m
  max_remembered_devices: 10

webauthn:
  enabled: true
  rp_id: null                # set at first-run. PERMANENT.
  rp_name: "DisPatch"
  user_verification: "required"

terminal:
  enabled: false             # CHANGED. Opt-in, owner-only, step-up required.

api_tokens:
  allow_loopback_token_free: false   # CHANGED. Loopback grants nothing.
```

**Changes from today, each of which is a security fix rather than a preference:**

| Was | Now | Why |
|---|---|---|
| `bind: 0.0.0.0` | `bind: 127.0.0.1` | T6. Any LAN device could reach the app directly. |
| No PIN ⇒ everything open, including PTY | Setup incomplete ⇒ `503` on everything | Unauthenticated RCE in the default config. |
| Cookie without `Secure` | `__Host-` + `Secure` when `behind_tls` | T3. |
| Terminal enabled by default | Disabled, owner-only, step-up | Blast radius. |
| Loopback = no credential | Scoped token required | "localhost" is not a trust boundary on a machine strangers install software on. |
| Network recovery endpoint clears the lock | Host CLI only | ~40-bit remotely reachable master credential. |
| PBKDF2-SHA256 200k | Argon2id RFC 9106 low-memory | Memory-hardness vs GPU cracking. |
| Global in-memory throttle | Per-account + per-IP, persisted | DoS in one direction, ineffective in the other. |

### 5.2 First-run experience — what the operator is forced to do

The app starts, binds loopback, and serves **only** `/setup`, gated by a one-time token printed to stdout and the journal:

```
  DisPatch is not configured yet.
  Open:  http://127.0.0.1:8765/setup?token=A7K2-9QMX-4RTB
  (this token is single-use and expires in 30 minutes)
```

Requiring console access to begin setup closes the classic self-hosted hole where a scanner reaches the setup wizard before the owner does.

**Step 1 — Owner account.** Username, password (≥12 chars, screened against the bundled breach list, strength meter, no composition rules). Argon2id.

**Step 2 — Second factor. Not skippable.** Two paths presented side by side:
- **Passkey (recommended)** — greyed out with an explanation if the current origin is not a secure context or the RP ID is unset, rather than silently missing.
- **Authenticator app** — locally-rendered QR, secret in text, verify-a-code-before-activating.

Even when a passkey is chosen, **TOTP enrolment is offered immediately afterwards as the escape hatch**, with the reason stated: "If you ever change your domain, your passkeys stop working. This is how you get back in."

**Step 3 — Recovery codes.** Ten codes, download button, mandatory "I have saved these" checkbox, plus a re-typed code to prove they were actually captured.

**Step 4 — Exposure mode. The mandatory choice, with no default pre-selected:**

```
How will you reach this server?

( ) Only from this computer                      → bind 127.0.0.1, no proxy
( ) Only from my home network                    → Caddy + tls internal, .internal name
( ) From the internet — I have a domain          → Caddy + Let's Encrypt DNS-01
( ) From the internet — I don't have a domain    → DuckDNS walkthrough
( ) I already have a reverse proxy               → prints required headers, verifies them
```

The wizard then:
1. **Tests reachability** — checks for a routable IP, checks `100.64.0.0/10` for CGNAT, and **branches the instructions accordingly instead of assuming port-forwarding will work.**
2. **Writes a Caddyfile** into the install directory.
3. **Sets the RP ID** from the chosen hostname, showing the permanence warning in bold.
4. **Verifies the proxy end-to-end** before declaring success — fetches its own `/api/health` through the public hostname and confirms it arrives over TLS with `X-Forwarded-Proto: https` from the trusted proxy IP. It must not report success on a config file it wrote but never exercised.

**Step 5 — Summary card** stating exactly what is now exposed, to whom, and what the operator still needs to do (install the CA root on phones, forward a port, etc.).

### 5.3 Generated Caddyfile — internet, DNS-01

```caddyfile
{
	email you@example.com
}

chat.example.com {
	tls {
		dns duckdns {env.DUCKDNS_API_TOKEN}
		# or: dns cloudflare {env.CF_API_TOKEN}
	}

	encode zstd gzip

	header {
		Strict-Transport-Security "max-age=31536000; includeSubDomains"
		X-Content-Type-Options    "nosniff"
		Referrer-Policy           "same-origin"
		Cross-Origin-Opener-Policy "same-origin"
		Permissions-Policy        "geolocation=(), microphone=(), camera=()"
		-Server
	}

	reverse_proxy 127.0.0.1:8765 {
		# X-Forwarded-{For,Proto,Host} are set by Caddy automatically.
		flush_interval -1        # don't buffer SSE / streaming responses
	}
}
```

Caddy proxies WebSocket upgrades transparently with no extra directives, and imposes no request-body limit by default — so `/ws`, `/ws/terminal` and large uploads all work unchanged.

### 5.4 Generated Caddyfile — LAN-only

```caddyfile
chat.internal {
	tls internal
	encode zstd gzip
	header {
		X-Content-Type-Options "nosniff"
		Referrer-Policy        "same-origin"
		-Server
		# No HSTS: a private CA plus HSTS is a lockout waiting to happen.
	}
	reverse_proxy 127.0.0.1:8765 {
		flush_interval -1
	}
}
```

Plus generated instructions to point local DNS (router, Pi-hole, or hosts file) at the box, run `caddy trust` locally, and install `~/.local/share/caddy/pki/authorities/local/root.crt` on each device — **with the iOS Certificate Trust Settings step called out explicitly and screenshotted**, because it is the step everyone misses.

### 5.5 App-side hardening checklist

```python
# Uvicorn launch
uvicorn app.main:app \
  --host 127.0.0.1 --port 8765 \
  --proxy-headers --forwarded-allow-ips 127.0.0.1   # NEVER "*"
```

- **`TrustedHostMiddleware`** restricted to `canonical_origins` — blocks Host-header injection and password-reset poisoning.
- **CSP** (vanilla JS, all libs vendored, so this can be tight):
  ```
  default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline';
  img-src 'self' data: blob:; media-src 'self' blob:; font-src 'self';
  connect-src 'self'; frame-ancestors 'none'; base-uri 'none';
  form-action 'self'; object-src 'none'
  ```
  Target removing `'unsafe-inline'` from `style-src` via nonces as a follow-up. *(Minor uncertainty: CSP3 specifies that `connect-src 'self'` covers same-origin `ws:`/`wss:`, and current browsers implement this — but if the WebSocket is blocked in any target browser, add `wss://chat.example.com` explicitly rather than debugging it in the field.)*
- **`Clear-Site-Data: "cache", "cookies", "storage"`** on logout, per OWASP.
- **`Cache-Control: no-store`** on every authenticated response.
- Keep FastAPI's `docs_url`/`openapi_url` disabled (already true).
- `chmod 0700` the data directory, not just individual files (currently only three files are explicitly `0600`; the directory itself uses the default umask).
- Generic auth failures — never distinguish "no such user" from "wrong password" in the response body, the status code, **or the timing**. Run a dummy Argon2 verify on unknown usernames so response time does not enumerate accounts.

### 5.6 Suggested dependency additions

| Package | Purpose | Notes |
|---|---|---|
| `argon2-cffi >= 25.1.0` | Password hashing | Pulls a C extension; wheels are widely available |
| `pyotp >= 2.9` | TOTP | Small, pure Python. **Add your own replay check** — it has none |
| `webauthn >= 3.0.0` | WebAuthn/passkeys | duo-labs `py_webauthn`; needs `cryptography` + `cbor2`. Pin the major version |
| `segno` | QR SVG generation | Pure Python, zero deps. Keeps the shared secret off third-party QR services |

This takes the project from zero third-party crypto dependencies to four. That is a real cost against the current "stdlib only" property and it is worth stating in the README — but PBKDF2 for passwords and no WebAuthn at all is the wrong trade for internet-exposed software, and hand-rolling any of these four would be worse.

---

## 6. Migration path

1. **Bind loopback + ship the Caddyfile generator.** Biggest risk reduction per line changed; nothing else depends on it.
2. **Close the fail-open** — no configured auth ⇒ `503`, not "open". Disable the terminal by default.
3. **Accounts + Argon2id**, migrating the existing PIN to a single `owner` account (`dispatch-admin` prints a one-time claim URL).
4. **Sessions into SQLite**, hashed, with the revocation UI and selector/validator remember-me.
5. **TOTP + recovery codes**, then make MFA mandatory for `owner`/`member`.
6. **WebAuthn**, once a stable RP ID exists — it is meaningless before that.
7. **Scoped API tokens**, then flip `allow_loopback_token_free` to `false`.
8. **Delete `POST /api/auth/recover`**; ship `dispatch-admin`.

Steps 1–2 are the ones that must land before the repo goes public. Everything after is an improvement; those two are the difference between "hardened" and "hands strangers an unauthenticated shell."

---

## 7. Open questions and flagged uncertainties

1. **`.internal` as a WebAuthn RP ID is unverified.** The spec implies `chat.internal` is legal (a registrable domain suffix, not a public suffix), and hosts-file-aliased private domains are reported to work, but **this is not independently verified** — no authoritative confirmation was found for Chrome/Safari/Firefox specifically. **Test all three before documenting LAN-only passkeys as supported.** If it fails, LAN-only users are TOTP-only — which is fine, but must be stated.
2. **Caddy patch version.** 2.11.x is current (2.11.4, June 2026), but the exact latest patch shifts; pin whatever `caddyserver.com/download` serves at release.
3. **CSP `connect-src 'self'` covering `wss:`** — spec-correct and implemented in current browsers, but verify against the project's browser matrix rather than assuming.
4. **DuckDNS as a recommended free option is a judgement call.** It is a single volunteer-run service; if it disappears, every install using it loses both its hostname and all its passkeys at once. Consider recommending a cheap real domain more forcefully, with DuckDNS as the explicitly-labelled fallback.
5. **Session timeout values are deliberately laxer than OWASP** (8 h idle vs their 15–30 min for low-risk apps). Defensible for a family chat PWA, but it is a considered deviation and reviewers should see the reasoning, not discover the number.
6. **Cloudflare's ToS position on this workload is genuinely ambiguous.** The blanket non-HTML clause (§2.8) was removed in the late-2025 rewrite, but the CDN-specific terms still restrict serving large files and video not hosted on Cloudflare's own storage. A chat app with a file server sits in the grey zone. The 100 MB body limit is the concrete, verified constraint — lead with that rather than with ToS speculation.
7. **VPS L4-passthrough is the right design but is under-documented in the wild.** Most Pangolin/VPS guides terminate TLS at the VPS. The passthrough variant needs a first-party, tested walkthrough or operators will follow a blog post and hand their plaintext to a rented box — defeating the entire point of the architecture.
8. **Not researched:** web push notification delivery when the app is only reachable over a VPN or LAN. Push requires the *browser vendor's* push service to reach a public endpoint, so tier 0/3 installs likely get no background notifications. This may be the strongest practical argument for tier 1 over tier 3 for a chat app, and it deserves its own investigation before the README recommends tier 3 to anyone.

---

## Sources

**Authentication & cryptography**
- [OWASP Password Storage Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html) — Argon2id parameters, scrypt/bcrypt/PBKDF2 fallbacks, peppering
- [OWASP Session Management Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Session_Management_Cheat_Sheet.html) — entropy, cookie attributes, `__Host-`, timeouts, rotation, `Clear-Site-Data`
- [OWASP Multifactor Authentication Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Multifactor_Authentication_Cheat_Sheet.html) — recovery codes, reauthentication, SMS as restricted
- [NIST SP 800-63B-4](https://nvlpubs.nist.gov/nistpubs/SpecialPublications/NIST.SP.800-63B-4.pdf) (final, July 2025) — rate limiting, ≤100 consecutive OTP failures, password length, no composition rules
- [argon2-cffi — Choosing Parameters](https://argon2-cffi.readthedocs.io/en/stable/parameters.html) and [API reference](https://argon2-cffi.readthedocs.io/en/stable/api.html) — RFC 9106 profiles, ~50 ms default
- [PyOTP documentation](https://pyauth.github.io/pyotp/) · [RFC 6238](https://datatracker.ietf.org/doc/rfc6238/)
- [NCSC — Traditional user credentials vs FIDO2 credentials for personal use](https://www.ncsc.gov.uk/paper/traditional-user-and-fido2-credentials-personal-use) — user-verifying FIDO2 ≈ 2FA
- [MDN — Set-Cookie](https://developer.mozilla.org/en-US/docs/Web/HTTP/Reference/Headers/Set-Cookie) · [httpwg/http-extensions #2605](https://github.com/httpwg/http-extensions/issues/2605) — `Secure`/`__Host-` on `http://localhost` browser divergence

**WebAuthn / passkeys**
- [web.dev — RP ID deep dive](https://web.dev/articles/webauthn-rp-id) — legal RP ID values, no IPs, no public suffixes, ports excluded, localhost exception
- [W3C Web Authentication Level 3](https://www.w3.org/TR/webauthn-3/) (Candidate Recommendation, Jan 2026) · [w3c/webauthn #1358](https://github.com/w3c/webauthn/issues/1358) — RP ID as IP host string
- [web.dev — Related Origin Requests](https://web.dev/articles/webauthn-related-origin-requests) · [Corbado — ROR guide](https://www.corbado.com/blog/webauthn-related-origins-cross-domain-passkeys) — `.well-known/webauthn`, five-label minimum, browser support incl. Firefox 152 (May 2026)
- [MojoAuth — Choosing Your RP ID](https://mojoauth.com/blog/webauthn-rp-id-passkey-decision) — RP ID permanence
- [py_webauthn (duo-labs)](https://github.com/duo-labs/py_webauthn) · [PyPI `webauthn`](https://pypi.org/project/webauthn/) — v3.0.0, 2026-06-29, Python ≥3.10
- [Passkeys Substack — 95% of WebAuthn errors are not errors](https://passkeys.substack.com/p/95-of-webauthn-errors-are-not-errors) — iOS 26.2 `isUVPAA()` regression in WKWebView, fixed in 26.3
- [MojoAuth — Passkey Recovery and Lockout](https://mojoauth.com/blog/passkey-recovery-and-lockout-relying-parties-guide) — never let an account depend on a single passkey

**Reverse proxy & TLS**
- [Caddy — Automatic HTTPS](https://caddyserver.com/docs/automatic-https) — internal CA vs public ACME, root install caveats, IP behaviour, HTTP-01/TLS-ALPN-01 ports, on-demand TLS warning
- [Caddy releases](https://github.com/caddyserver/caddy/releases) — 2.11.x current
- [Caddy community — Wildcard with DuckDNS](https://caddy.community/t/wildcard-domain-with-duckdns/31024) — DNS-01 Caddyfile, xcaddy requirement
- [Let's Encrypt — 6-day and IP Address Certificates Generally Available](https://letsencrypt.org/2026/01/15/6day-and-ip-general-availability) · [Announcing Six Day and IP Address Certificate Options](https://letsencrypt.org/2025/01/16/6-day-and-ip-certs) — 160-hour lifetime, `shortlived` profile
- [Let's Encrypt — Rate Limits](https://letsencrypt.org/docs/rate-limits/) — eTLD+1 bucketing via the Public Suffix List
- [Bugzilla 1165730 — Adding DuckDNS.org to the Public Suffix List](https://bugzilla.mozilla.org/show_bug.cgi?id=1165730)
- [Public Suffix List — Learn more](https://publicsuffix.org/learn/)

**Remote access options**
- [Immich — Remote Access](https://docs.immich.app/guides/remote-access/) · [Jellyfin — Networking](https://jellyfin.org/docs/general/post-install/networking/) — comparable projects' stance: port-forwarding "not recommended"
- [immich-app #25183 — Uploads fail behind Cloudflare Free due to 100MB limit](https://github.com/immich-app/immich/discussions/25183)
- [Cloudflare — Updated ToS](https://blog.cloudflare.com/updated-tos) — removal of the HTML/non-HTML construct from §2.8, CDN-specific terms
- [Pangolin vs Cloudflare One](https://pangolin.net/news/pangolin-v-cloudflare) · [Self-hosted Cloudflare Tunnel alternative](https://leewc.com/articles/self-hosted-cloudflared-tailscale-alternative-pangolin/)
- [WireGuard vs Tailscale vs Headscale vs NetBird](https://serverside.com/en-gb/blog/wireguard-vs-tailscale-vs-headscale-vs-netbird) — DERP/TURN relay behaviour under CGNAT
- [.internal (Wikipedia)](https://en.wikipedia.org/wiki/.internal) · [IANA — Proposed Private-Use TLD](https://www.iana.org/news/2024/proposed-private-use-tld) — ICANN reservation, July 2024
- [FastAPI — Behind a Proxy](https://fastapi.tiangolo.com/advanced/behind-a-proxy/) · [Uvicorn deployment](https://uvicorn.dev/deployment/) — `--forwarded-allow-ips` spoofing risk
