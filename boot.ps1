<#
    boot.ps1 -- the "Start Bay Tracker Server" desktop button.

    One click brings the server up the right way for THIS machine, whichever way
    it was installed:

      * Installed as a Windows service (setup.ps1 -InstallService)?
        Starts the service if it isn't already running, then health-checks it.
        (The service also auto-starts on boot and auto-restarts on crash; this
        button is for the times you stopped it, or want to confirm it's up.)

      * Running in the foreground instead (no service)?
        Hands off to start.ps1, which launches the server in this window
        (leave it open; Ctrl+C stops it).

    Safe to click any time -- if the server is already up it just says so. No
    admin prompt (starting an already-installed service does not need elevation).

    Run by hand:  powershell -ExecutionPolicy Bypass -File .\boot.ps1
#>
param(
    [int]$Port = 5000,
    [string]$ServiceName = "BayTracker"
)

$ErrorActionPreference = "Stop"
$RepoDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $RepoDir

function Get-LanUrl {
    $ipv4 = (Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
             Where-Object { $_.IPAddress -notmatch "^(127\.|169\.254\.)" } |
             Select-Object -First 1).IPAddress
    if (-not $ipv4) { $ipv4 = "<this-pc-ip>" }
    return "http://${ipv4}:$Port"
}

function Test-Health {
    param([int]$TimeoutSec = 20)
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while ((Get-Date) -lt $deadline) {
        try {
            $r = Invoke-WebRequest -UseBasicParsing -TimeoutSec 5 "http://localhost:$Port/healthz"
            if ($r.StatusCode -eq 200) { return $true }
        } catch { Start-Sleep -Seconds 2 }
    }
    return $false
}

Write-Host "=== Start Bay Tracking server ===" -ForegroundColor Cyan

$svc = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if ($svc) {
    # --- Service-managed deployment ----------------------------------------
    $url = Get-LanUrl
    if ($svc.Status -eq "Running") {
        Write-Host "Service '$ServiceName' is already running." -ForegroundColor Green
    } else {
        Write-Host "Starting service '$ServiceName' (current state: $($svc.Status))..."
        $nssm = Join-Path $RepoDir "tools\nssm.exe"
        if (Test-Path $nssm) { & $nssm start $ServiceName | Out-Null } else { Start-Service -Name $ServiceName }
        Write-Host "Start requested." -ForegroundColor Green
    }

    Write-Host "Health check..."
    if (Test-Health -TimeoutSec 25) {
        Write-Host "Server is UP and healthy." -ForegroundColor Green
    } else {
        Write-Warning "Service did not pass the health check yet. It may still be starting --"
        Write-Warning "check the service log at BAYTRACKER_DATA\service.log if it doesn't come up."
    }
    Write-Host ""
    Write-Host "Point devices at:"
    Write-Host "    Dashboard (TVs):  $url/dashboard" -ForegroundColor Green
    Write-Host "    Logging console:  $url/console"   -ForegroundColor Green
    Write-Host "    Stats / Admin:    $url/stats  +  /admin"
    Write-Host ""
    Write-Host "(This server runs as a Windows service, so it comes back on its own after a reboot.)"
}
else {
    # --- Foreground deployment: start.ps1 runs the server in THIS window ----
    $startPs1 = Join-Path $RepoDir "start.ps1"
    if (-not (Test-Path $startPs1)) { throw "start.ps1 not found in $RepoDir -- run setup.ps1 first." }
    Write-Host "No '$ServiceName' service is installed -- launching in the foreground."
    Write-Host "Leave this window open; Ctrl+C stops the server." -ForegroundColor Yellow
    Write-Host ""
    & $startPs1 -Port $Port
}
