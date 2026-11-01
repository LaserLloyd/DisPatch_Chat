#Requires -Version 5.1
<#
.SYNOPSIS
    Start, stop and set up a DisPatch install made by the Windows installer.

.DESCRIPTION
    Lives next to the installed app ({app}\launcher.ps1). Windows PowerShell 5.1
    runs it (it ships with every supported Windows), so it avoids anything newer.

      -Action Setup      create the per-user folders, run `uv sync --frozen` and
                         register the per-user scheduled task (the installer runs this)
      -Action Open       start DisPatch if it is not running, then open the browser
                         (the Start-menu shortcut)
      -Action Start      start DisPatch in the background and wait until it answers
      -Action Stop       stop it
      -Action Status     print /api/health; exit 0 when it answers, 1 when not
      -Action Serve      run the server in the foreground (the scheduled task's action)
      -Action Uninstall  stop it and remove the task and the sign-in shortcut
                         (the uninstaller runs this; your data stays)

    Where things live:
      {app}                              the program (backend, frontend, uv.exe, Python)
      %LOCALAPPDATA%\DisPatch\data       your chats, settings and uploads (DISPATCH_DATA_DIR)
      %LOCALAPPDATA%\DisPatch\logs       launcher.log, setup.log, server.*.log
      %LOCALAPPDATA%\DisPatch\launcher.json   {"host": "127.0.0.1", "port": 8765}

    DisPatch listens on 127.0.0.1 (this computer only) unless launcher.json says
    otherwise. An unreadable or invalid launcher.json falls back to 127.0.0.1:
    a typo never opens the port to the network.
#>
[CmdletBinding()]
param(
    [ValidateSet('Setup', 'Open', 'Start', 'Stop', 'Status', 'Serve', 'Uninstall')]
    [string]$Action = 'Open',
    # Setup only: start DisPatch when this user signs in to Windows.
    [switch]$Autostart
)

Set-StrictMode -Version 3
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

# ----------------------------------------------------------------------------- paths
$AppDir      = $PSScriptRoot
$BackendDir  = Join-Path $AppDir 'backend'
$UvExe       = Join-Path $AppDir 'bin\uv.exe'
$PythonDir   = Join-Path $AppDir 'python'        # uv-managed CPython, removed on uninstall
$UvCacheDir  = Join-Path $AppDir 'uv-cache'      # removed on uninstall
$VenvPython  = Join-Path $BackendDir '.venv\Scripts\python.exe'
$Root        = Join-Path $env:LOCALAPPDATA 'DisPatch'
$DataDir     = Join-Path $Root 'data'
$LogDir      = Join-Path $Root 'logs'
$ConfigPath  = Join-Path $Root 'launcher.json'
$PowerShell  = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$TaskName    = 'DisPatch'
$TaskPath    = '\DisPatch\'
$StartupLink = Join-Path ([Environment]::GetFolderPath('Startup')) 'DisPatch.lnk'

# The interpreter uv installs. backend/.python-version allows 3.12-3.14; one fixed
# minor keeps every install on the version CI tested.
$PythonVersion = '3.13'
$DefaultHost   = '127.0.0.1'
$DefaultPort   = 8765
$StartTimeoutS = 120

# ----------------------------------------------------------------------------- helpers
function Write-Log([string]$Message) {
    $line = '{0}  {1}  {2}' -f (Get-Date -Format 's'), $Action, $Message
    try {
        if (-not (Test-Path -LiteralPath $LogDir)) { New-Item -ItemType Directory -Path $LogDir -Force | Out-Null }
        Add-Content -LiteralPath (Join-Path $LogDir 'launcher.log') -Value $line -Encoding UTF8
    } catch { }
    Write-Host $line
}

function Test-ValidHost([string]$Value) {
    if ($Value -eq 'localhost') { return $true }
    $ip = $null
    return [System.Net.IPAddress]::TryParse($Value, [ref]$ip)
}

function Get-LauncherConfig {
    $cfg = [ordered]@{ host = $DefaultHost; port = $DefaultPort }
    if (-not (Test-Path -LiteralPath $ConfigPath)) { return $cfg }
    try {
        $raw = Get-Content -LiteralPath $ConfigPath -Raw -Encoding UTF8 | ConvertFrom-Json
    } catch {
        Write-Log "launcher.json is not valid JSON; using $DefaultHost`:$DefaultPort"
        return $cfg
    }
    $names = @($raw.PSObject.Properties | ForEach-Object { $_.Name })
    if ($names -contains 'host') {
        $h = [string]$raw.host
        if (Test-ValidHost $h) { $cfg.host = $h }
        else { Write-Log "launcher.json host '$h' is not an IP address; using $DefaultHost" }
    }
    if ($names -contains 'port') {
        $p = 0
        if ([int]::TryParse([string]$raw.port, [ref]$p) -and $p -ge 1 -and $p -le 65535) { $cfg.port = $p }
        else { Write-Log "launcher.json port '$($raw.port)' is out of range; using $DefaultPort" }
    }
    return $cfg
}

# Where to knock: a wildcard bind answers on loopback; an IPv6 literal needs brackets.
function Get-ProbeUrl($Cfg) {
    $h = [string]$Cfg.host
    if ($h -eq '0.0.0.0') { $h = '127.0.0.1' }
    elseif ($h -eq '::') { $h = '[::1]' }
    elseif ($h.Contains(':')) { $h = "[$h]" }
    return "http://${h}:$($Cfg.port)"
}

function Get-Health($Cfg) {
    try {
        $r = Invoke-WebRequest -UseBasicParsing -Uri ((Get-ProbeUrl $Cfg) + '/api/health') -TimeoutSec 3
        if ($r.StatusCode -ne 200) { return $null }
        $body = $r.Content | ConvertFrom-Json
        if (@('ok', 'degraded') -contains [string]$body.status) { return $body }
    } catch { }
    return $null
}

# The server processes THIS install started: a python.exe under {app} running
# app.main:app. Matching on the full path and the command line means Stop never
# touches another Python program, or a second DisPatch from somewhere else.
# (A venv's python.exe is a small redirector that starts the real interpreter
# from {app}\python as a child, so both show up here.)
function Get-ServerProcess {
    $prefix = $AppDir.TrimEnd('\') + '\'
    @(Get-CimInstance -ClassName Win32_Process -Filter "Name = 'python.exe' OR Name = 'pythonw.exe'" |
        Where-Object {
            $_.ExecutablePath -and $_.CommandLine -and
            $_.ExecutablePath.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase) -and
            $_.CommandLine.Contains('app.main:app')
        })
}

function Get-DispatchTask {
    try { return Get-ScheduledTask -TaskName $TaskName -TaskPath $TaskPath -ErrorAction Stop } catch { return $null }
}

function Invoke-Logged([string]$Exe, [string[]]$Arguments, [string]$LogName) {
    $out = Join-Path $LogDir "$LogName.out.log"
    $err = Join-Path $LogDir "$LogName.err.log"
    $quoted = $Arguments | ForEach-Object { if ($_ -match '[\s"]') { '"' + ($_ -replace '"', '\"') + '"' } else { $_ } }
    $p = Start-Process -FilePath $Exe -ArgumentList $quoted -WorkingDirectory $BackendDir -NoNewWindow -Wait -PassThru `
        -RedirectStandardOutput $out -RedirectStandardError $err
    return $p.ExitCode
}

# ----------------------------------------------------------------------------- setup
function Install-Environment {
    foreach ($d in @($Root, $DataDir, $LogDir)) {
        if (-not (Test-Path -LiteralPath $d)) { New-Item -ItemType Directory -Path $d -Force | Out-Null }
    }
    if (-not (Test-Path -LiteralPath $ConfigPath)) {
        # Written once; an upgrade never overwrites the user's choice.
        $json = '{' + [Environment]::NewLine +
                '  "host": "' + $DefaultHost + '",' + [Environment]::NewLine +
                '  "port": ' + $DefaultPort + [Environment]::NewLine + '}'
        [System.IO.File]::WriteAllText($ConfigPath, $json + [Environment]::NewLine)
        Write-Log "wrote $ConfigPath (this computer only)"
    }
    if (-not (Test-Path -LiteralPath $UvExe)) { throw "uv.exe is missing from $UvExe; reinstall DisPatch" }

    # uv keeps its Python and its cache inside the program folder, so the
    # uninstaller removes them and nothing lands in the user's own uv setup.
    $env:UV_PYTHON_INSTALL_DIR = $PythonDir
    $env:UV_CACHE_DIR = $UvCacheDir
    $env:UV_PYTHON_PREFERENCE = 'only-managed'
    $env:UV_NO_PROGRESS = '1'
    foreach ($n in @('VIRTUAL_ENV', 'UV_PROJECT_ENVIRONMENT', 'UV_PYTHON', 'UV_OFFLINE', 'UV_NO_SYNC')) {
        Remove-Item -LiteralPath "Env:\$n" -ErrorAction SilentlyContinue
    }

    Write-Log "uv sync --frozen --no-dev --python $PythonVersion (downloads Python and the wheels on first install)"
    $code = Invoke-Logged $UvExe @('sync', '--frozen', '--no-dev', '--python', $PythonVersion) 'setup'
    if ($code -ne 0) { throw "uv sync failed with exit code $code; see $LogDir\setup.err.log" }
    if (-not (Test-Path -LiteralPath $VenvPython)) { throw "uv sync finished but $VenvPython is missing" }

    $code = Invoke-Logged $VenvPython @('-c', 'import fastapi, uvicorn, aiosqlite, PIL') 'setup-check'
    if ($code -ne 0) { throw "the installed environment cannot import its dependencies; see $LogDir\setup-check.err.log" }
}

function Invoke-Setup {
    Install-Environment
    Register-DispatchTask -AtLogon:$Autostart
    Write-Log 'setup finished'
}

# One per-user scheduled task runs the server (-Action Serve). With -AtLogon it
# also gets a sign-in trigger. Limited run level, the user's own account, no
# stored password: nothing here needs administrator rights.
function Register-DispatchTask([switch]$AtLogon) {
    $user = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
    $arg = '-NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File "' + $PSCommandPath + '" -Action Serve'
    $params = @{
        TaskName    = $TaskName
        TaskPath    = $TaskPath
        Description = 'Runs the DisPatch chat server for this user (127.0.0.1 unless launcher.json says otherwise).'
        Action      = New-ScheduledTaskAction -Execute $PowerShell -Argument $arg -WorkingDirectory $AppDir
        Principal   = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
        Settings    = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew `
                        -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
        Force       = $true
    }
    if ($AtLogon) { $params.Trigger = New-ScheduledTaskTrigger -AtLogOn -User $user }
    $registered = $false
    try {
        Register-ScheduledTask @params | Out-Null
        $registered = $true
        Write-Log ("scheduled task {0}{1} registered (start at sign-in: {2})" -f $TaskPath, $TaskName, [bool]$AtLogon)
    } catch {
        Write-Log "could not register the scheduled task: $($_.Exception.Message)"
    }
    if (Test-Path -LiteralPath $StartupLink) { Remove-Item -LiteralPath $StartupLink -Force }
    if ($AtLogon -and -not $registered) {
        # Fallback for machines where policy blocks per-user tasks: a shortcut
        # in the user's Startup folder does the same at sign-in.
        $shell = New-Object -ComObject WScript.Shell
        $lnk = $shell.CreateShortcut($StartupLink)
        $lnk.TargetPath = $PowerShell
        $lnk.Arguments = '-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "' + $PSCommandPath + '" -Action Start'
        $lnk.WorkingDirectory = $AppDir
        $lnk.WindowStyle = 7
        $lnk.Save()
        Write-Log "start at sign-in uses the Startup-folder shortcut $StartupLink"
    }
}

# ----------------------------------------------------------------------------- run
function Invoke-Serve {
    $cfg = Get-LauncherConfig
    if (Get-Health $cfg) { Write-Log 'already running'; return 0 }
    if (-not (Test-Path -LiteralPath $VenvPython)) {
        # Self-heal (an interrupted install, a deleted .venv). The task and its
        # sign-in trigger are left exactly as they are.
        Write-Log 'the Python environment is missing; rebuilding it first'
        Install-Environment
    }
    if (-not (Test-Path -LiteralPath $LogDir)) { New-Item -ItemType Directory -Path $LogDir -Force | Out-Null }
    foreach ($n in @('server.out.log', 'server.err.log')) {
        $f = Join-Path $LogDir $n
        if (Test-Path -LiteralPath $f) { Move-Item -LiteralPath $f -Destination ($f + '.1') -Force }
    }
    $env:DISPATCH_DATA_DIR = $DataDir
    $env:DISPATCH_HOST = [string]$cfg.host
    $env:DISPATCH_PORT = [string]$cfg.port
    $env:PYTHONUTF8 = '1'
    $env:PYTHONIOENCODING = 'utf-8'
    Remove-Item Env:\VIRTUAL_ENV -ErrorAction SilentlyContinue
    Remove-Item Env:\PYTHONHOME -ErrorAction SilentlyContinue
    Remove-Item Env:\PYTHONPATH -ErrorAction SilentlyContinue

    # One process, no --workers: sessions, sockets and the backup loops live in
    # process memory (see the Dockerfile's CMD for the same reasoning).
    $uvicornArgs = @('-m', 'uvicorn', 'app.main:app', '--host', [string]$cfg.host, '--port', [string]$cfg.port,
                     '--no-access-log', '--timeout-graceful-shutdown', '20')
    Write-Log ("starting on {0} (data: {1})" -f (Get-ProbeUrl $cfg), $DataDir)
    $p = Start-Process -FilePath $VenvPython -ArgumentList $uvicornArgs -WorkingDirectory $BackendDir -NoNewWindow -PassThru `
        -RedirectStandardOutput (Join-Path $LogDir 'server.out.log') -RedirectStandardError (Join-Path $LogDir 'server.err.log')
    $null = $p.Handle    # keeps the handle so ExitCode is readable after the wait
    $p.WaitForExit()
    Write-Log "server exited with code $($p.ExitCode)"
    return $p.ExitCode
}

function Invoke-Start {
    $cfg = Get-LauncherConfig
    if (Get-Health $cfg) { return $true }
    if (@(Get-ServerProcess).Count -eq 0) {
        $viaTask = $false
        if (Get-DispatchTask) {
            try {
                Start-ScheduledTask -TaskName $TaskName -TaskPath $TaskPath
                # The task scheduler refuses quietly in some sessions (no interactive
                # sign-in); give it a few seconds to show a server process.
                for ($i = 0; $i -lt 30 -and -not $viaTask; $i++) {
                    Start-Sleep -Milliseconds 500
                    $viaTask = @(Get-ServerProcess).Count -gt 0
                }
            } catch { Write-Log "the scheduled task did not start: $($_.Exception.Message)" }
        }
        if ($viaTask) {
            Write-Log 'started through the scheduled task'
        } else {
            Start-Process -FilePath $PowerShell -WindowStyle Hidden -ArgumentList @(
                '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-WindowStyle', 'Hidden',
                '-File', ('"' + $PSCommandPath + '"'), '-Action', 'Serve') | Out-Null
            Write-Log 'started the server directly'
        }
    }
    $deadline = (Get-Date).AddSeconds($StartTimeoutS)
    while ((Get-Date) -lt $deadline) {
        if (Get-Health $cfg) { Write-Log 'DisPatch is answering'; return $true }
        Start-Sleep -Seconds 1
    }
    Write-Log "DisPatch did not answer within $StartTimeoutS s; see $LogDir\server.err.log"
    return $false
}

function Invoke-Stop {
    $task = Get-DispatchTask
    if ($task -and $task.State -eq 'Running') {
        try { Stop-ScheduledTask -TaskName $TaskName -TaskPath $TaskPath } catch { }
    }
    for ($i = 0; $i -lt 10; $i++) {
        $procs = @(Get-ServerProcess)
        if ($procs.Count -eq 0) { break }
        foreach ($proc in $procs) {
            try { Stop-Process -Id $proc.ProcessId -Force -ErrorAction Stop } catch { }
        }
        Start-Sleep -Milliseconds 500
    }
    if (@(Get-ServerProcess).Count -gt 0) { Write-Log 'some server processes did not stop'; return $false }
    Write-Log 'stopped'
    return $true
}

function Show-Problem([string]$Text) {
    try { (New-Object -ComObject WScript.Shell).Popup($Text, 0, 'DisPatch', 16) | Out-Null } catch { }
}

# ----------------------------------------------------------------------------- main
try {
    switch ($Action) {
        'Setup' { Invoke-Setup; exit 0 }
        'Serve' { exit (Invoke-Serve) }
        'Start' { if (Invoke-Start) { exit 0 } else { exit 1 } }
        'Stop'  { if (Invoke-Stop) { exit 0 } else { exit 1 } }
        'Open' {
            if (Invoke-Start) {
                $cfg = Get-LauncherConfig
                Start-Process ((Get-ProbeUrl $cfg) + '/')
                exit 0
            }
            Show-Problem "DisPatch did not start. The log files are in $LogDir."
            exit 1
        }
        'Status' {
            $cfg = Get-LauncherConfig
            $h = Get-Health $cfg
            if ($h) { Write-Output ("{0} status={1}" -f (Get-ProbeUrl $cfg), $h.status); exit 0 }
            Write-Output ("{0} not answering" -f (Get-ProbeUrl $cfg)); exit 1
        }
        'Uninstall' {
            [void](Invoke-Stop)
            try { Unregister-ScheduledTask -TaskName $TaskName -TaskPath $TaskPath -Confirm:$false -ErrorAction Stop } catch { }
            if (Test-Path -LiteralPath $StartupLink) { Remove-Item -LiteralPath $StartupLink -Force }
            Write-Log "task removed; your data stays in $Root"
            exit 0
        }
    }
} catch {
    Write-Log ("failed: " + $_.Exception.Message)
    if ($Action -eq 'Open') { Show-Problem ("DisPatch could not start: " + $_.Exception.Message) }
    exit 1
}
