<#
    backup.ps1 -- scheduled database backup (point Windows Task Scheduler here).

    Writes a consistent, timestamped copy of the live database into
    BAYTRACKER_DATA\backups, and -- if a backup network path is set in /admin or
    passed via -Dest -- also copies it off the machine so a dead PC doesn't erase
    the history (Appendix A6 / B8). Old local backups beyond the retention count
    are pruned by backup_db.py so the disk can never silently fill with copies.

    Every run appends one dated OK/FAIL line to BAYTRACKER_DATA\backup.log --
    the scheduled task runs hidden (often as SYSTEM, invisible to non-admins),
    so that log is the ONLY place a human can answer "are backups happening?".

    Schedule it (run as the same account that has BAYTRACKER_DATA set), e.g. daily:
      schtasks /create /tn "BayTracker Backup" /tr "powershell -ExecutionPolicy Bypass -File C:\path\to\backup.ps1" /sc DAILY /st 02:00
#>
param([string]$Dest = "")

$ErrorActionPreference = "Stop"
$RepoDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$venvPy = Join-Path $RepoDir "venv\Scripts\python.exe"

# Resolve the data folder the same way the app does, so the log lands next to
# the database whichever account runs the task.
$dataDir = $env:BAYTRACKER_DATA
if (-not $dataDir) {
    foreach ($scope in @("User", "Machine")) {
        $v = [Environment]::GetEnvironmentVariable("BAYTRACKER_DATA", $scope)
        if ($v) { $dataDir = $v.Trim('"').Trim(); break }
    }
}
if (-not $dataDir) { $dataDir = "C:\BayTrackerData" }

$code = -1
try {
    if ($Dest) {
        & $venvPy (Join-Path $RepoDir "backup_db.py") --dest $Dest
    } else {
        & $venvPy (Join-Path $RepoDir "backup_db.py")
    }
    $code = $LASTEXITCODE
} catch {
    Write-Warning "backup_db.py could not run: $($_.Exception.Message)"
}

$stamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
$status = switch ($code) {
    0       { "OK" }
    2       { "OK-LOCAL (off-machine copy FAILED -- check the network path in /admin)" }
    default { "FAILED (exit $code)" }
}
try { Add-Content -Path (Join-Path $dataDir "backup.log") -Value "$stamp  $status" } catch { }
exit $code
