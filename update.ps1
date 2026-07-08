<#
    update.ps1 -- deliberate, reversible update to a specific release tag.

    Updates are ALWAYS a human-triggered action -- the production PC never
    auto-pulls from GitHub (Appendix B7). The floor PC runs a known git tag, not
    bare main.

    The script adapts to HOW this PC runs the server:

      * Service install (setup.ps1 -InstallService): the full safe sequence --
        back up DB -> checkout tag -> install pinned deps -> migrate -> restart
        the service -> health-check /healthz -> AUTOMATICALLY roll back to the
        previous version if the health check fails.

      * Foreground install (no service -- the server runs in a console window):
        the script cannot restart a window it doesn't own, so there is nothing to
        health-check. It does the safe, reversible part -- back up DB -> checkout
        tag -> install deps -> migrate -- then hands the restart back to the
        operator ("stop the window, click Start Bay Tracker Server"). If the
        deploy/migrate itself fails, the checkout is rolled back to the previous
        version so the old code is intact and still runnable.

    The database lives outside the repo (BAYTRACKER_DATA), so none of the git
    steps can ever touch the accumulated log.

    Exit codes: 0 service update healthy; 1 service rolled back (healthy again);
    2 serious (rollback errored / still unhealthy); 3 foreground deploy OK, manual
    restart needed; 4 foreground deploy failed, code rolled back, restart to
    resume the previous version; 5 preflight failed (backup failed, or a service
    update was attempted without admin rights) -- NOTHING was changed.

    Example:
      powershell -ExecutionPolicy Bypass -File .\update.ps1 -Tag v1.1.0
#>
param(
    [Parameter(Mandatory = $true)][string]$Tag,
    [int]$Port = 5000,
    [string]$ServiceName = "BayTracker",
    [int]$HealthTimeoutSec = 40
)

$ErrorActionPreference = "Stop"
$RepoDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $RepoDir
$venvPy = Join-Path $RepoDir "venv\Scripts\python.exe"

function Test-Health {
    # Returns $true once /healthz responds 200 within the timeout.
    param([int]$TimeoutSec)
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while ((Get-Date) -lt $deadline) {
        try {
            $r = Invoke-WebRequest -UseBasicParsing -TimeoutSec 5 "http://localhost:$Port/healthz"
            if ($r.StatusCode -eq 200) { return $true }
        } catch { Start-Sleep -Seconds 2 }
    }
    return $false
}

function Restart-App {
    # Restart-Service throws a readable error under $ErrorActionPreference=Stop.
    # (nssm.exe was used here before, but its exit code was never checked and it
    # prints UTF-16 to the console -- an "Access is denied" became an unreadable
    # garble followed by a 40-second health-check mystery. nssm still HOSTS the
    # service; the SCM restart below works the same.)
    Restart-Service -Name $ServiceName -Force
    Write-Host "Restarted service '$ServiceName'."
}

function Deploy-Ref {
    # Checks out $Ref and brings code + schema to it. Does NOT restart the app --
    # the caller decides how (service restart vs. manual). Throws on any failure so
    # the caller can roll back. Native steps don't throw on a non-zero exit on their
    # own, so each is checked explicitly.
    param([string]$Ref)
    git fetch --tags --quiet
    if ($LASTEXITCODE -ne 0) { throw "git fetch failed (exit $LASTEXITCODE)." }
    git checkout --quiet $Ref
    if ($LASTEXITCODE -ne 0) { throw "git checkout $Ref failed (exit $LASTEXITCODE)." }
    & $venvPy -m pip install -r (Join-Path $RepoDir "requirements.txt") --quiet
    if ($LASTEXITCODE -ne 0) { throw "pip install failed (exit $LASTEXITCODE)." }
    & $venvPy (Join-Path $RepoDir "migrate.py")
    if ($LASTEXITCODE -ne 0) { throw "migrate.py failed (exit $LASTEXITCODE)." }
}

Write-Host "=== Bay Tracking update -> $Tag ===" -ForegroundColor Cyan

# Which deployment model is this? Only a service set to start AUTOMATICALLY is
# treated as the active deployment (that's how setup.ps1 -InstallService leaves
# it). A leftover service switched to Manual/Disabled is a decommissioned
# install -- it isn't what serves the app, and restarting it would need admin
# rights the operator may not have, so it must not hijack the update.
$svc = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
$serviceManaged = [bool]($svc -and $svc.StartType -eq 'Automatic')
if ($svc -and -not $serviceManaged) {
    Write-Host "(Service '$ServiceName' exists but is set to '$($svc.StartType)' start -- treating this PC as a FOREGROUND install.)" -ForegroundColor DarkGray
}

# Restarting a service needs an elevated PowerShell. Find out NOW, before the
# backup/checkout, instead of deploying and then failing at the restart step.
if ($serviceManaged) {
    $principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        Write-Warning "This PC runs Bay Tracker as the Windows service '$ServiceName', and restarting a service needs an Administrator PowerShell."
        Write-Warning "NOTHING has been changed. Re-run this update from an elevated PowerShell:"
        Write-Warning "  powershell -ExecutionPolicy Bypass -File `"$RepoDir\update.ps1`" -Tag $Tag"
        exit 5
    }
}

# 1. Backup the database BEFORE anything else.
Write-Host "[1] Backing up the database..."
& $venvPy (Join-Path $RepoDir "backup_db.py")
if ($LASTEXITCODE -ne 0) {
    Write-Warning "The database backup FAILED (exit $LASTEXITCODE) -- the update stops here, BEFORE any change."
    exit 5
}

# 2. Record the current version for rollback.
$previous = (git rev-parse --short HEAD).Trim()
# Scoped EAP=Continue: redirected native stderr + EAP=Stop throws in PS 5.1.
$previousDesc = & { $ErrorActionPreference = 'Continue'; git describe --tags --always 2>$null }
Write-Host "[2] Current version recorded: $previousDesc ($previous)"

if ($serviceManaged) {
    # ===== Service-managed: deploy, restart, health-check, auto-rollback =====
    try {
        Write-Host "[3] Deploying tag $Tag (git fetch + checkout)..."
        Write-Host "[4] Installing pinned dependencies..."
        Write-Host "[5] Running migrations..."
        Deploy-Ref -Ref $Tag
        Write-Host "[6] Restarting service..."
        Restart-App
        Write-Host "[7] Health check..."
        if (Test-Health -TimeoutSec $HealthTimeoutSec) {
            Write-Host "=== Update to $Tag succeeded and is healthy. ===" -ForegroundColor Green
            exit 0
        } else {
            throw "Health check did not return 200 within $HealthTimeoutSec seconds."
        }
    }
    catch {
        Write-Warning "Update FAILED: $($_.Exception.Message)"
        Write-Warning "Rolling back to previous version $previousDesc ($previous)..."
        try {
            Deploy-Ref -Ref $previous
            Restart-App
            if (Test-Health -TimeoutSec $HealthTimeoutSec) {
                Write-Host "Rollback to $previousDesc succeeded; service is healthy again." -ForegroundColor Yellow
                Write-Host "The database backup from step 1 is in BAYTRACKER_DATA\backups if you need it."
                exit 1
            } else {
                Write-Error ("ROLLBACK ALSO FAILED A HEALTH CHECK. Investigate immediately. " +
                             "The pre-update database backup is safe in BAYTRACKER_DATA\backups.")
                exit 2
            }
        }
        catch {
            Write-Error "ROLLBACK ERRORED: $($_.Exception.Message). The database backup from step 1 is safe."
            exit 2
        }
    }
}
else {
    # ===== Foreground (no service): deploy only; the operator restarts =====
    # There is no service to restart, and we can't restart a console-window server
    # from here, so there's nothing to health-check. Do the safe, reversible deploy
    # and hand the restart back. If the deploy/migrate fails, roll the checkout back
    # so the previous version stays intact and runnable.
    Write-Host "This PC is not service-managed -- treating it as a FOREGROUND (manual) install." -ForegroundColor Yellow
    Write-Host "Deploying the new code; you'll restart the server window yourself at the end."
    try {
        Write-Host "[3] Deploying tag $Tag (git fetch + checkout)..."
        Write-Host "[4] Installing pinned dependencies..."
        Write-Host "[5] Running migrations..."
        Deploy-Ref -Ref $Tag
    }
    catch {
        Write-Warning "Update FAILED before any restart: $($_.Exception.Message)"
        Write-Warning "Rolling the code back to $previousDesc ($previous) (a running server was left untouched)..."
        try {
            Deploy-Ref -Ref $previous
        } catch {
            Write-Error "ROLLBACK ERRORED: $($_.Exception.Message). The database backup from step 1 is safe in BAYTRACKER_DATA\backups."
            exit 2
        }
        Write-Warning "Code restored to $previousDesc. If you already stopped the server, restart it to resume the previous version."
        exit 4
    }

    Write-Host ""
    Write-Host "=== Deployed $Tag. ===" -ForegroundColor Green
    Write-Host "This install has no Windows service, so the new code is ON DISK but NOT yet running." -ForegroundColor Yellow
    Write-Host "RESTART NOW to load it:" -ForegroundColor Yellow
    Write-Host "  1. In the server's console window, press Ctrl+C (or close it) to stop the old version."
    Write-Host "  2. Double-click the 'Start Bay Tracker Server' desktop icon"
    Write-Host "     (or run:  powershell -ExecutionPolicy Bypass -File `"$RepoDir\start.ps1`" -Force )"
    Write-Host "The database backup from step 1 is in BAYTRACKER_DATA\backups if you ever need it."
    exit 3
}
