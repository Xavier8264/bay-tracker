<#
    make_shortcut.ps1 -- put a clickable "Bay Tracker Server" icon on the
    Desktop that launches the server (start.ps1) when double-clicked.

    Run it once from the installed repo folder:
        powershell -ExecutionPolicy Bypass -File .\make_shortcut.ps1

    Options:
        -Dashboard   also drop a "Bay Tracker Dashboard" icon that opens the
                     live board in the default browser (handy on the server PC).

    Safe to re-run; it overwrites the shortcut in place. The launcher does NOT
    need admin rights (only the one-time firewall rule did), so clicking it
    never triggers a UAC prompt. The shortcut points at THIS repo's start.ps1,
    so create it from the folder you actually run the server from
    (e.g. C:\BayTracking).
#>
param([switch]$Dashboard, [int]$Port = 5000)

$ErrorActionPreference = "Stop"
$RepoDir  = Split-Path -Parent $MyInvocation.MyCommand.Path
$startPs1 = Join-Path $RepoDir "start.ps1"
if (-not (Test-Path $startPs1)) { throw "start.ps1 not found next to make_shortcut.ps1 ($RepoDir)." }

$desktop = [Environment]::GetFolderPath("Desktop")
$ws = New-Object -ComObject WScript.Shell

# --- the launcher shortcut -------------------------------------------------
$lnkPath = Join-Path $desktop "Bay Tracker Server.lnk"
$lnk = $ws.CreateShortcut($lnkPath)
$lnk.TargetPath = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
# -NoExit keeps the window open so the URLs (and any "port in use" message) stay
# readable; the server itself runs in the foreground in that window.
$lnk.Arguments = "-NoExit -ExecutionPolicy Bypass -File `"$startPs1`""
$lnk.WorkingDirectory = $RepoDir
$venvPy = Join-Path $RepoDir "venv\Scripts\python.exe"
if (Test-Path $venvPy) { $lnk.IconLocation = "$venvPy,0" }
$lnk.Description = "Start the Bay Tracking server. Leave the window open; Ctrl+C stops it."
$lnk.WindowStyle = 1
$lnk.Save()
Write-Host "Created: $lnkPath" -ForegroundColor Green

# --- optional: open-the-dashboard shortcut ---------------------------------
if ($Dashboard) {
    $urlPath = Join-Path $desktop "Bay Tracker Dashboard.url"
    Set-Content -Path $urlPath -Encoding ASCII -Value @(
        "[InternetShortcut]",
        "URL=http://localhost:$Port/dashboard"
    )
    Write-Host "Created: $urlPath" -ForegroundColor Green
}

Write-Host ""
Write-Host "Double-click 'Bay Tracker Server' on the Desktop to start the server." -ForegroundColor Cyan
