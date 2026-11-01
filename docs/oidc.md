# Outside sign-in (OpenID Connect)

Since 4.3 a profile can sign in with an account at an identity provider you run or trust
(Authentik, Keycloak, Authelia, Zitadel, Microsoft Entra ID, and others that speak OpenID
Connect), instead of typing its PIN or password. It is **off by default**, and only the
Owner (the first, protected Admin) can turn it on, because the provider then decides who
gets in.

What it does not do: it never creates a profile, never matches an account to a profile by
email, and never replaces the Owner's own PIN or password. A profile signs in with an
outside account only after someone linked the two on purpose. The security details are in
[security.md](security.md#outside-sign-in-openid-connect).

## Before you start

- **DisPatch needs an https address.** Set `DISPATCH_PUBLIC_URL=https://<your address>`
  (the environment, or Settings → System → Network → the public address), for example the
  address Tailscale Serve or Caddy gives you. The sign-in cookie only works over https, so
  the switch stays off until this is set.
- **The provider must keep its endpoints on its own host.** DisPatch fetches the
  provider's discovery document, its signing keys and its token endpoint only from the
  issuer's own https host (scheme, host and port). That closes a path for a forged
  discovery document to send DisPatch elsewhere, but it also **refuses providers whose
  keys or token endpoint live on another host**. Google is one: its issuer is
  `accounts.google.com`, its keys are on `www.googleapis.com` and its token endpoint on
  `oauth2.googleapis.com`. Self-hosted providers (Authentik, Keycloak, Authelia, Zitadel)
  and Microsoft Entra ID use one host and work.

## Set it up

1. **At the provider**, create a confidential web client (an "OAuth2/OpenID provider" or
   "application"). Redirect URI: `<your public address>/api/oidc/callback` (the Outside
   sign-in card shows the exact address). Scopes: `openid email profile`.
2. **In DisPatch**, as the Owner: Settings → System → **Outside sign-in**. Fill in:
   - **Issuer address**, exactly as the provider's discovery document writes it
     (`https://auth.example.net/application/o/dispatch/`; a trailing slash makes it a
     different issuer, so copy it with or without one as the provider does);
   - **Client ID** and **Client secret** (the secret goes to the secret store and is never
     shown again; leave the box empty later to keep it);
   - **Scopes** (must include `openid`) and the **Provider name on the button**.

   Tick **Turn on outside sign-in** and press **Save**. DisPatch reads the discovery
   document once to check it and refuses the settings if the document names a different
   issuer or puts its endpoints on another host.
3. **Link each person.** Sign in to a profile the usual way, then **Link sign-in** under
   Settings → Account and finish the round trip at the provider. The Owner can link any
   profile from People & Access. **Unlink sign-in** removes the link at any time.

From then on the sign-in screen shows a button with the provider name. One outside
account links one profile.

## What else to know

- The Owner always keeps a local PIN or password. Removing the last one while the Owner's
  profile is linked is refused (`oidc_last_credential`).
- A disabled profile cannot sign in this way, and failed attempts are throttled like a
  wrong PIN.
- Turning outside sign-in off hides the button and stops sign-ins with it; existing links
  stay until someone unlinks them. Resetting the Owner from the command line
  (`python -m app admin reset`) removes the Owner's links.
- The settings live in `<data>/oidc.yaml` and the links in the `oidc_links` table; the
  client secret travels in encrypted backups with the other keys.
- **Not yet checked against a real provider.** The test suite runs the whole flow against
  an in-process fake provider. Try it with your provider before you rely on it.
