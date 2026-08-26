# Development

## Running it

```bash
cd backend
uv sync
uv run uvicorn app.main:app --reload --port 8765
```

Data goes to `~/.local/share/local-chat` by default — XDG, so
`XDG_DATA_HOME` moves it with everything else on your machine. It is NOT a
`./data` directory beside the source, which matters the first time you run the
dev server on a box that already has a real install: point `DISPATCH_DATA_DIR`
somewhere throwaway before you start, or you are developing against your own
chat history.

```bash
DISPATCH_DATA_DIR=/tmp/dispatch-dev uv run uvicorn app.main:app --reload --port 8765
```

(Every setting answers to `DISPATCH_*` first and the legacy `LOCAL_CHAT_*`
second — see `backend/app/config.py`.)

**One process only.** Sessions, live WebSocket connections, rate-limit counters
and the background loops are all in-process state. The app takes an exclusive
lock on its data directory at startup and refuses to run twice against the same
one, so `--workers 4` fails loudly instead of producing an app that half-works
in ways nobody would trace back to a flag.

## The frontend has no build step

`frontend/static/` is served as-is: ES modules and plain CSS. Edit a file,
reload the page. That is a deliberate trade — it makes the app trivial to host
and to audit, and it costs you two things:

**Cache busting is manual.** Every asset URL carries `?v=N`. When you change a
file, bump its `?v=` everywhere it is referenced *and* bump `CACHE` in
`frontend/static/sw.js`. Miss one and users get a half-updated app: new HTML
against an old module, which usually shows up as a blank screen.

`grep -rn '<module>.js?v=' frontend/static/` finds every reference — a module
imported by three others has four places to change, not one. A **new** module
also has to be added to `SHELL` in `sw.js`, or it is the one file an offline
PWA cannot load.

**Nothing type-checks your JavaScript.** CI runs `node --check` on every module,
which catches syntax errors and nothing else.

Heavy vendor bundles load on demand — `highlight.js` on the first code block,
`xterm` when the terminal opens. If you add a dependency that only some users
need, follow that pattern (`loadScript` / `loadStyle` in `js/util.js`) rather
than adding a `<script>` tag that everyone pays for.

## Tests

```bash
cd backend && uv run pytest -q
```

800+ tests, about a minute. The suite is strictly hermetic: every test
gets a throwaway database and monkeypatched data directories, and nothing
touches a real install or the network. `backend/tests/test_auth_gate.py` is the
best model to copy — access control is the part most worth testing.

## Before you commit

```bash
python3 scripts/scrub_check.py      # blocks private data reaching the repo
python3 scripts/check-locales.py    # translations complete, consistent and safe
cd backend && uv run pytest -q
```

Both scripts have a `--selftest` mode that exercises their own security rules
(`scrub_check.py --selftest` proves `--staged` reads the git index rather than
the working tree and `--rev` reads a commit rather than the checkout, then runs
its positive/negative fixtures for every pattern class; `check-locales.py
--selftest` runs the HTML-injection fixtures). CI runs them; run them yourself
after touching either script.

The first one is not optional and runs in CI. Better, install the hooks once and
stop having to remember:

```bash
sh scripts/install-hooks.sh     # pre-commit, commit-msg, pre-push
```

Note that CI runs `scrub_check.py` **without** the personal-identifier rules —
`scripts/scrub-rules.local.txt` is git-ignored so the list of private words is
never published. Names and hostnames are therefore caught only by the local
hooks. See [CONTRIBUTING.md](../CONTRIBUTING.md).

## Host tools (the nightly timer and the avatar rotator)

Two of DisPatch's moving parts are **not** part of the app and are not copied
by a deploy, because systemd runs them from the operator's own `PATH`
directory rather than from the install tree:

| Tool | What it does |
|---|---|
| `scripts/local-chat-daily.sh` | nightly: creates each bot's dated thread, rotates avatars, prunes yesterday's empty dated threads |
| `scripts/dispatch-avatar-rotate` | picks today's face for every bot with an avatar bank and uploads it through the API |

They live in `scripts/` and reach the host through one documented step:

```bash
sh scripts/install-tools.sh            # dry run — what would change
sh scripts/install-tools.sh --go       # install into ~/.local/bin
sh scripts/install-tools.sh --check    # exit 1 if the installed copy has drifted
```

`--go` backs up whatever it replaces and writes via a rename, so a timer can
never start a half-written script. Put `--check` in whatever you use to verify
a deployment: it is the only thing that will tell you the host is running an
older copy than the repo.

Neither tool hardcodes a roster. `dispatch-avatar-rotate` rotates every bot
that has an avatar pool (`<data>/avatar-pool/<bot>/`) unless
`DISPATCH_ROTATE_BOTS` names them explicitly, and finds a pre-migration avatar
directory only when `DISPATCH_APP_DIR` is set. That is deliberate: the earlier
version carried a hardcoded install path, the directory moved, and daily
rotation was dead for two days while the failure was swallowed into a log on
tmpfs. Nothing here may fail quietly — a failed rotation now reaches the
journal, and the log lives beside the app's other logs.

## Shipping a change

Edit the repository, run the checks above, and open a pull request. Maintainers
deploy from `main`.

If you run DisPatch on your own hardware, treat the repository as upstream and
your install as downstream: pull this project onto the box like any other user
rather than editing the install in place. A backend change needs a restart; a
frontend-only change needs a hard refresh and the `sw.js` `CACHE` bump described
above.
