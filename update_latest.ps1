<#
    update_latest.ps1 -- the "Update Bay Tracker" desktop button.

    For when you push a new release from your laptop and want the floor PC to
    pick it up without typing a version number. One click:

      1. Fetches the latest tags from GitHub (origin).
      2. Finds the newest release tag (e.g. v1.2.0).
      3. If it's newer than what's running, shows current -> new and asks you to
         confirm, then hands off to update.ps1, which does the SAFE deploy:
         back up the DB -> check out the tag -> install pinned deps -> migrate ->
         restart -> health-check -> AUTOMATICALLY roll back if the check fails.

    This deliberately tracks tagged RELEASES, not bare `main` -- the floor PC
    always runs a known version (spec Appendix B7). To ship an update, on your
    laptop:  git push origin main   then   git tag vX.Y.Z; git push origin vX.Y.Z

    Nothing is changed without your confirmation. Run by hand:
        powershell -ExecutionPolicy Bypass -File .\update_latest.ps1
        powershell -ExecutionPolicy Bypass -File .\update_latest.ps1 -Yes   # no prompt
#>
param(
    [int]$Port = 5000,
    [string]$ServiceName = "BayTracker",
    [switch]$Yes              # skip the confirmation prompt
)

$ErrorActionPreference = "Stop"
$RepoDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $RepoDir

Write-Host "=== Update Bay Tracker (from GitHub) ===" -ForegroundColor Cyan

if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    Write-Warning "git is not installed / not on PATH, so this PC can't pull updates."
    Write-Warning "Install Git for Windows (https://git-scm.com), then try again."
    return
}

Write-Host "Fetching the latest releases from GitHub..."
# git prints its fetch summary ("From https://... * [new tag] ...") to STDERR.
# Under $ErrorActionPreference = 'Stop', PowerShell 5.1 turns REDIRECTED native
# stderr into a terminating error, so a `2>&1` here made this step "fail" with a
# bogus "could not reach GitHub" precisely when a new release was downloaded.
# Leave stderr alone (it still prints to the console) and trust the exit code.
git fetch --tags --prune origin
if ($LASTEXITCODE -ne 0) {
    Write-Warning "Could not fetch from GitHub (git exit code $LASTEXITCODE) -- see the message above."
    Write-Warning "Check this PC's internet connection and try again."
    return
}

# Newest release tag, sorted by version (v1.10.0 > v1.9.0, not string order).
$latest = (git tag --list "v*" --sort=-v:refname | Select-Object -First 1)
if (-not $latest) {
    Write-Warning "No release tags (vX.Y.Z) were found on GitHub yet -- nothing to update to."
    Write-Host   "Tag a release on your laptop first:  git tag v1.2.0 ; git push origin v1.2.0"
    return
}
$latest = $latest.Trim()

# The 2>$null redirects run with EAP=Continue scoped to the block: redirected
# native stderr + EAP=Stop throws in PS 5.1 (same footgun as the fetch above).
$currentDesc = & { $ErrorActionPreference = 'Continue'; git describe --tags --always 2>$null }
$latestSha   = & { $ErrorActionPreference = 'Continue'; git rev-parse "$latest^{commit}" 2>$null }
$headSha     = & { $ErrorActionPreference = 'Continue'; git rev-parse "HEAD^{commit}" 2>$null }

Write-Host ""
Write-Host "Currently running: $currentDesc"
Write-Host "Latest release:    $latest"

if ($latestSha -and $headSha -and ($latestSha -eq $headSha)) {
    Write-Host ""
    Write-Host "You're already running the latest release ($latest). Nothing to do." -ForegroundColor Green
    return
}

# If HEAD is AHEAD of the newest release tag (someone ran/committed newer code
# on this PC, or a release wasn't tagged), "updating" would silently DOWNGRADE
# and re-break whatever the newer commits fixed. Never do that on a one-click
# path -- even with -Yes.
& { $ErrorActionPreference = 'Continue'; git merge-base --is-ancestor "$latest^{commit}" HEAD 2>$null }
if ($LASTEXITCODE -eq 0) {
    Write-Warning "This PC is running code NEWER than the latest release tag ($latest) -- an 'update' would be a DOWNGRADE."
    Write-Warning "Nothing was changed. Tag and push a new release from the dev machine, then click Update again."
    Write-Warning "(To intentionally roll back, run:  powershell -ExecutionPolicy Bypass -File .\update.ps1 -Tag $latest )"
    return
}

if (-not $Yes) {
    Write-Host ""
    $ans = Read-Host "Update to $latest now? The DB is backed up first and rolled back automatically if the new version is unhealthy. [y/N]"
    if ($ans -notmatch '^(y|yes)$') {
        Write-Host "Cancelled -- no changes made." -ForegroundColor Yellow
        return
    }
}

# Hand off to the proven safe-update path. Run it in a CHILD PowerShell so its
# internal `exit` codes can't close this window before we print the summary.
Write-Host ""
$updatePs1 = Join-Path $RepoDir "update.ps1"
$ps = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
& $ps -NoProfile -ExecutionPolicy Bypass -File $updatePs1 -Tag $latest -Port $Port -ServiceName $ServiceName
$code = $LASTEXITCODE

Write-Host ""
switch ($code) {
    0       { Write-Host "=== Update to $latest complete and healthy. ===" -ForegroundColor Green }
    1       { Write-Warning "Update failed its health check and was rolled back to the previous version, which is healthy again. The pre-update DB backup is in BAYTRACKER_DATA\backups." }
    3       { Write-Host "=== Update to $latest deployed. ===" -ForegroundColor Green
              Write-Host "This PC runs the server manually (no Windows service), so finish the update by restarting it:" -ForegroundColor Yellow
              Write-Host "  1. Stop the server's console window (Ctrl+C, or close it)." -ForegroundColor Yellow
              Write-Host "  2. Double-click the 'Start Bay Tracker Server' desktop icon." -ForegroundColor Yellow }
    4       { Write-Warning "The new version $latest failed to deploy, so the code was rolled back to the previous version. Restart the server to resume it. The pre-update DB backup is safe in BAYTRACKER_DATA\backups." }
    5       { Write-Warning "The update stopped BEFORE changing anything (see the message above -- e.g. the backup failed, or the service needs an Administrator PowerShell). Nothing to undo." }
    default { Write-Warning "Update ran into trouble (exit $code). Read the messages above; the pre-update DB backup is safe in BAYTRACKER_DATA\backups." }
}
