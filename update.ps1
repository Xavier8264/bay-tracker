<#
    update.ps1 -- deliberate, reversible update to a specific release tag.

    Updates are ALWAYS a human-triggered action -- the production PC never
    auto-pulls from GitHub (Appendix B7). The floor PC runs a known git tag, not
    bare main.

    Sequence (Appendix B7):
      1. Back up the database (consistent online backup).
      2. Record the current version so we can roll back to it.
      3. Deploy the chosen tag (git fetch + checkout).
      4. Install the tag's pinned dependencies.
      5. Run migrate.py (additive, idempotent).
      6. Restart the service.
      7. Health-check /healthz.
      8. If the health check fails, AUTOMATICALLY roll back to the previous
         version (re-deploy, re-install, re-migrate, restart, re-check).

    The database lives outside the repo (BAYTRACKER_DATA), so none of the git
    steps can ever touch the accumulated log.

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
    $svc = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
    if ($svc) {
        $nssm = Join-Path $RepoDir "tools\nssm.exe"
        if (Test-Path $nssm) { & $nssm restart $ServiceName } else { Restart-Service $ServiceName }
        Write-Host "Restarted service '$ServiceName'."
    } else {
        Write-Warning "Service '$ServiceName' not found. If you run the server manually, restart it now."
    }
}

function Deploy-Ref {
    param([string]$Ref)
    git fetch --tags --quiet
    git checkout --quiet $Ref
    & $venvPy -m pip install -r (Join-Path $RepoDir "requirements.txt") --quiet
    & $venvPy (Join-Path $RepoDir "migrate.py")
    Restart-App
}

Write-Host "=== Bay Tracking update -> $Tag ===" -ForegroundColor Cyan

# 1. Backup the database BEFORE anything else.
Write-Host "[1/8] Backing up the database..."
& $venvPy (Join-Path $RepoDir "backup_db.py")

# 2. Record the current version for rollback.
$previous = (git rev-parse --short HEAD).Trim()
$previousDesc = (git describe --tags --always 2>$null)
Write-Host "[2/8] Current version recorded: $previousDesc ($previous)"

# 3-7. Deploy the new tag, migrate, restart, health-check.
try {
    Write-Host "[3/8] Deploying tag $Tag (git fetch + checkout)..."
    Write-Host "[4/8] Installing pinned dependencies..."
    Write-Host "[5/8] Running migrations..."
    Write-Host "[6/8] Restarting service..."
    Deploy-Ref -Ref $Tag

    Write-Host "[7/8] Health check..."
    if (Test-Health -TimeoutSec $HealthTimeoutSec) {
        Write-Host "=== Update to $Tag succeeded and is healthy. ===" -ForegroundColor Green
        exit 0
    } else {
        throw "Health check did not return 200 within $HealthTimeoutSec seconds."
    }
}
catch {
    Write-Warning "[8/8] Update FAILED: $($_.Exception.Message)"
    Write-Warning "Rolling back to previous version $previousDesc ($previous)..."
    try {
        Deploy-Ref -Ref $previous
        if (Test-Health -TimeoutSec $HealthTimeoutSec) {
            Write-Host "Rollback to $previousDesc succeeded; service is healthy again." -ForegroundColor Yellow
            Write-Host "The database backup from step 1 is in BAYTRACKER_DATA\backups if you need it."
            exit 1
        } else {
            Write-Error "ROLLBACK ALSO FAILED A HEALTH CHECK. Investigate immediately. " +
                        "The pre-update database backup is safe in BAYTRACKER_DATA\backups."
            exit 2
        }
    }
    catch {
        Write-Error "ROLLBACK ERRORED: $($_.Exception.Message). The database backup from step 1 is safe."
        exit 2
    }
}
