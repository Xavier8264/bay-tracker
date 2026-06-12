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
#>
param(
    [int]$Port = 5000,
    [int]$Threads = 32,
    [string]$DataDir = "",     # empty => machine/user BAYTRACKER_DATA, else C:\BayTrackerData
    [switch]$Force             # take the port over if another process holds it
)

$ErrorActionPreference = "Stop"
$RepoDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $RepoDir

# --- 1. Resolve the data folder (parameter > existing env var > default) -----
if (-not $DataDir) {
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

# --- 4. Launch -------------------------------------------------------------------
$env:BAYTRACKER_DATA = $DataDir
$ipv4 = (Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
         Where-Object { $_.IPAddress -notmatch "^(127\.|169\.254\.)" } |
         Select-Object -First 1).IPAddress
if (-not $ipv4) { $ipv4 = "<this-pc-ip>" }

Write-Host "=== Bay Tracking server ===" -ForegroundColor Cyan
Write-Host "Code:  $RepoDir"
Write-Host "Data:  $DataDir"
Write-Host "URLs:  http://${ipv4}:$Port/dashboard   /console   /stats   /admin" -ForegroundColor Green
Write-Host "Leave this window open. Ctrl+C stops the server." -ForegroundColor Yellow
Write-Host ""

# Foreground, so the console shows the startup line (which states the resolved
# data folder) and any errors. The app itself creates/repairs the database on
# startup if anything is missing.
& $venvPy -m waitress --listen=0.0.0.0:$Port --threads=$Threads app:app
exit $LASTEXITCODE
