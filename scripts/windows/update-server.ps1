<#
.SYNOPSIS
  Deploy the latest main to the always-on PULSE server.

.DESCRIPTION
  Backup -> stop -> fast-forward to origin/main -> install deps -> migrate ->
  start -> health check. If any step fails the server is left STOPPED and
  the script prints exactly how to roll back (previous commit + backup file);
  it never rolls back or deletes anything by itself.

.EXAMPLE
  .\scripts\windows\update-server.ps1 -InstallDir H:\Claude\MyPULSE
#>
[CmdletBinding()]
param(
    [string]$InstallDir = (Join-Path $HOME "MyPULSE"),
    [int]$Port = 5001,
    [string]$Branch = "main",
    [string]$TaskName = "PULSE Server"
)
$ErrorActionPreference = "Stop"

function Step($msg) { Write-Host "`n==> $msg" -ForegroundColor Cyan }

$InstallDir = [IO.Path]::GetFullPath($InstallDir)
$VenvPy  = Join-Path $InstallDir ".venv\Scripts\python.exe"
$DbPath  = Join-Path $InstallDir "data\ledger.db"
$LogFile = Join-Path $InstallDir "data\logs\server.log"
$env:PULSE_DB_PATH = $DbPath

if (-not (Test-Path (Join-Path $InstallDir ".git"))) { Write-Host "ERROR: $InstallDir is not a PULSE server checkout. Run install-server.ps1 first." -ForegroundColor Red; exit 1 }
if (-not (Test-Path $DbPath)) { Write-Host "ERROR: no ledger at $DbPath." -ForegroundColor Red; exit 1 }

function Set-TaskState([string]$verb) {
    # Try as the current user first (install grants this); fall back to one UAC prompt.
    try {
        if ($verb -eq "Stop") { Stop-ScheduledTask -TaskName $TaskName } else { Start-ScheduledTask -TaskName $TaskName }
    } catch {
        Write-Host "Needs admin to $($verb.ToLower()) '$TaskName' - approve the UAC prompt."
        $cmd = "$verb-ScheduledTask -TaskName '$TaskName'"
        $p = Start-Process powershell.exe -Verb RunAs -Wait -PassThru -ArgumentList @("-NoProfile", "-Command", $cmd)
        if ($p.ExitCode -ne 0) { throw "Could not $($verb.ToLower()) '$TaskName'." }
    }
}

$dirty = & git -C $InstallDir status --porcelain --untracked-files=no
if ($dirty) { Write-Host "ERROR: the server checkout has local edits (it should only track ${Branch}):`n$dirty" -ForegroundColor Red; exit 1 }

Step "Checking for updates"
& git -C $InstallDir fetch origin $Branch
if ($LASTEXITCODE -ne 0) { Write-Host "ERROR: git fetch failed." -ForegroundColor Red; exit 1 }
$old = (& git -C $InstallDir rev-parse HEAD).Trim()
$incoming = & git -C $InstallDir log --oneline "HEAD..origin/$Branch"
if (-not $incoming) { Write-Host "Already up to date ($($old.Substring(0,7)))."; exit 0 }
Write-Host "Incoming commits:"; $incoming | ForEach-Object { Write-Host "  $_" }

$backup = $null
$stopped = $false
try {
    Step "Backing up the ledger"
    Push-Location $InstallDir
    try {
        $out = & $VenvPy cli\backup.py --reason pre-update
        if ($LASTEXITCODE -ne 0) { throw "backup failed" }
        $out | ForEach-Object { Write-Host $_ }
        $backup = (($out | Select-String "Backup written:") -replace "^Backup written:\s*", "").Trim()
    } finally { Pop-Location }

    Step "Stopping '$TaskName'"
    Set-TaskState "Stop"
    $stopped = $true
    for ($i = 0; $i -lt 15 -and (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue); $i++) { Start-Sleep -Seconds 1 }

    Step "Updating code"
    & git -C $InstallDir merge --ff-only "origin/$Branch"
    if ($LASTEXITCODE -ne 0) { throw "git fast-forward failed" }
    & $VenvPy -m pip install --quiet -r (Join-Path $InstallDir "requirements.txt")
    if ($LASTEXITCODE -ne 0) { throw "pip install failed" }

    Step "Applying migrations"
    Push-Location $InstallDir
    try {
        & $VenvPy cli\migrate.py
        if ($LASTEXITCODE -ne 0) { throw "migration failed" }
    } finally { Pop-Location }

    Step "Starting '$TaskName'"
    Set-TaskState "Start"
    $ok = $false
    for ($i = 0; $i -lt 30; $i++) {
        try {
            if ((Invoke-WebRequest -UseBasicParsing -TimeoutSec 3 "http://127.0.0.1:$Port/").StatusCode -eq 200) { $ok = $true; break }
        } catch { }
        Start-Sleep -Seconds 1
    }
    if (-not $ok) { throw "PULSE did not answer on port $Port within 30s after starting" }
} catch {
    Write-Host "`nUPDATE FAILED: $_" -ForegroundColor Red
    if (Test-Path $LogFile) { Write-Host "Last log lines:"; Get-Content $LogFile -Tail 15 }
    if ($stopped) { Write-Host "The server is STOPPED." -ForegroundColor Red }
    Write-Host "To roll back:"
    Write-Host "  git -C `"$InstallDir`" reset --hard $old"
    if ($backup) { Write-Host "  copy `"$backup`" over `"$DbPath`" (only if a migration ran)" }
    Write-Host "  Start-ScheduledTask -TaskName '$TaskName'"
    exit 1
}

$now = (& git -C $InstallDir log -1 --format="%h %s").Trim()
Write-Host "`nUpdated and running: $now" -ForegroundColor Green
if ($backup) { Write-Host "Pre-update backup: $backup" }
