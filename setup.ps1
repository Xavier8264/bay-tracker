<#
    setup.ps1 -- one-time install on a Windows floor PC.

    What it does (idempotent -- safe to re-run):
      1. Finds Python 3 and creates a repo-local virtual environment (venv).
      2. Installs the exact pinned dependencies from requirements.txt.
      3. Sets the BAYTRACKER_DATA environment variable to a data folder that
         lives OUTSIDE this repo (so updating the code can never touch the log).
      4. Creates the database and seeds structural defaults (non-destructive).
      5. (Optional) Opens the firewall port.
      6. (Optional) Installs an auto-start, auto-restart Windows service via NSSM.
      7. (Optional) Registers a daily database-backup scheduled task.

    IMPORTANT: the data folder must NOT be inside a OneDrive/Dropbox-synced
    location -- cloud sync corrupts live SQLite files. The default C:\BayTrackerData
    is a safe local path.

    Examples:
      # Basic install (run from the repo folder):
      powershell -ExecutionPolicy Bypass -File .\setup.ps1

      # Air-gapped install (no internet -- installs from the vendored .\wheelhouse):
      powershell -ExecutionPolicy Bypass -File .\setup.ps1 -Offline

      # Full production install (run an elevated PowerShell):
      powershell -ExecutionPolicy Bypass -File .\setup.ps1 -OpenFirewall -InstallService -ScheduleBackup

      # ...and also copy each backup off-machine to a share:
      powershell -ExecutionPolicy Bypass -File .\setup.ps1 -ScheduleBackup -BackupDest "\\server\share\baytracker"
#>
param(
    [string]$DataDir = "C:\BayTrackerData",   # where the database + backups live (OFF OneDrive!)
    [int]$Port = 5000,                          # the single port to serve on
    [int]$Threads = 32,                         # waitress threads (>= number of TVs + console + headroom)
    [string]$ServiceName = "BayTracker",
    [switch]$Offline,                            # install deps from the vendored .\wheelhouse (no internet)
    [switch]$OpenFirewall,                       # add the inbound firewall rule (needs admin)
    [switch]$InstallService,                     # install the NSSM auto-start service (needs admin)
    [switch]$ScheduleBackup,                     # register a daily DB-backup scheduled task (needs admin)
    [string]$BackupDest = "",                    # optional off-machine copy target (e.g. \\server\share)
    [string]$BackupTime = "02:00"                # daily backup time (HH:mm)
)

$ErrorActionPreference = "Stop"
$RepoDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $RepoDir
Write-Host "=== Bay Tracking setup ===" -ForegroundColor Cyan
Write-Host "Repo:  $RepoDir"
Write-Host "Data:  $DataDir"
Write-Host "Port:  $Port"

# --- 0. Warn if the data dir looks like a synced folder ----------------------
if ($DataDir -match "OneDrive|Dropbox|Google Drive") {
    Write-Warning "DataDir looks like a cloud-synced folder. Cloud sync CORRUPTS live SQLite files."
    Write-Warning "Choose a plain local path (e.g. C:\BayTrackerData) and back it up on a schedule instead."
}

# --- 1. Find Python 3 --------------------------------------------------------
$python = $null
foreach ($cmd in @("py", "python")) {
    $found = Get-Command $cmd -ErrorAction SilentlyContinue
    if ($found) {
        $verArg = if ($cmd -eq "py") { @("-3", "--version") } else { @("--version") }
        try { $v = & $found.Source @verArg; if ($v -match "Python 3") { $python = $found.Source; $pyPrefix = $verArg[0..($verArg.Count-2)]; break } } catch {}
    }
}
if (-not $python) { throw "Python 3 was not found. Install it from python.org (check 'Add Python to PATH')." }
Write-Host "Python: $python" -ForegroundColor Green

# --- 2. Create venv + install deps ------------------------------------------
$venvPy = Join-Path $RepoDir "venv\Scripts\python.exe"
if (-not (Test-Path $venvPy)) {
    Write-Host "Creating virtual environment..."
    if ($python -match "py.exe$") { & $python -3 -m venv venv } else { & $python -m venv venv }
}
# Offline installs (air-gapped floor PC) pull from the vendored .\wheelhouse so
# they never reach PyPI; -Offline is auto-enabled if the wheelhouse is present
# and no network is reachable, so the documented online flow is unchanged.
$wheelhouse = Join-Path $RepoDir "wheelhouse"
$haveWheels = (Test-Path $wheelhouse) -and (@(Get-ChildItem $wheelhouse -Filter *.whl -ErrorAction SilentlyContinue).Count -gt 0)
if (-not $Offline -and $haveWheels) {
    # Decide online vs. offline by actually reaching PyPI over HTTPS -- NOT an ICMP
    # ping. Corporate networks routinely block outbound ping while still allowing
    # HTTPS, so a ping test wrongly forces the (Python-version-specific) wheelhouse.
    $online = $false
    try { Invoke-WebRequest -Uri "https://pypi.org/simple/" -UseBasicParsing -Method Head -TimeoutSec 5 | Out-Null; $online = $true } catch {}
    if (-not $online) { $Offline = $true; Write-Host "PyPI not reachable over HTTPS -- using the vendored wheelhouse." }
}
if ($Offline) {
    if (-not $haveWheels) { throw "-Offline requested but no wheels found in $wheelhouse. Run 'pip download -r requirements.txt -d wheelhouse' on a connected machine first." }
    # The wheelhouse contains compiled, version-locked wheels (e.g. MarkupSafe and
    # charset-normalizer for cp314). If this venv's Python doesn't match them, pip
    # cannot satisfy the pins and aborts the WHOLE -r install -- leaving an empty
    # venv. Fail now with an actionable message instead of producing that.
    $pyVer = (& $venvPy -c "import sys;print(f'{sys.version_info[0]}.{sys.version_info[1]}')").Trim()
    $pyTag = "cp$($pyVer -replace '\.','')"
    $abi = Get-ChildItem $wheelhouse -Filter *-win_amd64.whl -ErrorAction SilentlyContinue
    if ($abi -and -not ($abi.Name -match $pyTag)) {
        $need = ($abi[0].Name -split '-')[-2]                                  # e.g. cp314
        $needVer = if ($need -match '^cp(\d)(\d+)$') { "$($Matches[1]).$($Matches[2])" } else { $need }
        throw "The offline wheelhouse's compiled wheels are built for Python $needVer ($need), but this venv uses Python $pyVer ($pyTag). pip cannot satisfy the pinned MarkupSafe/charset-normalizer, so an offline install would install nothing. Fix: install Python $needVer (python.org, 64-bit), delete the venv (Remove-Item -Recurse -Force venv) and re-run setup; OR rebuild the wheelhouse for Python $pyVer on a connected machine: py -3 -m pip download -r requirements.txt -d wheelhouse"
    }
    Write-Host "Installing pinned dependencies from vendored wheelhouse (offline)..."
    & $venvPy -m pip install --no-index --find-links $wheelhouse -r (Join-Path $RepoDir "requirements.txt")
    if ($LASTEXITCODE -ne 0) { throw "Dependency install FAILED (offline, exit $LASTEXITCODE). See the pip output above. The server cannot start until this succeeds." }
} else {
    Write-Host "Installing pinned dependencies from PyPI..."
    & $venvPy -m pip install --upgrade pip --quiet
    & $venvPy -m pip install -r (Join-Path $RepoDir "requirements.txt")
    if ($LASTEXITCODE -ne 0) { throw "Dependency install FAILED (online, exit $LASTEXITCODE). See the pip output above. The server cannot start until this succeeds." }
}

# Prove the critical modules are importable. A partial/empty install (e.g. pip
# aborting on one unsatisfiable pin) must NOT be allowed to reach the "Setup
# complete" / shortcut-creation steps and masquerade as a working install.
& $venvPy -c "import waitress, flask, openpyxl" 2>$null
if ($LASTEXITCODE -ne 0) { throw "Dependencies did not install correctly (cannot import waitress/flask/openpyxl). Setup aborted -- fix the pip errors above and re-run." }

# --- 3. Set BAYTRACKER_DATA (machine scope if admin, else user) -------------
$env:BAYTRACKER_DATA = $DataDir
$scope = "User"
try {
    [Environment]::SetEnvironmentVariable("BAYTRACKER_DATA", $DataDir, "Machine")
    $scope = "Machine"
} catch {
    [Environment]::SetEnvironmentVariable("BAYTRACKER_DATA", $DataDir, "User")
}
Write-Host "Set BAYTRACKER_DATA ($scope scope) = $DataDir" -ForegroundColor Green
New-Item -ItemType Directory -Force -Path $DataDir | Out-Null

# --- 4. Initialize the database (non-destructive) ---------------------------
Write-Host "Initializing database (safe if it already exists)..."
& $venvPy (Join-Path $RepoDir "init_db.py")
if ($LASTEXITCODE -ne 0) { throw "Database initialization FAILED (init_db.py exit $LASTEXITCODE). See the message above." }

# --- 5. Firewall (optional) -------------------------------------------------
if ($OpenFirewall) {
    Write-Host "Opening firewall TCP $Port..."
    $rule = "BayTracker ($Port)"
    netsh advfirewall firewall delete rule name="$rule" | Out-Null
    netsh advfirewall firewall add rule name="$rule" dir=in action=allow protocol=TCP localport=$Port | Out-Null
    Write-Host "Firewall rule '$rule' added." -ForegroundColor Green
}

# --- 6. Service via NSSM (optional) -----------------------------------------
if ($InstallService) {
    $nssm = Join-Path $RepoDir "tools\nssm.exe"
    if (-not (Test-Path $nssm)) {
        Write-Warning "tools\nssm.exe not found. Download it from https://nssm.cc and place it there,"
        Write-Warning "then re-run with -InstallService. (See tools\README.txt.)"
        Write-Warning "Alternatively, use Task Scheduler -- see README.md."
    } else {
        Write-Host "Installing Windows service '$ServiceName' (auto-start, auto-restart)..."
        # nssm reports progress AND 'Can't open service!' on stderr. Under the
        # script-wide ErrorActionPreference='Stop', a redirected native-stderr line
        # is wrapped as a terminating NativeCommandError -- which aborted a FIRST
        # install (no service to stop/remove yet). Relax it for the nssm calls and
        # only pre-clean when the service actually exists.
        $eap = $ErrorActionPreference
        $ErrorActionPreference = "Continue"
        try {
            if (Get-Service -Name $ServiceName -ErrorAction SilentlyContinue) {
                & $nssm stop $ServiceName | Out-Null
                & $nssm remove $ServiceName confirm | Out-Null
            }
            # Run waitress (production WSGI server) bound to all interfaces.
            & $nssm install $ServiceName $venvPy "-m waitress --listen=0.0.0.0:$Port --threads=$Threads app:app"
            & $nssm set $ServiceName AppDirectory $RepoDir
            & $nssm set $ServiceName AppEnvironmentExtra "BAYTRACKER_DATA=$DataDir"
            & $nssm set $ServiceName Start SERVICE_AUTO_START
            & $nssm set $ServiceName AppStdout (Join-Path $DataDir "service.log")
            & $nssm set $ServiceName AppStderr (Join-Path $DataDir "service.log")
            & $nssm set $ServiceName AppExit Default Restart       # auto-restart on crash
            & $nssm start $ServiceName
        } finally {
            $ErrorActionPreference = $eap
        }
        Write-Host "Service '$ServiceName' installed and started." -ForegroundColor Green
    }
}

# --- 7. Daily database backup scheduled task (optional) ---------------------
# The database is the only irreplaceable asset; updates/restarts can't corrupt
# it (WAL + non-destructive startup), but a dead disk or deleted folder still
# can. This registers a daily, consistent, off-machine-capable backup so there
# is always a recent copy. Idempotent: re-running replaces the existing task.
if ($ScheduleBackup) {
    $taskName = "BayTracker Backup"
    Write-Host "Registering scheduled daily backup '$taskName' at $BackupTime..."
    try {
        $backupPs1 = Join-Path $RepoDir "backup.ps1"
        $destArg = if ($BackupDest) { " -Dest '$BackupDest'" } else { "" }
        # Set BAYTRACKER_DATA inline so the task finds the right data folder even
        # when it runs as SYSTEM (which doesn't see user-scope env vars).
        $inner = "`$env:BAYTRACKER_DATA='$DataDir'; & '$backupPs1'$destArg"
        $action = New-ScheduledTaskAction -Execute "powershell.exe" `
            -Argument "-ExecutionPolicy Bypass -NonInteractive -WindowStyle Hidden -Command `"$inner`""
        $trigger = New-ScheduledTaskTrigger -Daily -At $BackupTime
        # Run as SYSTEM, whether or not anyone is logged in; catch up if the PC
        # was off at the scheduled time.
        $principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
        $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable
        Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
        Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
            -Principal $principal -Settings $settings | Out-Null
        Write-Host "Scheduled backup registered (daily $BackupTime, as SYSTEM)." -ForegroundColor Green
        if ($BackupDest) {
            Write-Host "  Off-machine copy -> $BackupDest"
        } else {
            Write-Host "  Local backups -> $DataDir\backups"
            Write-Host "  Tip: also set an off-machine target with -BackupDest '\\server\share\baytracker'," -ForegroundColor Yellow
            Write-Host "       or set 'Backup network path' in /admin -- the task copies there automatically." -ForegroundColor Yellow
        }
    } catch {
        Write-Warning "Could not register the scheduled backup (needs an elevated PowerShell): $_"
        Write-Warning "Run setup.ps1 again from an Administrator prompt, or schedule backup.ps1 by hand (see README)."
    }
}

# --- Desktop shortcuts (clickable launchers) --------------------------------
# Drops two labelled icons on the Desktop: "Start Bay Tracker Server" (boot the
# server) and "Update Bay Tracker" (pull the latest release from GitHub).
try {
    & (Join-Path $RepoDir "make_shortcut.ps1") -Port $Port -ServiceName $ServiceName
} catch {
    Write-Warning "Could not create the desktop shortcuts: $_"
}

# --- Done: print the URLs ---------------------------------------------------
$ipv4 = (Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
         Where-Object { $_.IPAddress -notmatch "^(127\.|169\.254\.)" } |
         Select-Object -First 1).IPAddress
if (-not $ipv4) { $ipv4 = "<this-pc-ip>" }

Write-Host ""
Write-Host "=== Setup complete ===" -ForegroundColor Cyan
if (-not $InstallService) {
    Write-Host "To run the server now (foreground):"
    Write-Host "    powershell -ExecutionPolicy Bypass -File .\start.ps1" -ForegroundColor Yellow
}
Write-Host ""
Write-Host "Desktop icons (created automatically):" -ForegroundColor Cyan
Write-Host "    Start Bay Tracker Server   -- double-click to bring the server up"
Write-Host "    Update Bay Tracker         -- double-click to pull the latest release from GitHub"
Write-Host ""
Write-Host "Point devices at:"
Write-Host "    Dashboard (TVs):   http://${ipv4}:$Port/dashboard" -ForegroundColor Green
Write-Host "    Logging console:   http://${ipv4}:$Port/console"   -ForegroundColor Green
Write-Host "    Stats / Admin:     http://${ipv4}:$Port/stats  +  /admin (set PINs in Admin)"
Write-Host ""
Write-Host "NEXT: pin this PC's IP with a DHCP reservation so the address never changes,"
Write-Host "      then open /admin and enter your real divisions, reasons, products, initials,"
Write-Host "      and shift/break/operating times (they start empty on purpose)."
if (-not $ScheduleBackup) {
    Write-Host ""
    Write-Warning "No automatic backup is scheduled. The database survives updates/restarts, but a"
    Write-Warning "dead disk or deleted folder is unrecoverable without backups. Re-run with"
    Write-Warning "-ScheduleBackup (ideally -BackupDest '\\server\share\baytracker') to set up a daily copy."
}
