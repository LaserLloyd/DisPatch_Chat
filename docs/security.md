# Security guide for operators

To report a vulnerability, see [SECURITY.md](../SECURITY.md). This page is about
running a DisPatch Chat install safely.

Most of what follows is checked automatically. Open **Settings → Dashboard**;
anything wrong appears as a finding with the fix attached. Check it after any
change to how the app is exposed.

## What you are trusting

Be clear about this before you put private conversations in it.

**The host is the trust boundary.** Anyone with shell access, or read access to
the data directory, can read every message and file. The database is a plain
SQLite file. If you need protection against someone taking the disk, use
full-disk encryption — DisPatch cannot provide it.

**Messages are not end-to-end encrypted**, and cannot be in an app shaped like
this: the server stores history, indexes it for search, and hands text to agent
backends. Transport is protected by TLS. Storage is protected by your
filesystem. Anyone marketing an app like this one as "E2E encrypted" is
misleading you about one of those three.

**Multi-user is isolation, not adversarial separation.** One member must not be
able to read another's conversations; if they can, it is a bug and we want the
report. But DisPatch is built for a household or small team, and it is not
designed to resist a determined insider who also controls the host.

**Agent backends run as your user.** A bot is a local process with your
permissions. Who you let talk to a bot is a real privilege decision, not a
cosmetic one.

## Checklist

> **Status note.** DisPatch is currently **single-account**: one PIN grants full
> access, and there are no per-person accounts, TOTP or passkeys yet. Where this
> page and [remote-access.md](remote-access.md) describe those, they describe
> the [design](design/remote-access-and-auth.md), not shipped behaviour. Read
> them as "what to plan for", and treat the PIN as the whole of your
> authentication today.

**Set a PIN immediately.** With no account configured, every route
is open — correct for the first two minutes on a laptop, dangerous anywhere
else. If the app is bound to anything other than `127.0.0.1` with no account
set, it says so loudly in the log and on the dashboard.

**Do not expose port 8765 to the internet directly.** Port-forward 443 to a
reverse proxy that terminates TLS, never 8765 to the app. On a LAN it is
perfectly normal to reach the app on 8765 — that is what the deployment guides
set up — but the moment it is reachable from outside your network it belongs
behind TLS. See [remote-access.md](remote-access.md).

**Keep the recovery code.** It is written to `RECOVERY-CODE.txt` in the data
directory on first run. Losing both it and the PIN means editing
`security.yaml` on the host to clear the PIN — which is the right proof of
ownership for self-hosted software, but it means you need shell access.

**Think about the limited tier.** DisPatch can serve a restricted view with no
password — useful for a tablet on the kitchen counter. Anyone who reaches the
port gets that view. It is confined to bots and conversations you mark
accessible, it cannot download or browse files, and it cannot change anything.
Decide deliberately which conversations belong in it, and turn it off entirely
if the app is reachable from the internet.

**Watch the quotas.** Limited-tier clients have daily per-device budgets for
uploaded bytes, messages sent, and conversations created. The message budget
matters most: every message to a bot spawns an agent turn, which may cost real
money. Tune with `DISPATCH_DECOY_*` — see [configuration.md](configuration.md).

**Run one process.** Sessions, connections and rate limits are in-process state.
The app takes an exclusive lock on its data directory and refuses to start
twice, so `--workers 4` fails loudly rather than half-working.

**Do not run as root.** The container image runs as UID 1000. If you run bare,
use a dedicated service account.

**Check your backups restore.** DisPatch takes rotating integrity-checked
snapshots, but a snapshot is not a backup until it is off the machine and you
have restored one. Note that snapshots cover the database, not the media and
file blobs — copy the whole data directory.

## The local viewer

The local viewer hands a browser bytes from the host's filesystem, which makes
it the widest read surface in the app. Its threat model, in full:

- **Unlocked operator only.** Safe Mode never renders the affordance, *and* the
  server refuses every viewer route for a limited session. Both halves are
  required; neither is sufficient. It is also deliberately absent from the
  machine-inbound surface, so the inbound API token — which belongs to on-box
  automation and, in some setups, a separate render machine — is not a key to
  your home directory.
- **Allowlist, not blocklist, first.** Nothing is served unless it resolves
  (symlinks followed) to inside a directory you configured. The default
  configuration is *no directories*, so the feature does nothing until you turn
  it on.
- **A deny list your configuration cannot override** sits on top of that: SSH
  and GPG keys, secret stores, agent state, `/etc /proc /sys /dev`, systemd
  units, this app's own credentials and database, and a filename pattern list
  for keys, `.env` files and databases. A root of `~` is therefore usable
  without exposing `~/.ssh`.
- **Dotfiles below a root are refused** unless you ask for them. Dotfiles
  *above* a root are your business: `~/.agent/workspace` is a legal root.
- **One refusal message.** Outside a root, on the deny list, or hidden — all
  three answer `Not served by the local viewer`. The client is never told which,
  so the endpoint cannot be walked to map the disk. Every refusal is logged with
  the path and the caller, and counted in `/api/health` as `viewer_denied_24h`.
- **Framing is sandboxed with an opaque origin.** HTML is served with
  `Content-Security-Policy: sandbox allow-scripts allow-forms allow-popups
  allow-modals allow-downloads; frame-ancestors 'self'` — never
  `allow-same-origin`. A framed page can run its own scripts but cannot read
  DisPatch's DOM, cookies or storage.
- **Framed pages load their assets through a scoped ticket, not the cookie.**
  An opaque origin sends no cookie, so a framed page is served from
  `/local/view/<ticket>/…`: a random capability minted by the authenticated
  `stat` call, bound to the caller's address, expiring after two idle hours,
  and valid only inside the page's own directory. A hostile HTML file you open
  can `fetch()` its own folder and nothing else; the ticket route refuses
  parent, sibling and symlink escapes with the same uniform message and is
  never on the machine-inbound surface. Every non-HTML
  viewer response gets `default-src 'none'; sandbox; frame-ancestors 'self'`,
  and all of them get `nosniff`, `Referrer-Policy: no-referrer`,
  `Cache-Control: private, max-age=0, must-revalidate` and
  `X-Robots-Tag: noindex`.
- **Content types come from a fixed extension table, never from sniffing**, so a
  `.txt` that begins with `<html>` cannot become an active document, and an
  unknown extension is sent as an attachment rather than rendered.
- **Directory listings never name an entry the file route would refuse**, so a
  listing cannot be the disclosure.

What it does *not* protect against: an operator who deliberately roots the
viewer at a directory full of things they did not want to share, or a **hard
link** inside a root to a file outside it (a hard link has no target to
resolve; only symlinks are seen through). The allowlist is the control; choose
it deliberately. The `*secret*` and `*credentials*` filename patterns are
deliberately broad and will also hide innocent files with those words in their
names. See
[configuration.md](configuration.md#local-viewer).

## Privacy mode

For a device you would rather leave no trace on — a shared tablet, a phone that
travels — privacy mode makes the client hold nothing locally:

- The service worker is unregistered and its cache purged, so no application
  shell or content is stored offline.
- Preferences move to session storage, so they die with the tab.
- The session cookie becomes non-persistent, and "remember this device" is
  refused.
- Drafts are never saved.

The cost is honest: the app will not work offline and will be slower to start,
because nothing is cached. Enable it per device in **Settings → Privacy**.

Note what this does *not* do. It stops the device retaining data. It does not
hide anything from the server, which still stores the full history — that is
what the delete controls are for.

## Reasonable expectations

DisPatch is a small application maintained by volunteers. It has had several
focused security reviews, it fails closed by default, and its access control is
covered by tests. It has not had a professional audit or a formal threat-model
review.

Run it for your household. Don't run it for a newsroom's source communications.
