# Open items before the public release

Two kinds of thing live here: a short checklist of one-off actions that have to
happen at (or just after) publish time, and the app-code changes that would each
remove a workaround a user would otherwise have to understand.

Anything that has since shipped is moved to [Already done](#already-done) at the
bottom rather than deleted, so nobody re-litigates a settled decision.

---

## Before the first public release

Accurate as of today. Each one is an action on the repository or its settings,
not a code change.

- [ ] **Claim the `OWNER` placeholder** (see the command below).

      Substitute on `LaserLloyd/dispatch-chat`, never on bare `OWNER` — three real
      identifiers would otherwise be mangled: `cap_add: [CHOWN, FOWNER, …]` in
      `docker-compose.yml`, `CODEOWNERS` in `docs/i18n.md`, and
      `LOCAL_CHAT_MIRROR_OWNER` in `docs/design/multi-user.md` (the command
      below is already scoped that way). Nothing breaks
      while the placeholder stands: `docker compose up` builds locally because
      `IMAGE` is left unset, and the remaining sites are labels, documentation
      links and clone URLs. `.github/workflows/docker-publish.yml` needs no
      substitution in its live code — it uses `${{ github.repository }}` — only
      in its commented-out registry-cache example.
      Verify afterwards: `grep -rn OWNER . --exclude-dir=.git`.

  ```bash
  grep -rl 'LaserLloyd/dispatch-chat' --exclude-dir=.git . \
  | xargs sed -i 's|LaserLloyd/dispatch-chat|myuser/dispatch-chat|g'
  ```
- [ ] **Enable private vulnerability reporting** (Settings → Code security →
      Private vulnerability reporting). `SECURITY.md` and the issue-template
      chooser both point at Security Advisories; until it is on, the "Report a
      vulnerability" link 404s and the documented fallback is the only route.
- [ ] **Enable Discussions**, or remove the Discussions link from
      `.github/ISSUE_TEMPLATE/config.yml` — it points at
      `https://github.com/LaserLloyd/dispatch-chat/discussions`, which does not exist
      until the feature is switched on.
- [ ] **Add an About/Source link in Settings.** AGPL §13 obliges an operator to
      offer the source to network users; the README states the obligation but
      the app does not yet discharge it. A "Source" entry in Settings → About,
      pointing at the repository, closes it. (Frontend change — no environment
      variable: the URL should be a JS constant, not a setting.)
- [ ] **Tag `v1.0.0`.** `CHANGELOG.md` carries a `1.0.0 — unreleased` section.
      The publish workflow only tags `latest` from a `v*` tag, so until the tag
      exists nothing is published as `latest`.
- [ ] **Make the GHCR package public** after the first successful publish — it
      is private by default, and `docker pull` fails with "denied" until it is.

---

## 1. Avatars default to the data directory — DONE

**Was.** `config.py` hardcoded `AVATAR_DIR = FRONTEND_DIR / "avatars"`, so uploaded
avatars were written into the source tree: inside the read-only image in a container,
and inside the directory a bare-metal deploy overwrites everywhere else.

**Now.** `AVATAR_DIR` is `<data dir>/avatars`, beside `media/` and `files/`.
`/static/avatars/*` is mounted from there explicitly, ahead of the general `/static`
mount — Starlette matches mounts in order, so the specific one claims the path and the
URL is unchanged. No frontend change, no config-schema change, and the Safe-Mode path
checks were untouched.

The shipped fix is **not** the `DISPATCH_AVATAR_DIR` env var this section originally
proposed. An env var would have been one more thing to set correctly; deriving the path
from the data directory that every other piece of state already uses needs no
configuration at all.

**Upgrades.** An install whose old location still holds real avatar files keeps using
it, so nothing goes blank on first boot. "Holds real files" and not "the directory
exists" matters: the repository ships that directory with a `.gitkeep`, so an
existence check would have sent every fresh install straight back into the code tree.

The container plumbing that worked around the old layout — the subpath mount in
`docker-compose.yml` and the entrypoint's missing-mount warning — is gone with it,
along with the Docker Engine 26+ / Compose v2.26+ requirement it carried.

## 2. Make the agent backend pluggable (the big one)

Moved to its own document: [design/agent-backend.md](design/agent-backend.md).
Still open — the direct-LLM path added since covers conversational bots, but not
a remote *agent* with tools.

---

## 3. Repository hygiene still outstanding

- `backend/app/main.py` is 7,463 lines. Not a blocker for release, but it is the first
  thing a reviewer will comment on, and it is the reason `dashboard_routes.py` was
  split out rather than added to.

---

## 4. Decide the first-run posture

The app binds `127.0.0.1` by default and logs a loud warning at every startup while no
PIN is set and it is listening off-box (`_warn_if_wide_open()` in `main.py`). That is
the current answer, and it is a reasonable one. What is still a judgement call for the
release: whether a fresh install reachable from the LAN should refuse to serve anything
but the setup flow until a PIN exists, rather than warning and serving.

---

## Already done

Kept so these are not re-proposed. Each was verified in the tree, not just remembered.

- **Guard against multiple workers.** Shipped as something stronger than the suggested
  `WEB_CONCURRENCY` check: `_claim_single_instance()` takes a POSIX record lock
  (`fcntl.lockf`) on `<data>/.instance.lock` and exits with an explanation. It catches
  `--workers N`, a second `docker compose up` on the same volume, and a hand-started dev
  server racing the service — one mechanism, three bugs. Documented in
  `docs/development.md`.
- **Degraded-state signals in `/api/health`.** `db_integrity_ok`, `last_backup_ok`,
  `last_backup_at`, `gateway_ok` and `data_dir` are all reported, and the container
  HEALTHCHECK reads `db_integrity_ok`. The host dashboard covers the rest of what this
  item asked for. (The avatar directory is the one field still missing — folded into §1.)
- **`backend/app/main.py.bak-packregen`** is gone from the tree.
- **`LICENSE` added** (GNU AGPL v3), and the Dockerfile's
  `org.opencontainers.image.licenses` label corrected from `MIT` to `AGPL-3.0-only` to
  match it. If the intent was "or later", change the label to `AGPL-3.0-or-later` —
  it is a licensing statement, so it should say what the owner means.
- **Startup warning when the app is wide open**, plus the loopback-by-default bind in
  `config.py`.
