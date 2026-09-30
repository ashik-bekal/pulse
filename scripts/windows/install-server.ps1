<#
.SYNOPSIS
  Set up PULSE as an always-on local server on Windows.

.DESCRIPTION
  Creates a separate "server" checkout of PULSE (tracking main only), with its
  own virtualenv and its own data folder, and registers a Scheduled Task that
  starts it at boot (no login needed) and restarts it if it crashes. The
  server binds to 127.0.0.1 only. Optionally publishes it to your own devices
  with Tailscale Serve (HTTPS, tailnet-only).

  Safe to re-run: an existing checkout is fast-forwarded, an existing ledger
  is never overwritten (it is backed up and migrated instead), and the
  Scheduled Task is replaced.

  Run from a normal (non-admin) PowerShell. You will get ONE UAC prompt, for
  registering the Scheduled Task.

.EXAMPLE
  # First install, moving the real ledger out of a dev checkout:
  .\scripts\windows\install-server.ps1 -InstallDir H:\Claude\MyPULSE -ImportDataFrom .\data -DisableSleep

.EXAMPLE
  # Fresh install for someone with no existing data:
  .\scripts\windows\install-server.ps1 -InstallDir $HOME\MyPULSE
#>
[CmdletBinding()]
param(
    [string]$InstallDir = (Join-Path $HOME "MyPULSE"),
    # An existing data folder (containing ledger.db) to copy into the server.
    # Copied with SQLite's backup API, verified, then the source folder is
    # RENAMED (never deleted) so a dev checkout can't keep writing to it.
    [string]$ImportDataFrom,
    [int]$Port = 5001,
    [string]$Branch = "main",
    [string]$TaskName = "PULSE Server",
    # Stop the PC sleeping/hibernating on AC power (the server dies when it sleeps).
    [switch]$DisableSleep,
    [switch]$SkipTailscale,
    # Clone from here instead of this checkout's 'origin' remote.
    [string]$RepoUrl
)

$ErrorActionPreference = "Stop"
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path

function Step($msg) { Write-Host "`n==> $msg" -ForegroundColor Cyan }
function Fail($msg) { Write-Host "ERROR: $msg" -ForegroundColor Red; exit 1 }
function Invoke-Checked {
    param([string]$Exe, [string[]]$Arguments)
    & $Exe @Arguments
    if ($LASTEXITCODE -ne 0) { Fail "'$Exe $($Arguments -join ' ')' failed (exit $LASTEXITCODE)" }
}

# -- 0. Preconditions ----------------------------------------------------------
Step "Checking prerequisites"
$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if ($principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Fail "Run this from a normal (non-admin) PowerShell. It asks for admin only for the Scheduled Task, so the checkout and data stay owned by you."
}
if (-not (Get-Command git -ErrorAction SilentlyContinue)) { Fail "git not found on PATH." }

$PyLauncher = $null
if (Get-Command py -ErrorAction SilentlyContinue) { $PyLauncher = @("py", "-3") }
elseif (Get-Command python -ErrorAction SilentlyContinue) { $PyLauncher = @("python") }
else { Fail "Python 3 not found (install from python.org, tick 'Add to PATH')." }

$InstallDir = [IO.Path]::GetFullPath($InstallDir)
if ($InstallDir.StartsWith($RepoRoot + "\", [StringComparison]::OrdinalIgnoreCase) -or
    $InstallDir -ieq $RepoRoot) {
    Fail "InstallDir must be outside this dev checkout ($RepoRoot)."
}

# The task runs without an interactive login, so it cannot see mapped network drives.
$qualifier = Split-Path -Qualifier $InstallDir
$disk = Get-CimInstance Win32_LogicalDisk -Filter "DeviceID='$qualifier'" -ErrorAction SilentlyContinue
if ($disk -and $disk.DriveType -eq 4) {
    Fail "$qualifier is a network drive. The server must live on a local disk."
}

if (-not $RepoUrl) {
    $RepoUrl = (& git -C $RepoRoot remote get-url origin).Trim()
    if (-not $RepoUrl) { Fail "Could not read 'origin' from $RepoRoot; pass -RepoUrl." }
}

$DataDir  = Join-Path $InstallDir "data"
$DbPath   = Join-Path $DataDir "ledger.db"
$VenvPy   = Join-Path $InstallDir ".venv\Scripts\python.exe"
$LogFile  = Join-Path $DataDir "logs\server.log"
$env:PULSE_DB_PATH = $DbPath   # every python step below targets the server's ledger

# If an older copy of the server task is running, stop it so files aren't locked.
$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing -and $existing.State -eq "Running") {
    Step "Stopping the running '$TaskName' task"
    try { Stop-ScheduledTask -TaskName $TaskName }
    catch { Fail "Could not stop '$TaskName' ($_). Stop it in Task Scheduler and re-run." }
    Start-Sleep -Seconds 2
}

$listener = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($listener) {
    $proc = Get-Process -Id ($listener | Select-Object -First 1).OwningProcess -ErrorAction SilentlyContinue
    Fail "Port $Port is already in use by '$($proc.ProcessName)' (PID $($proc.Id)). Stop your dev server (or pass -Port) and re-run."
}

# -- 1. Server checkout ----------------------------------------------------------
Step "Preparing server checkout at $InstallDir ($Branch)"
if (Test-Path (Join-Path $InstallDir ".git")) {
    $dirty = & git -C $InstallDir status --porcelain --untracked-files=no
    if ($dirty) { Fail "The server checkout has local edits. It should only ever track ${Branch}:`n$dirty" }
    Invoke-Checked git @("-C", $InstallDir, "fetch", "origin", $Branch)
    Invoke-Checked git @("-C", $InstallDir, "checkout", $Branch)
    Invoke-Checked git @("-C", $InstallDir, "merge", "--ff-only", "origin/$Branch")
} elseif ((Test-Path $InstallDir) -and (Get-ChildItem $InstallDir -Force | Where-Object { $_.Name -ne "data" })) {
    Fail "$InstallDir exists and is not a PULSE checkout. Choose an empty folder."
} else {
    if (Test-Path $InstallDir) {
        # Only a data folder is present: clone beside it, then keep it.
        $tmp = "$InstallDir.clone-tmp"
        Invoke-Checked git @("clone", "--branch", $Branch, $RepoUrl, $tmp)
        Get-ChildItem $tmp -Force | Move-Item -Destination $InstallDir
        Remove-Item $tmp
    } else {
        Invoke-Checked git @("clone", "--branch", $Branch, $RepoUrl, $InstallDir)
    }
}
$commit = (& git -C $InstallDir log -1 --format="%h %s").Trim()
Write-Host "Server code at: $commit"

# -- 2. Virtualenv -----------------------------------------------------------------
Step "Installing Python dependencies"
if (-not (Test-Path $VenvPy)) {
    $launcherArgs = @()
    if ($PyLauncher.Count -gt 1) { $launcherArgs = $PyLauncher[1..($PyLauncher.Count - 1)] }
    Invoke-Checked $PyLauncher[0] ($launcherArgs + @("-m", "venv", (Join-Path $InstallDir ".venv")))
}
Invoke-Checked $VenvPy @("-m", "pip", "install", "--quiet", "--upgrade", "pip")
Invoke-Checked $VenvPy @("-m", "pip", "install", "--quiet", "-r", (Join-Path $InstallDir "requirements.txt"))

# -- 3. Data -------------------------------------------------------------------------
Push-Location $InstallDir
try {
    if (Test-Path $DbPath) {
        Step "Existing ledger found - backing it up and applying migrations"
        if ($ImportDataFrom) { Write-Host "(-ImportDataFrom ignored: the server already has a ledger; it is never overwritten.)" -ForegroundColor Yellow }
        Invoke-Checked $VenvPy @("cli\backup.py", "--reason", "pre-install")
    } elseif ($ImportDataFrom) {
        Step "Moving data from $ImportDataFrom"
        $src = (Resolve-Path $ImportDataFrom).Path
        Invoke-Checked $VenvPy @("cli\move_data.py", "--src", $src, "--dest", $DataDir, "--retire-source")
    } else {
        Step "No existing data - creating a starter ledger"
        Invoke-Checked $VenvPy @("cli\init_db.py")
    }
    Invoke-Checked $VenvPy @("cli\migrate.py")
} finally { Pop-Location }

# -- 4. Scheduled Task (the one elevated step) ----------------------------------------
Step "Registering Scheduled Task '$TaskName' (approve the UAC prompt)"
$user = [Security.Principal.WindowsIdentity]::GetCurrent()
$registerArgs = @(
    "-NoProfile", "-ExecutionPolicy", "Bypass",
    "-File", "`"$(Join-Path $PSScriptRoot 'register-server-task.ps1')`"",
    "-TaskName", "`"$TaskName`"",
    "-InstallDir", "`"$InstallDir`"",
    "-Port", $Port,
    "-UserName", "`"$($user.Name)`"",
    "-UserSid", $user.User.Value
)
if ($DisableSleep) { $registerArgs += "-DisableSleep" }
$p = Start-Process powershell.exe -Verb RunAs -Wait -PassThru -ArgumentList $registerArgs
if ($p.ExitCode -ne 0) { Fail "Registering the Scheduled Task failed (exit $($p.ExitCode)). See the message in the admin window." }

# -- 5. Health check --------------------------------------------------------------------
Step "Waiting for PULSE on http://127.0.0.1:$Port"
$ok = $false
for ($i = 0; $i -lt 30; $i++) {
    try {
        $r = Invoke-WebRequest -UseBasicParsing -TimeoutSec 3 "http://127.0.0.1:$Port/"
        if ($r.StatusCode -eq 200) { $ok = $true; break }
    } catch { Start-Sleep -Seconds 1 }
}
if (-not $ok) {
    Write-Host "PULSE did not answer within 30s. Last log lines ($LogFile):" -ForegroundColor Red
    if (Test-Path $LogFile) { Get-Content $LogFile -Tail 20 }
    exit 1
}
Write-Host "PULSE is up." -ForegroundColor Green

# -- 6. Tailscale -------------------------------------------------------------------------
$tsUrl = $null
if (-not $SkipTailscale) {
    Step "Publishing to your tailnet with Tailscale Serve"
    $ts = Get-Command tailscale -ErrorAction SilentlyContinue
    if (-not $ts) {
        Write-Host "Tailscale not installed. Install it (https://tailscale.com/download), sign in, then run:" -ForegroundColor Yellow
        Write-Host "  tailscale serve --bg --https=443 http://127.0.0.1:$Port"
    } else {
        & tailscale serve --bg --https=443 "http://127.0.0.1:$Port"
        if ($LASTEXITCODE -ne 0) {
            Write-Host "tailscale serve failed. Check you are signed in and that HTTPS certificates are enabled for your tailnet (admin console > DNS), then run the command above." -ForegroundColor Yellow
        } else {
            try {
                $dns = ((& tailscale status --json) | ConvertFrom-Json).Self.DNSName.TrimEnd(".")
                $tsUrl = "https://$dns"
            } catch { }
        }
    }
}

Write-Host "`nDone." -ForegroundColor Green
Write-Host "  On this PC:        http://127.0.0.1:$Port"
if ($tsUrl) { Write-Host "  Phone / Mac:       $tsUrl   (devices signed in to your tailnet only)" }
Write-Host "  Server code:       $InstallDir  ($commit)"
Write-Host "  Ledger:            $DbPath"
Write-Host "  Logs:              $LogFile"
Write-Host "  Update later with: .\scripts\windows\update-server.ps1 -InstallDir `"$InstallDir`""
