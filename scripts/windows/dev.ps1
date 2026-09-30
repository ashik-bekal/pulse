<#
.SYNOPSIS
  Start the PULSE dev server against DEMO data, on a different port from the
  always-on server, so development can never touch the real ledger.

.EXAMPLE
  .\scripts\windows\dev.ps1            # http://127.0.0.1:5002, demo-data\ledger.db
  .\scripts\windows\dev.ps1 -Reseed    # start again from a fresh demo ledger
#>
[CmdletBinding()]
param(
    [int]$Port = 5002,
    [switch]$Reseed,
    # Werkzeug debugger (executes code from the browser - local dev only).
    [switch]$WerkzeugDebug
)
$ErrorActionPreference = "Stop"
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$DemoDir  = Join-Path $RepoRoot "demo-data"
$Py       = Join-Path $RepoRoot ".venv\Scripts\python.exe"

if (-not (Test-Path $Py)) {
    Write-Host "Creating .venv"
    if (Get-Command py -ErrorAction SilentlyContinue) { & py -3 -m venv (Join-Path $RepoRoot ".venv") }
    else { & python -m venv (Join-Path $RepoRoot ".venv") }
    & $Py -m pip install --quiet -r (Join-Path $RepoRoot "requirements.txt")
}

if (Test-Path (Join-Path $RepoRoot "data\ledger.db")) {
    Write-Host "Note: this checkout still has data\ledger.db. dev.ps1 ignores it and uses demo data." -ForegroundColor Yellow
}

$env:PULSE_DB_PATH = Join-Path $DemoDir "ledger.db"
$env:PULSE_PORT    = "$Port"
$env:PULSE_HOST    = "127.0.0.1"
$env:PULSE_DEBUG   = $(if ($WerkzeugDebug) { "1" } else { "0" })

if ($Reseed -and (Test-Path $env:PULSE_DB_PATH)) {
    Remove-Item "$($env:PULSE_DB_PATH)*"   # demo ledger + its -wal/-shm only
}

Push-Location $RepoRoot
try {
    if (-not (Test-Path $env:PULSE_DB_PATH)) {
        & $Py cli\seed_demo.py
    } else {
        & $Py cli\migrate.py
    }
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    Write-Host "`nDev server: http://127.0.0.1:$Port  (demo data: $($env:PULSE_DB_PATH))" -ForegroundColor Green
    & $Py web\app.py
} finally { Pop-Location }
