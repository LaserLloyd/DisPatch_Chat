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

**The themes are a vendored bundle, not code.** `frontend/static/ui-theme/` is
the ForgeUI 1.2 bundle (the `ThemeForge` repository), copied in whole and
verified by `files.json`: `ui-theme.js` (the theme runtime, the first and only
blocking script in `<head>`), `ui-theme.css` (the tokens for every theme),
`ui-components.css` / `ui-components.js` (the component layer and its
`UIComponents` helpers) with the `locales/` string tables, and `VERSION`
(`ThemeForge <version> <digest>`). Never edit a bundle file: a colour fix
belongs upstream in the theme repository.

Three things live in this repository instead:

- **`forgeui.json`** (repo root) names the folder (`dest`), the bundle versions
  this app accepts (`"1.2"` is any 1.2.x), the adapters to ship (none: DisPatch
  has its own element layer) and the theme choice (`settings.themes`, `default`,
  `storage_key`, which must stay `dispatch-palette`).
- **The overlay** `frontend/overlays/dispatch-extra.css` (listed in
  `extra_files`) maps DisPatch's own token names (`--bg-*`, `--user-bubble`,
  `--md-*`, ...) onto the contract. It is app-owned and copied to
  `ui-theme/adapters/dispatch-extra.css`; edit the source in `overlays/`, never
  the copy, and never append anything into a bundle file.
- **The picker settings** are the `data-*` on the runtime's `<script>` tag in
  `index.html`. They repeat `settings` from `forgeui.json`; a test fails if the
  two disagree.

To update the themes, install the `forgeui` package (not from PyPI: that name
belongs to another project), then replace the folder and commit the result:

```sh
uv venv .forgeui-venv        # keep it out of git
uv pip install --python .forgeui-venv "forgeui @ git+https://github.com/LaserLloyd/ThemeForge@v1.2.0"
.forgeui-venv/bin/python -m forgeui vendor           # write the new copy (VERSION last)
.forgeui-venv/bin/python -m forgeui vendor --check   # exit 1 when the copy drifted
.forgeui-venv/bin/python -m forgeui vendor --source forgeui-1.2.0.zip   # offline: a zip, wheel or folder
```

(On Windows the interpreter is `.forgeui-venv\Scripts\python`.) `vendor` verifies
every file against the bundle's `files.json` before writing, refuses to overwrite
a bundle file edited by hand unless you pass `--force`, and removes files the new
bundle dropped. `--check` is what CI runs (the Lint job) and what
`backend/tests/test_forgeui_vendor.py` runs when `forgeui` is installed; the same
test checks the hashes, the overlay and the picker settings with the standard
library alone. The old `tools/sync_theme.py` registry command is gone; do not
look for it.

After an update: the folder's URLs carry no `?v=`, the server sends them
`no-cache` (`main.py`) and `sw.js` is network-first, so a new copy is live on the
next page load. Bump `CACHE` in `sw.js` only when the list of files in `SHELL`
changes (a file added, renamed or removed). DisPatch-only colour roles and layout
tokens live in `frontend/static/theme.css`.

`grep -rn '<module>.js?v=' frontend/static/` finds every reference — a module
imported by three others has four places to change, not one. A **new** module
also has to be added to `SHELL` in `sw.js`, or it is the one file an offline
PWA cannot load.

**Nothing type-checks your JavaScript.** CI runs `node --check` on every module,
which catches syntax errors and nothing else.

Heavy vendor bundles load on demand — `highlight.js` on the first code block.
If you add a dependency that only some users
need, follow that pattern (`loadScript` / `loadStyle` in `js/util.js`) rather
than adding a `<script>` tag that everyone pays for.

## Tests

```bash
cd backend && uv run pytest -q
```

About 3,000 tests; the backend suite takes 15–22 minutes (it is wall-clock bound).
Three timing/mtime tests are known to flake on a fast box
(`test_mirror.py::test_state_roundtrip`, `test_reactions.py::test_safe_ids_does_not_rewalk_unchanged_pools`, and one of the
dashboard/advisor timing tests). The suite is strictly hermetic: every test
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

### Dependency audit

CI's `audit` job runs `pip-audit` against the exact locked set and fails on any
known advisory. Run the same check locally after touching `backend/pyproject.toml`
or `backend/uv.lock`:

```bash
cd backend
uv export --frozen --no-hashes --no-emit-project --all-extras > /tmp/requirements-audit.txt
uvx pip-audit==2.10.1 -r /tmp/requirements-audit.txt --no-deps --disable-pip --strict
```

To fix a finding, raise the floor in `pyproject.toml` (a transitive package such
as `starlette` or `anyio` gets an explicit lower bound with a comment) and move
only that package: `uv lock --upgrade-package <name>==<fixed version>`. Prefer
that to an `--ignore-vuln`; if one is ever unavoidable, add it to the CI step
with a comment naming the advisory, the reason and a date to revisit.

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

### Windows

The backend imports and the whole suite *collects* on native Windows (`uv run pytest --co -q`:
2,900+ tests). Run it with `PYTHONUTF8=1` (the app reads YAML with the locale encoding in a
few places). A subset of tests that depend on POSIX paths, `/tmp` semantics or process groups
still fail on Windows; **the full suite is run in WSL** (or any Linux):

```
cd backend && uv sync --frozen && DISPATCH_DATA_DIR=$PWD/../.scratch-data python -m pytest -q -p no:cacheprovider
```

Never point `DISPATCH_DATA_DIR` under `/tmp` for a full run — the suite's write guard treats
`/tmp` as disposable. Frontend: `cd frontend && npm ci && npm run test:ci` works on Windows,
macOS and Linux (`CI_REQUIRE_DOM=1` is set by `scripts/test-ci.mjs`). `.gitattributes` forces
LF endings so a Windows checkout does not break the source-scan tests.

### Contracts and the access-control tests

`python scripts/gen_contracts.py` regenerates `docs/contracts/` and `docs/errors.md`
(`--check` verifies; `tests/test_contracts_drift.py` runs it). Adding a route means adding it
to `backend/app/route_policy.py` — `tests/test_route_table.py` fails the build otherwise —
and any new `HTTPException` message to `backend/app/contracts/error_catalog.json`
(`tests/test_error_catalog.py`). `tests/test_g5_access.py` calls every protected route with no
session and with a Restricted session and expects a refusal. Use the `world` fixture
(`tests/conftest.py`) for tests that need an owner, profiles and clients with chosen peers.
