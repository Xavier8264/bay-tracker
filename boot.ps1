<#
    boot.ps1 -- the "Start Bay Tracker Server" desktop button.

    One click brings the server up the right way for THIS machine, whichever way
    it was installed:

      * Installed as an AUTO-START Windows service (setup.ps1 -InstallService)?
        Starts the service if it isn't already running, then health-checks it.
        (The service also auto-starts on boot and auto-restarts on crash; this
        button is for the times you stopped it, or want to confirm it's up.)
        NOTE: starting/stopping a Windows service needs an Administrator
        PowerShell -- if this window isn't elevated you'll get a clear message.

      * Running in the foreground instead (no service, or a leftover service
        switched to Manual/Disabled start)?
        Hands off to start.ps1, which launches the server in this window
        (leave it open; Ctrl+C stops it). No admin rights needed.

    Safe to click any time -- if the server is already up it just says so.

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
    # Prefer the adapter that owns the default route -- the address other machines
    # actually reach this PC on. Hyper-V/WSL/VMware virtual NICs (often 172.x) have
    # no gateway, so this skips them and picks the real Wi-Fi/Ethernet IP. Falls
    # back to the first real IPv4 if there's no default route (e.g. isolated LAN).
    $ipv4 = $null
    try {
        $ifIndex = Get-NetRoute -DestinationPrefix '0.0.0.0/0' -ErrorAction Stop |
                   Sort-Object RouteMetric, InterfaceMetric |
                   Select-Object -First 1 -ExpandProperty ifIndex
        if ($ifIndex) {
            $ipv4 = (Get-NetIPAddress -AddressFamily IPv4 -InterfaceIndex $ifIndex -ErrorAction Stop |
                     Where-Object { $_.IPAddress -notmatch "^(127\.|169\.254\.)" } |
                     Select-Object -First 1).IPAddress
        }
    } catch { }
    if (-not $ipv4) {
        $ipv4 = (Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
                 Where-Object { $_.IPAddress -notmatch "^(127\.|169\.254\.)" } |
                 Select-Object -First 1).IPAddress
    }
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

# Only a service set to start AUTOMATICALLY counts as the active deployment
# (that's how setup.ps1 -InstallService leaves it). A leftover service switched
# to Manual/Disabled is decommissioned -- starting it would need admin rights
# and it isn't what serves the app, so fall through to the foreground path.
$svc = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if ($svc -and $svc.StartType -ne 'Automatic') {
    Write-Host "(Service '$ServiceName' exists but is set to '$($svc.StartType)' start -- treating this PC as a FOREGROUND install.)" -ForegroundColor DarkGray
    $svc = $null
}
if ($svc) {
    # --- Service-managed deployment ----------------------------------------
    $url = Get-LanUrl
    if ($svc.Status -eq "Running") {
        Write-Host "Service '$ServiceName' is already running." -ForegroundColor Green
    } else {
        Write-Host "Starting service '$ServiceName' (current state: $($svc.Status))..."
        try {
            Start-Service -Name $ServiceName
            Write-Host "Start requested." -ForegroundColor Green
        } catch {
            Write-Warning "Could not start the service: $($_.Exception.Message)"
            Write-Warning "Starting a Windows service needs an Administrator PowerShell. Right-click PowerShell,"
            Write-Warning "'Run as administrator', then:  Start-Service $ServiceName"
            exit 1
        }
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

    # "Safe to click any time" must actually be true. If a healthy server is
    # already up, say so and stop (no scary port-in-use error). If something
    # holds the port but does NOT answer /healthz (a wedged server -- exactly
    # when the operator reaches for this button), replace it via -Force instead
    # of telling the operator to type a PowerShell command by hand.
    if (Test-Health -TimeoutSec 5) {
        $url = Get-LanUrl
        Write-Host "The server is already UP and healthy." -ForegroundColor Green
        Write-Host "    Dashboard (TVs):  $url/dashboard" -ForegroundColor Green
        Write-Host "    Logging console:  $url/console"   -ForegroundColor Green
        exit 0
    }
    $listener = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
    Write-Host "This PC is not service-managed -- launching in the foreground."
    Write-Host "Leave this window open; Ctrl+C stops the server." -ForegroundColor Yellow
    Write-Host ""
    if ($listener) {
        Write-Warning "Something holds port $Port but is not answering health checks -- replacing it..."
        & $startPs1 -Port $Port -Force
    } else {
        & $startPs1 -Port $Port
    }
}
