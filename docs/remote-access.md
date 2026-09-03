# Reaching DisPatch Chat from outside your house

DisPatch does not depend on Tailscale, ZeroTier, or any other commercial mesh
VPN. This guide is the ladder: start at the top, stop at the first rung that
works for your situation.

The full reasoning — including the options rejected and why — is in
[design/remote-access-and-auth.md](design/remote-access-and-auth.md).

## The invariant

**TLS terminates on hardware you own.**

Everything below preserves that. It is why Cloudflare Tunnel is not the default
recommendation: it terminates your TLS, which means Cloudflare reads every
message in plaintext. That may be an acceptable trade for you — it is a
legitimate rung on this ladder — but it should be a decision, not a default.

A second reason the ladder is ordered this way: **your public hostname is
permanent.** Passkeys are cryptographically bound to it. Pick a name you control
and can keep, and you can change *how* traffic reaches your house — port
forward today, VPS tomorrow — without invalidating a single device's credentials.
Bind your identity to a transport (a Tailscale MagicDNS name, `*.trycloudflare.com`, a LAN IP)
and you cannot.

---

## Rung 1 — Reverse proxy with a real certificate (recommended)

**Use when:** you have a domain name and a routable IP address.

DisPatch never speaks TLS itself. Caddy sits in front, terminates TLS with a
Let's Encrypt certificate, and proxies to it. (A bare `uvicorn` process binds
loopback by default; the container publishes on `0.0.0.0` — set
`BIND_ADDR=127.0.0.1` in `.env` so only Caddy can reach the app.)

Caddy is a **compose profile** in this repository, not a separate file. Set
`SITE_ADDRESS` (and `ACME_EMAIL`) in `.env`, then:

```bash
docker compose --profile proxy up -d
```

`deploy/caddy/Caddyfile` is bind-mounted into the container by the profile —
there is nothing to copy into `/etc/caddy`.

Forward TCP **443** from your router to the host. Do not forward 8765.

**HTTP-01 is what the shipped setup uses**, and it is what works out of the box:
`caddy:2-alpine` contains no DNS-provider modules, and the shipped Caddyfile has
no `tls { dns … }` block. That is fine for the common case — a domain that
resolves to a routable address, with port 80 reachable.

**DNS-01 is better if you can get it** — it survives an ISP blocking port 80, it
is the only challenge that works for a name resolving to a private address
(Rung 3), and it issues wildcards. It needs a Caddy binary built with your DNS
provider's module, which means building a custom image (`caddy:2-builder` with
`xcaddy build --with github.com/caddy-dns/<provider>`) and adding the `tls`
block yourself. Not shipped, deliberately: the module is provider-specific and
the credentials are a secret this repository should never carry.

**Why Caddy and not nginx:** the WebSocket connection requires the `Host` header
to survive the proxy — DisPatch's cross-origin guard compares `Origin` against
`Host` and closes the socket if they disagree. Caddy preserves `Host` by
default. The nginx `proxy_pass` snippet that everyone copies does not, and the
failure is nasty: the page loads, REST works, and chat simply never updates,
with no error anywhere. If you must use nginx, set `proxy_set_header Host
$host;` and read [deploy-docker.md](deploy-docker.md#the-one-that-will-get-you-host-header-rewriting).

---

## Rung 2 — Behind CGNAT: a small VPS relay

**Use when:** your ISP gives you a shared address, so port forwarding cannot
work. Common with mobile broadband, Starlink, and much of Europe and Asia.

Test whether this is you: compare the address on your router's WAN page against
`curl -s https://ifconfig.me`. If they differ, you are behind CGNAT.

The shape: a €4/month VPS holds the public address. WireGuard connects it to
your home machine. The VPS forwards **raw TCP** on 443 — it does not terminate
TLS.

```
phone ──TLS──▶ VPS :443 ──raw TCP over WireGuard──▶ home Caddy ──▶ DisPatch
                (sees only ciphertext)              (TLS terminates HERE)
```

Because the VPS passes bytes through at layer 4, it never holds your
certificate's private key and cannot read your messages. If it is seized or
compromised, the attacker gets ciphertext and traffic timing.

Full setup: [design/remote-access-and-auth.md § 2](design/remote-access-and-auth.md).

---

## Rung 3 — LAN only

**Use when:** you only ever use DisPatch at home, or you would rather run a VPN
and keep nothing exposed.

This is a completely legitimate choice, and the most secure one. Two things to
get right:

**Get a real certificate anyway.** Browsers increasingly refuse to do useful
things over plain HTTP, and one of those things matters here: `navigator.
credentials` — the passkey API — is simply **absent** on an insecure origin.
Not degraded, absent. So on plain HTTP over your LAN, passkeys cannot be
offered at all and you are limited to password + TOTP.

Two ways to get a real certificate on a private network:

- **DNS-01 for a real domain that resolves to a private address.** Works
  properly, trusted by every device, no per-device setup. Recommended — but it
  needs the custom Caddy build described in Rung 1, since the shipped image
  cannot answer a DNS challenge.
- **`tls internal`** (Caddy's built-in CA). Easy, but every device must install
  the CA certificate, and iOS in particular makes that tedious.

**Do not use a bare IP address as your origin.** Even with a valid IP
certificate, WebAuthn cannot use an IP as a relying-party ID, so passkeys stay
unavailable. Use a hostname.

---

## The local viewer follows you

Whichever rung you land on, the **local viewer** works from it: a phone on
mobile data opens a file on the host the same way the desktop in the next room
does, because the server reads the bytes and the browser only renders them. No
filesystem access, no VPN-specific plumbing, nothing to configure per device.

Which is worth saying out loud in a remote-access guide: an unlocked session
from anywhere can read the folders you named as viewer roots. It is off until
you name one, it refuses secrets, dotfiles and system paths regardless, and
Safe Mode never reaches it at all — but the roots you choose are readable from
wherever your PIN travels. Choose them with that in mind; see
[security.md](security.md).

---

## Authentication

> **Not yet implemented.** This section describes the
> [design](design/remote-access-and-auth.md). Today DisPatch authenticates with
> a single shared PIN and has no TOTP or passkey support. The hostname advice
> above still matters — it is what makes the eventual migration painless.

Everything in this section is **roadmap, not behaviour** — it describes what
the design calls for, in the future tense of a plan. Nothing below exists in the
app today. Independent of how traffic reaches you, the design says:

- **Password + TOTP** is the floor. It works on every origin, forever, with no
  secure-context requirement. Every account has it.
- **Passkeys are better where they work** — phishing-resistant, and a
  user-verifying passkey counts as two factors on its own. Requires HTTPS and a
  stable hostname.
- **Recovery codes** are generated at enrolment. Print them. If you lose your
  second factor and your codes, recovery is a command on the host — which is
  the correct proof of ownership for self-hosted software, and is documented in
  [security.md](security.md).

**The relying-party ID is permanent.** It is your hostname, and changing it
invalidates every passkey on every device simultaneously. The design has
first-run setup confirm it for that reason. This is the one part of the section
that affects a decision you make *today*: choose a hostname you will keep.

---

## What about push notifications?

Web push requires the browser vendor's push service to reach a publicly
resolvable endpoint. On rung 1 and rung 2 this works. On a LAN-only or VPN-only
install it generally does not, and no amount of local configuration fixes it —
the constraint is in the browser, not in DisPatch.

If push matters to your household, that is a real argument for rungs 1–2.
