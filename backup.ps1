<#
    backup.ps1 -- scheduled database backup (point Windows Task Scheduler here).

    Writes a consistent, timestamped copy of the live database into
    BAYTRACKER_DATA\backups, and -- if a backup network path is set in /admin or
    passed via -Dest -- also copies it off the machine so a dead PC doesn't erase
    the history (Appendix A6 / B8).

    Schedule it (run as the same account that has BAYTRACKER_DATA set), e.g. daily:
      schtasks /create /tn "BayTracker Backup" /tr "powershell -ExecutionPolicy Bypass -File C:\path\to\backup.ps1" /sc DAILY /st 02:00
#>
param([string]$Dest = "")

$ErrorActionPreference = "Stop"
$RepoDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$venvPy = Join-Path $RepoDir "venv\Scripts\python.exe"

if ($Dest) {
    & $venvPy (Join-Path $RepoDir "backup_db.py") --dest $Dest
} else {
    & $venvPy (Join-Path $RepoDir "backup_db.py")
}
