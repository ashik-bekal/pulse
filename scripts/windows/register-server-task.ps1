<#
.SYNOPSIS
  Elevated helper for install-server.ps1: registers the PULSE Scheduled Task.
  Not meant to be run on its own.

  The task starts at boot WITHOUT anyone logging in ("S4U": runs as you, no
  stored password, no network-share access - it only needs local files and
  127.0.0.1), restarts every minute on failure, and never times out.
  It also grants your own account start/stop rights on the task, so
  update-server.ps1 can restart it without another UAC prompt.
#>
param(
    [Parameter(Mandatory)] [string]$TaskName,
    [Parameter(Mandatory)] [string]$InstallDir,
    [Parameter(Mandatory)] [int]$Port,
    [Parameter(Mandatory)] [string]$UserName,
    [Parameter(Mandatory)] [string]$UserSid,
    [switch]$DisableSleep
)
$ErrorActionPreference = "Stop"
try {
    $python = Join-Path $InstallDir ".venv\Scripts\python.exe"
    $db     = Join-Path $InstallDir "data\ledger.db"
    $log    = Join-Path $InstallDir "data\logs\server.log"
    $action = New-ScheduledTaskAction -Execute $python -WorkingDirectory $InstallDir `
        -Argument "cli\serve.py --port $Port --db-path `"$db`" --log-file `"$log`""
    $settings = New-ScheduledTaskSettingsSet `
        -ExecutionTimeLimit ([TimeSpan]::Zero) `
        -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
        -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -StartWhenAvailable -MultipleInstances IgnoreNew

    $description = "PULSE ledger server on 127.0.0.1:$Port (installed by scripts/windows/install-server.ps1)"
    try {
        $principal = New-ScheduledTaskPrincipal -UserId $UserName -LogonType S4U -RunLevel Limited
        Register-ScheduledTask -TaskName $TaskName -Action $action -Settings $settings `
            -Trigger (New-ScheduledTaskTrigger -AtStartup) -Principal $principal `
            -Description $description -Force | Out-Null
        Write-Host "Registered '$TaskName': starts at boot, no login needed."
    } catch {
        # Some account setups reject S4U. Fall back to starting at your logon.
        Write-Host "Boot-time (S4U) registration failed: $_" -ForegroundColor Yellow
        Write-Host "Falling back to 'start when $UserName logs on'." -ForegroundColor Yellow
        $principal = New-ScheduledTaskPrincipal -UserId $UserName -LogonType Interactive -RunLevel Limited
        Register-ScheduledTask -TaskName $TaskName -Action $action -Settings $settings `
            -Trigger (New-ScheduledTaskTrigger -AtLogOn -User $UserName) -Principal $principal `
            -Description $description -Force | Out-Null
    }

    # Let the installing user start/stop this task without elevation.
    try {
        $svc = New-Object -ComObject Schedule.Service
        $svc.Connect()
        $task = $svc.GetFolder("\").GetTask($TaskName)
        $sddl = $task.GetSecurityDescriptor(4)   # DACL only
        if ($sddl -notmatch [regex]::Escape(";;;$UserSid)")) {
            $task.SetSecurityDescriptor($sddl + "(A;;FA;;;$UserSid)", 0)
        }
    } catch {
        Write-Host "Note: could not grant non-admin control of the task ($_). update-server.ps1 will ask for admin when restarting." -ForegroundColor Yellow
    }

    if ($DisableSleep) {
        powercfg /change standby-timeout-ac 0
        powercfg /change hibernate-timeout-ac 0
        Write-Host "Sleep and hibernate disabled on AC power."
    }

    Start-ScheduledTask -TaskName $TaskName
    Write-Host "Started '$TaskName'."
    exit 0
} catch {
    Write-Host "ERROR: $_" -ForegroundColor Red
    Read-Host "Press Enter to close"
    exit 1
}
