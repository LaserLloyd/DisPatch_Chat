# Windows installer

A per-user [Inno Setup](https://jrsoftware.org/isinfo.php) installer for DisPatch. No
Docker, no WSL, no administrator rights. The user guide is
[docs/deploy-windows.md](../../docs/deploy-windows.md); this page is for whoever builds it.

| File | What it does |
|---|---|
| `dispatch.iss` | The Inno Setup script (needs Inno Setup 6.3 or newer). |
| `launcher.ps1` | Installed next to the app. Sets up, starts, stops and opens DisPatch; the Start-menu shortcuts and the scheduled task call it. Windows PowerShell 5.1 compatible. |
| `build/` | Build inputs that are not in git: `uv.exe`, and uv's licence files. |
| `Output/` | Where `iscc` writes `DisPatch-Setup-<version>.exe`. |

## What the installer does

1. Copies the app (backend without its tests, `frontend/static`, `frontend/overlays`,
   `apps/`) and a bundled `uv.exe` to `%LOCALAPPDATA%\Programs\DisPatch`.
2. Runs `launcher.ps1 -Action Setup`, which
   - creates `%LOCALAPPDATA%\DisPatch\{data,logs}` and `launcher.json`
     (`{"host": "127.0.0.1", "port": 8765}`; an existing file is never overwritten),
   - runs `uv sync --frozen --no-dev --python 3.13` in `backend\`. uv downloads its own
     CPython into `{app}\python` and the wheels into `{app}\uv-cache`, so the first install
     needs an internet connection and nothing touches a Python the user already has,
   - checks the new environment imports its core packages,
   - registers the per-user scheduled task `\DisPatch\DisPatch` (limited rights, no stored
     password). With **Start DisPatch when I sign in** ticked it gets an at-sign-in trigger;
     if policy blocks per-user tasks, a Startup-folder shortcut does the same job.
3. Start menu: **DisPatch** (start if needed, then open the browser), **Stop DisPatch**,
   **DisPatch logs**, **Uninstall DisPatch**.

The server runs as `python -m uvicorn app.main:app --host <launcher.json host> --port
<port> --no-access-log --timeout-graceful-shutdown 20` with `DISPATCH_DATA_DIR`,
`DISPATCH_HOST` and `DISPATCH_PORT` set. One process, no `--workers` (see the Dockerfile's
`CMD` for why). An invalid `launcher.json` falls back to `127.0.0.1:8765`.

Upgrading = running a newer installer over the old one: it stops the server, replaces the
program tree (`backend\app`, `frontend`, `apps`), keeps `.venv` and uv's Python, and re-syncs.

Uninstalling stops the server, removes the task and every file under the program folder,
and **keeps `%LOCALAPPDATA%\DisPatch`** unless the user answers Yes to "Also delete your
DisPatch data?" (default No). A silent uninstall keeps it unless run with `/DELETEDATA=1`.

## Building locally (on Windows)

1. Install Inno Setup 6.3+.
2. Put a Windows x86_64 `uv.exe` in `packaging\windows\build\` (from the
   `uv-x86_64-pc-windows-msvc.zip` asset of a [uv release](https://github.com/astral-sh/uv/releases);
   use the version pinned as `UV_VERSION` in `.github/workflows/windows-installer.yml`), and
   optionally its `LICENSE-MIT` / `LICENSE-APACHE` as `uv-LICENSE-MIT` / `uv-LICENSE-APACHE`.
3. From a **clean checkout** (the script copies `frontend\static` as it is, so a working
   tree with someone's avatars in it would ship them):

   ```powershell
   cd packaging\windows
   & "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe" /DAppVersion=4.3.0 dispatch.iss
   ```

Silent install for testing: `DisPatch-Setup-<v>.exe /VERYSILENT /SUPPRESSMSGBOXES /CURRENTUSER /LOG=install.log`
(add `/TASKS="autostart"` for the sign-in task). The log ends with
`DisPatch setup finished: OK` when the Python step worked.

## CI

`.github/workflows/windows-installer.yml` runs on **Run workflow** and on `v*` tags only.
It checks `uv.lock` is current, parses `launcher.ps1` under both PowerShell editions,
builds the installer, installs it silently, waits for `/api/health`, checks the port is
bound to `127.0.0.1` only, stops the server, uninstalls, checks the data folder is still
there, and uploads the installer and the logs as workflow artifacts. It publishes nothing.
