<#
    make_shortcut.ps1 -- put the Bay Tracker control icons on the Desktop.

    Creates two clearly-labelled shortcuts, each with its own intuitive icon:

      * "Start Bay Tracker Server"  (green play icon)   -> boot.ps1
            Brings the server up (starts the Windows service if installed,
            otherwise launches it in the foreground).

      * "Update Bay Tracker"        (blue download icon) -> update_latest.ps1
            Pulls the latest RELEASE from GitHub and deploys it safely
            (backup -> migrate -> restart -> health-check -> auto-rollback).
            Handy once you start pushing updates from your laptop.

    setup.ps1 runs this for you. Re-run by hand any time (it overwrites in place):
        powershell -ExecutionPolicy Bypass -File .\make_shortcut.ps1

    Options:
        -Dashboard   also drop a "Bay Tracker Dashboard" icon that opens the live
                     board in the default browser (handy on the server PC).

    The launchers are designed for the FOREGROUND install and need no admin
    rights there, so clicking them never triggers a UAC prompt. On a PC that
    runs the auto-start Windows service instead (setup.ps1 -InstallService),
    starting/restarting the service requires an Administrator PowerShell -- the
    scripts detect that and say so instead of failing cryptically.
    The shortcuts point at THIS repo's scripts, so create them from the
    folder you actually run the server from (e.g. C:\BayTracking).
#>
param(
    [switch]$Dashboard,
    [int]$Port = 5000,
    [string]$ServiceName = "BayTracker"
)

$ErrorActionPreference = "Stop"
$RepoDir = Split-Path -Parent $MyInvocation.MyCommand.Path
foreach ($f in @("boot.ps1", "update_latest.ps1")) {
    if (-not (Test-Path (Join-Path $RepoDir $f))) { throw "$f not found next to make_shortcut.ps1 ($RepoDir)." }
}

# --- Make sure the icons exist (regenerate if a clone is missing them) -------
$startIco  = Join-Path $RepoDir "assets\start.ico"
$updateIco = Join-Path $RepoDir "assets\update.ico"
if (-not (Test-Path $startIco) -or -not (Test-Path $updateIco)) {
    try {
        & (Join-Path $RepoDir "assets\make_icons.ps1")
    } catch {
        Write-Warning "Could not generate custom icons ($_). Falling back to a system icon."
    }
}
# Fallback icon if the custom ones still aren't there for any reason.
$fallbackIco = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
function Resolve-Icon([string]$ico) { if (Test-Path $ico) { "$ico,0" } else { "$fallbackIco,0" } }

$desktop  = [Environment]::GetFolderPath("Desktop")
$ws       = New-Object -ComObject WScript.Shell
$powershell = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"

function New-Launcher {
    param([string]$Name, [string]$Script, [string]$Icon, [string]$Description,
          [string]$Folder = $desktop)
    $lnkPath = Join-Path $Folder "$Name.lnk"
    $scriptPath = Join-Path $RepoDir $Script
    $lnk = $ws.CreateShortcut($lnkPath)
    $lnk.TargetPath = $powershell
    # -NoExit keeps the window open so the URLs / result stay readable.
    $lnk.Arguments = "-NoExit -ExecutionPolicy Bypass -File `"$scriptPath`" -Port $Port -ServiceName `"$ServiceName`""
    $lnk.WorkingDirectory = $RepoDir
    $lnk.IconLocation = (Resolve-Icon $Icon)
    $lnk.Description = $Description
    $lnk.WindowStyle = 1
    $lnk.Save()
    Write-Host "Created: $lnkPath" -ForegroundColor Green
}

# --- 1. Start the server -----------------------------------------------------
New-Launcher -Name "Start Bay Tracker Server" -Script "boot.ps1" -Icon $startIco `
    -Description "Start the Bay Tracking server (starts the service, or launches it in this window)."

# --- 2. Update from GitHub ---------------------------------------------------
New-Launcher -Name "Update Bay Tracker" -Script "update_latest.ps1" -Icon $updateIco `
    -Description "Pull the latest release from GitHub and deploy it safely (backup + auto-rollback)."

# --- 3. Auto-start at every login (Startup folder) ---------------------------
# On a FOREGROUND install nothing else brings the server back after a reboot
# (Windows Update reboots monthly) -- without this, "someone remembers to
# double-click the icon" is a load-bearing part of the design. boot.ps1 is safe
# to run when the server is already up (it health-checks first and just reports
# healthy), so this same launcher works on every login in every install mode.
# Needs no admin rights: the per-user Startup folder is always writable.
$startupDir = [Environment]::GetFolderPath("Startup")
New-Launcher -Name "Start Bay Tracker Server" -Script "boot.ps1" -Icon $startIco `
    -Description "Auto-start the Bay Tracking server at login." -Folder $startupDir

# --- Clean up the old single-purpose launcher from earlier versions ----------
# Earlier installs created one launcher named either "Bay Tracker Server" or
# "Bay Tracker Service"; remove it so there's no stale duplicate next to the new
# "Start Bay Tracker Server" icon.
foreach ($oldName in @("Bay Tracker Server.lnk", "Bay Tracker Service.lnk")) {
    $legacy = Join-Path $desktop $oldName
    if (Test-Path $legacy) {
        Remove-Item $legacy -Force -ErrorAction SilentlyContinue
        Write-Host "Removed old '$([System.IO.Path]::GetFileNameWithoutExtension($oldName))' shortcut (replaced by 'Start Bay Tracker Server')." -ForegroundColor DarkGray
    }
}

# --- Optional: open-the-dashboard shortcut -----------------------------------
if ($Dashboard) {
    $urlPath = Join-Path $desktop "Bay Tracker Dashboard.url"
    Set-Content -Path $urlPath -Encoding ASCII -Value @(
        "[InternetShortcut]",
        "URL=http://localhost:$Port/dashboard"
    )
    Write-Host "Created: $urlPath" -ForegroundColor Green
}

Write-Host ""
Write-Host "Desktop icons ready:" -ForegroundColor Cyan
Write-Host "    Start Bay Tracker Server   -- click to bring the server up"
Write-Host "    Update Bay Tracker         -- click to pull the latest release from GitHub"
Write-Host "Also added 'Start Bay Tracker Server' to the Startup folder, so the server"
Write-Host "comes back automatically at every login (delete that .lnk to opt out)."
