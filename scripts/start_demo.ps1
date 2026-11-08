# Start the Beat the Shutdown console for a demo.
#
#   powershell -ExecutionPolicy Bypass -File scripts\start_demo.ps1            # build if needed, then serve
#   powershell -ExecutionPolicy Bypass -File scripts\start_demo.ps1 -Rebuild   # always rebuild the console
#
# Serves the API and the built console on 0.0.0.0:8000, so phones and the ESP32 twin on the
# same network can reach it. Prints the URLs and this laptop's IPv4 addresses. Ctrl+C stops it.
param(
    [int]$Port = 8000,
    [switch]$Rebuild
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

# Node may have been installed after this terminal opened: refresh PATH from the registry.
$env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [Environment]::GetEnvironmentVariable("Path", "User")

# 1. Build the console if needed (or asked).
$dist = Join-Path $root "frontend\dist\index.html"
if ($Rebuild -or -not (Test-Path $dist)) {
    if (-not (Get-Command npm -ErrorAction SilentlyContinue)) {
        Write-Host "npm not found: install Node.js LTS, then rerun." -ForegroundColor Red
        exit 1
    }
    Write-Host "Building the console..." -ForegroundColor Cyan
    Push-Location (Join-Path $root "frontend")
    if (-not (Test-Path "node_modules")) { npm install --no-audit --no-fund }
    npm run build
    Pop-Location
}

# 2. Refuse to start twice on the same port.
$busy = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($busy) {
    $procId = ($busy | Select-Object -First 1).OwningProcess
    Write-Host "Port $Port is already in use (process $procId). Stop it first or pass -Port 8001." -ForegroundColor Red
    exit 1
}

# 3. Addresses to share.
$ips = @(Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
    Where-Object { $_.IPAddress -notlike "127.*" -and $_.IPAddress -notlike "169.254.*" -and $_.PrefixOrigin -ne "WellKnown" } |
    Sort-Object -Property @{ Expression = { $_.InterfaceAlias -match "Wi-?Fi|Wireless|WLAN" }; Descending = $true } |
    Select-Object -ExpandProperty IPAddress -Unique)  # @() keeps a list even with one address

Write-Host ""
Write-Host "Beat the Shutdown console" -ForegroundColor Green
Write-Host "  On this laptop:   http://localhost:$Port"
foreach ($ip in $ips) {
    Write-Host "  On the network:   http://${ip}:$Port      (phones, judges)"
}
if ($ips) {
    Write-Host ""
    Write-Host "  Laptop IPv4 for the ESP32 (firmware/twin_wifi/secrets.h SERVER_HOST): $($ips[0])" -ForegroundColor Yellow
}
Write-Host "  Simulated twin (no hardware): python scripts\fake_twin.py --reset"
Write-Host "  If phones can't connect, allow Python through Windows Firewall on private networks."
Write-Host ""

# 4. Serve API + console. Foreground: Ctrl+C to stop.
python -m uvicorn backend.api:app --host 0.0.0.0 --port $Port
