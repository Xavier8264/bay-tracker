<#
    start.ps1 -- THE way to launch the Bay Tracking server by hand.

    Run from anywhere:

        powershell -ExecutionPolicy Bypass -File C:\BayTracking\start.ps1

    It removes every known way a manual launch can go quietly wrong:

      1. Always runs from THIS repo folder with THIS repo's venv -- so you can
         never accidentally serve a different copy of the code (e.g. a dev
         checkout in another folder) or use another checkout's venv.
      2. Pins BAYTRACKER_DATA for the server process (default C:\BayTrackerData,
         or whatever the machine/user environment variable already says), and
         refuses cloud-synced folders.
      3. Checks the port BEFORE launching. If something is already listening it
         names that process and stops -- because otherwise the new server dies
         with "address in use" while your browser keeps talking to the OLD
         (possibly broken) process. Use -Force to kill the old listener and
         take the port over.
      4. Verifies the venv + database exist (pointing you at setup.ps1 if not).

    Examples:
        .\start.ps1                  # normal launch on port 5000
        .\start.ps1 -Force           # kill whatever holds port 5000, then launch
        .\start.ps1 -Port 8080       # serve on a different port
        .\start.ps1 -Demo            # serve the DISPOSABLE demo database instead
                                     # (generates it on first use; the real log is
                                     #  never opened -- restart without -Demo to
                                     #  return to live data)
#>
param(
    [int]$Port = 5000,
    # Every open dashboard/console tab holds ONE waitress thread for the life of
    # its SSE connection; when all threads are held, every request (including
    # /healthz) queues forever and the whole plant looks OFFLINE. Keep this
    # comfortably above the number of screens that could ever be open at once.
    [int]$Threads = 64,
    [string]$DataDir = "",     # empty => machine/user BAYTRACKER_DATA, else C:\BayTrackerData
    [switch]$Force,            # take the port over if another process holds it
    [switch]$Demo              # serve the demo database (separate folder, "DEMO DATA" badge)
)

$ErrorActionPreference = "Stop"
$RepoDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $RepoDir

# --- 1. Resolve the data folder (parameter > existing env var > default) -----
if ($Demo) {
    # Demo mode serves a physically separate, disposable database. The real
    # data folder is not opened at all. make_demo_data.py refuses to write
    # anywhere that isn't a *_demo folder, so this cannot collide with the log.
    if ($DataDir -and -not $DataDir.ToLower().EndsWith("_demo")) {
        throw "-Demo requires a data folder ending in '_demo' (got: $DataDir)."
    }
    if (-not $DataDir) { $DataDir = "$env:SystemDrive\BayTrackerData_demo" }
} elseif (-not $DataDir) {
    foreach ($scope in @("Process", "User", "Machine")) {
        $v = [Environment]::GetEnvironmentVariable("BAYTRACKER_DATA", $scope)
        if ($v) { $DataDir = $v.Trim('"').Trim(); break }
    }
}
if (-not $DataDir) { $DataDir = "C:\BayTrackerData" }
if ($DataDir -match "OneDrive|Dropbox|Google Drive") {
    throw "BAYTRACKER_DATA points at a cloud-synced folder ($DataDir). Cloud sync corrupts live SQLite files -- use a plain local path like C:\BayTrackerData."
}

# --- 2. Verify the install -----------------------------------------------------
$venvPy = Join-Path $RepoDir "venv\Scripts\python.exe"
if (-not (Test-Path $venvPy)) {
    throw "No venv found at $venvPy. Run setup first:  powershell -ExecutionPolicy Bypass -File .\setup.ps1"
}
if (-not (Test-Path (Join-Path $RepoDir "app.py"))) {
    throw "app.py not found next to start.ps1 -- this script must live in the repo folder."
}

# --- 3. Make sure the port is actually free ------------------------------------
$listener = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
if ($listener) {
    $owner = Get-CimInstance Win32_Process -Filter "ProcessId = $($listener.OwningProcess)" -ErrorAction SilentlyContinue
    $desc = if ($owner) { "$($owner.Name) (PID $($owner.ProcessId))`n    $($owner.CommandLine)" } else { "PID $($listener.OwningProcess)" }
    if ($Force) {
        Write-Warning "Port ${Port} is held by: $desc"
        Write-Warning "Killing it (-Force given)..."
        Stop-Process -Id $listener.OwningProcess -Force
        Start-Sleep -Seconds 1
    } else {
        Write-Host ""
        Write-Host "Port ${Port} is ALREADY IN USE by:" -ForegroundColor Red
        Write-Host "    $desc"
        Write-Host ""
        Write-Host "If that is an old/stuck Bay Tracking server, re-run with -Force to replace it:" -ForegroundColor Yellow
        Write-Host "    powershell -ExecutionPolicy Bypass -File .\start.ps1 -Force"
        Write-Host "(Launching anyway would fail with 'address in use' while browsers keep"
        Write-Host " talking to the old process -- which is how you get a blank dashboard.)"
        exit 1
    }
}

# --- 3b. Demo mode: (re)generate the example dataset, anchored to NOW -----------
# The demo's live times are counted from each open run's start up to "now", so a
# demo left sitting for hours/days shows runs that have been "open" for 15-50+
# hours -- nonsense for a pitch. The data is disposable and deterministic, so we
# rebuild it on every -Demo launch, anchored to the moment you start the server.
# Result: a freshly launched board always reads realistically (elapsed < 13h,
# total < 16h). If a long-running demo drifts up during the day, just restart.
if ($Demo) {
    $demoDb = Join-Path $DataDir "baytracker.db"
    if (Test-Path $demoDb) {
        Write-Host "Refreshing demo dataset (anchored to now)..." -ForegroundColor Yellow
    } else {
        Write-Host "Generating demo dataset (first use)..." -ForegroundColor Yellow
    }
    $genArgs = @((Join-Path $RepoDir "make_demo_data.py"), "--data-dir", $DataDir)
    if (Test-Path $demoDb) { $genArgs += "--fresh" }   # replace the existing demo file
    & $venvPy $genArgs
    if ($LASTEXITCODE -ne 0) { throw "Demo data generation failed (see message above)." }
}

# --- 4. Launch -------------------------------------------------------------------
$env:BAYTRACKER_DATA = $DataDir
# Prefer the adapter that owns the default route -- the address other machines
# actually reach this PC on. Hyper-V/WSL/VMware virtual NICs (often 172.x) have
# no gateway, so this skips them and picks the real Wi-Fi/Ethernet IP. Falls
# back to the first real IPv4 if there's no default route (e.g. isolated LAN).
$ipv4 = $null
try {
    $ifIndex = Get-NetRoute -DestinationPrefix '0.0.0.0/0' -ErrorAction Stop |
               Sort-Object RouteMetric, InterfaceMetric |
               Select-Object -First 1 -ExpandProperty ifIndex
    if ($ifIndex) {
        $ipv4 = (Get-NetIPAddress -AddressFamily IPv4 -InterfaceIndex $ifIndex -ErrorAction Stop |
                 Where-Object { $_.IPAddress -notmatch "^(127\.|169\.254\.)" } |
                 Select-Object -First 1).IPAddress
    }
} catch { }
if (-not $ipv4) {
    $ipv4 = (Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
             Where-Object { $_.IPAddress -notmatch "^(127\.|169\.254\.)" } |
             Select-Object -First 1).IPAddress
}
if (-not $ipv4) { $ipv4 = "<this-pc-ip>" }

Write-Host "=== Bay Tracking server ===" -ForegroundColor Cyan
if ($Demo) {
    Write-Host ">>> DEMO MODE: serving EXAMPLE data from $DataDir <<<" -ForegroundColor Yellow
    Write-Host ">>> The real log is untouched. Restart without -Demo to go live. <<<" -ForegroundColor Yellow
}
Write-Host "Code:  $RepoDir"
Write-Host "Data:  $DataDir"
Write-Host "URLs:  http://${ipv4}:$Port/dashboard   /console   /stats   /admin" -ForegroundColor Green
Write-Host "Leave this window open. Ctrl+C stops the server." -ForegroundColor Yellow
Write-Host ""

# Foreground, so the console shows the startup line (which states the resolved
# data folder) and any errors. The app itself creates/repairs the database on
# startup if anything is missing.
#
# The launch runs in a relaunch loop: on an unattended floor PC a one-off crash
# (transient I/O error, OOM) must not mean "dark kiosks until a human clicks
# the icon". A clean exit or Ctrl+C (STATUS_CONTROL_C_EXIT) ends the loop; a
# crash relaunches after a pause. If another process has taken the port in the
# meantime (an update.ps1/-Force restart replaced us), we bow out instead of
# fighting it for the port forever.
$ctrlC = -1073741510   # STATUS_CONTROL_C_EXIT
while ($true) {
    & $venvPy -m waitress --listen=0.0.0.0:$Port --threads=$Threads app:app
    $code = $LASTEXITCODE
    if ($code -eq 0 -or $code -eq $ctrlC) { exit $code }

    $newHolder = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    if ($newHolder) {
        Write-Warning "Port $Port is now served by another process (a restart replaced this window). Not relaunching."
        exit 0
    }

    $stamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    try {
        Add-Content -Path (Join-Path $DataDir "server.err.log") -Value "$stamp waitress exited unexpectedly (code $code); relaunching"
    } catch { }
    Write-Warning "Server exited unexpectedly (code $code). Relaunching in 10 seconds... (Ctrl+C to stop for good)"
    Start-Sleep -Seconds 10
}
