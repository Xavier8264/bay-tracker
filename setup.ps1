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

    IMPORTANT: the data folder must NOT be inside a OneDrive/Dropbox-synced
    location -- cloud sync corrupts live SQLite files. The default C:\BayTrackerData
    is a safe local path.

    Examples:
      # Basic install (run from the repo folder):
      powershell -ExecutionPolicy Bypass -File .\setup.ps1

      # Full production install (run an elevated PowerShell):
      powershell -ExecutionPolicy Bypass -File .\setup.ps1 -OpenFirewall -InstallService
#>
param(
    [string]$DataDir = "C:\BayTrackerData",   # where the database + backups live (OFF OneDrive!)
    [int]$Port = 5000,                          # the single port to serve on
    [int]$Threads = 32,                         # waitress threads (>= number of TVs + console + headroom)
    [string]$ServiceName = "BayTracker",
    [switch]$OpenFirewall,                       # add the inbound firewall rule (needs admin)
    [switch]$InstallService                      # install the NSSM auto-start service (needs admin)
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
Write-Host "Installing pinned dependencies..."
& $venvPy -m pip install --upgrade pip --quiet
& $venvPy -m pip install -r (Join-Path $RepoDir "requirements.txt")

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
        & $nssm stop $ServiceName 2>$null
        & $nssm remove $ServiceName confirm 2>$null
        # Run waitress (production WSGI server) bound to all interfaces.
        & $nssm install $ServiceName $venvPy "-m waitress --listen=0.0.0.0:$Port --threads=$Threads app:app"
        & $nssm set $ServiceName AppDirectory $RepoDir
        & $nssm set $ServiceName AppEnvironmentExtra "BAYTRACKER_DATA=$DataDir"
        & $nssm set $ServiceName Start SERVICE_AUTO_START
        & $nssm set $ServiceName AppStdout (Join-Path $DataDir "service.log")
        & $nssm set $ServiceName AppStderr (Join-Path $DataDir "service.log")
        & $nssm set $ServiceName AppExit Default Restart       # auto-restart on crash
        & $nssm start $ServiceName
        Write-Host "Service '$ServiceName' installed and started." -ForegroundColor Green
    }
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
    Write-Host "    venv\Scripts\python.exe -m waitress --listen=0.0.0.0:$Port --threads=$Threads app:app" -ForegroundColor Yellow
}
Write-Host ""
Write-Host "Point devices at:"
Write-Host "    Dashboard (TVs):   http://${ipv4}:$Port/dashboard" -ForegroundColor Green
Write-Host "    Logging console:   http://${ipv4}:$Port/console"   -ForegroundColor Green
Write-Host "    Stats / Admin:     http://${ipv4}:$Port/stats  +  /admin (set PINs in Admin)"
Write-Host ""
Write-Host "NEXT: pin this PC's IP with a DHCP reservation so the address never changes,"
Write-Host "      then open /admin and enter your real divisions, reasons, products, initials,"
Write-Host "      and shift/break/operating times (they start empty on purpose)."
