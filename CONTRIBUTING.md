# Contributing to DisPatch Chat

Thanks for taking the time. This document is short on purpose.

By participating you agree to abide by the
[Code of Conduct](CODE_OF_CONDUCT.md).

## Before anything else: no private data

DisPatch is developed against real, live installs — the maintainers' own chat
histories. **Never commit user data, hostnames, IP addresses, home directory
paths, credentials, or personal names.**

```bash
python3 scripts/scrub_check.py
```

This runs in CI and blocks the merge. It is not advisory. If it flags something
that is genuinely a documentation example, add an inline `scrub-ok` comment on
that line explaining why — the marker is deliberately visible so a reviewer sees
it.

### Install the hooks

Git hooks are not versioned, so nothing installs them for you. Run this once
after cloning:

```bash
sh scripts/install-hooks.sh
```

That installs three, each guarding a different thing a push publishes:

| Hook | Checks |
|---|---|
| `pre-commit` | the **staged content** of every file you are committing |
| `commit-msg` | the **commit message** |
| `pre-push`   | the **tree of every commit being pushed**, the **message of every outgoing commit**, and that no runtime-data file became tracked |

`pre-commit` reads the **git index**, not your working tree — staging a file and
then editing the secret out of the working copy does not get it past the hook,
because the commit would still carry it. `pre-push` reads the pushed **commit**
for the same reason one step further out: a clean checkout says nothing about
what the commit you are sending contains.

The installer will not overwrite a hook you wrote yourself; pass `--force` to
replace it.

### What the scanner covers

Built in, so every fork gets them:

* **Credentials** — OpenAI/Anthropic `sk-`, Stripe, the GitHub token family
  (`ghp_`/`gho_`/`ghu_`/`ghs_`/`ghr_`/`github_pat_`), GitLab, Slack, AWS,
  Google, literal `Bearer` tokens, JWTs, PEM private keys, password hashes,
  and hard-coded `password = "…"`-shaped assignments.
* **Hosts and networks** — RFC1918 addresses, CGNAT/Tailscale `100.64.0.0/10`
  host addresses (the range *itself*, as written in docs, is fine), Tailscale
  MagicDNS names and tailnet ids.
* **People** — email addresses other than `example.com`, `noreply@` and
  `…@users.noreply.github.com`; absolute `/home/<user>` and `/var/home/<user>`
  paths, with the documentation placeholders (`/home/you`, `/home/youruser`,
  `/home/app`, `/home/$USER`, …) allowed by name; uid-specific
  `/run/user/<uid>` paths.
* **Assistant provenance** — `claude.ai/code/session_…` links and
  `Claude-Session:` / `Co-Authored-By: Claude` trailers. The public log is a
  human record, and a session link points at a private transcript.
* **Runtime data** — `*.db`, `security.yaml`, `config.yaml`,
  `trusted-devices.yaml`, `.env` and `.env.*`, `media/`, `files/`, `backups/`,
  generated reaction images, and uploaded avatars (which are photographs of
  real people, and which no content rule could ever catch).

Plus **local rules**: `scripts/scrub-rules.local.txt`, one regex per line, for
your own name, hostnames and handles. That file is **git-ignored on purpose** —
publishing the list of words that must never be published is its own small
leak. The consequence is that CI, and any fresh clone, runs with **no**
personal-identifier rules; the scanner says so out loud when the file is
missing. Your **local hooks are the only gate that ever sees them**, which is
why installing the hooks is not optional if you fork this.

### Adding a `scrub-ok`

When a hit is genuinely a documentation example or a test fixture, put the
marker on that line in whatever comment syntax the file uses, with a reason:

```python
api_key = "s3cr3t-not-a-real-key-000"  # scrub-ok: fixture; must LOOK like a key
```

```html
<!-- scrub-ok: RFC 5737 documentation address -->
```

It suppresses **that line only**, and it is deliberately visible so a reviewer
sees it. A commit message cannot carry one — reword the message instead. If you
find yourself adding markers in bulk, the rule is wrong: fix the rule.

After changing `scrub_check.py` or the rules file, run its fixtures:

```bash
python3 scripts/scrub_check.py --selftest
```

## Ways to help, easiest first

**Translations.** Every string is in `frontend/static/locales/<lang>.json`.
Adding a language is one file — no code. Fixing an awkward phrase in a language
you speak is genuinely valuable; machine-adjacent translation is easy to spot
and unpleasant to read. See [docs/i18n.md](docs/i18n.md).

Run `python3 scripts/check-locales.py` before you send it. Note that **HTML in a
locale value is restricted**, and the checker enforces it: a handful of
formatting tags (`<strong>`, `<em>`, `<b>`, `<i>`, `<code>`, `<kbd>`, `<small>`,
`<span>`, `<br>`), and only `class` and `id` attributes on them. No links, no
`style`, no event handlers, no `javascript:` or `data:` URIs. This is not a
style rule: values marked `data-i18n-html` are written to `innerHTML`, so a
translation is executable content and a translation PR is the cheapest way into
this app. Keep the English string's tags and ids exactly, translate the words
between them, and you will never see the checker complain.

**Deployment reports.** Ran it on hardware or an OS we don't document? Tell us
what broke. Real friction on a real Pi is more useful than a feature request.

**Bugs.** See the template. A reproduction against a fresh install beats a
description.

**Features.** Open an issue before writing code. DisPatch is deliberately small
and the most common review outcome for an unsolicited large PR is "this is good
work, but it isn't this project" — which wastes your time, and that's on us for
not saying so first.

## Development

```bash
cd backend
uv sync
uv run pytest              # 800+ tests, about a minute
uv run uvicorn app.main:app --reload --port 8765
```

The frontend has **no build step**. ES modules and plain CSS, served as-is —
edit, reload. Two things this costs you:

- `?v=N` on every asset URL does the cache busting. Change a file, bump its
  `?v=`, and bump `CACHE` in `frontend/static/sw.js`. They must stay in sync or
  users get a half-updated app.
- Nothing type-checks your JavaScript. CI parses every module (`node --check`),
  which catches syntax errors and nothing else. Be careful.

## What we look for in a pull request

**Tests for behaviour that matters.** Especially anything touching auth, the
access tiers, uploads, or file serving. The suite has a strong hermetic style —
throwaway database, monkeypatched data directories, nothing touching a real
install. Follow `backend/tests/test_auth_gate.py`.

**Comments that say why.** This codebase's comments explain reasoning, not
mechanics — what the alternative was, what broke last time, which invariant a
line is defending. `# increment the counter` is noise; "the counter is read live
rather than snapshotted, so concurrent uploads contend on the same budget" is
the thing that stops someone reintroducing the bug. Match that.

**Security-relevant changes are held to a higher bar.** New endpoints inherit
the access gate. New WebSocket routes need inline authentication — HTTP
middleware does not run for the WebSocket scope, and this has bitten the project
before. Never "fix" a permission problem by loosening a gate.

**Small, focused diffs.** A refactor bundled with a fix makes both harder to
review and impossible to revert independently.

## Style

Python: 4 spaces, type hints on new functions, `ruff check` clean.
JavaScript: 2 spaces, ES modules, no new runtime dependencies without discussion
— the frontend vendors everything locally and never fetches from a CDN.

## Reviews

Maintainers are volunteers. A first response usually takes a few days. If a pull
request goes quiet for two weeks, a nudge is welcome and not rude.

Disagreement is fine and often useful. Bring a reason, or a test that
demonstrates the point.

## Security issues

Do **not** open a public issue. See [SECURITY.md](SECURITY.md).

## Third-party code

`frontend/static/vendor/` holds vendored libraries under their own licences —
inventory and full licence texts in
[frontend/static/vendor/README.md](frontend/static/vendor/README.md). If you
add or update a file there, update that inventory in the same pull request.
