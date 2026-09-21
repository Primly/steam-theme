# Registers three user-level scheduled tasks:
#   SteamWallpaperTheme         logon  -> polling service (pythonw main.py)
#   SteamWallpaperTheme-Unlock  unlock -> re-apply cached theme (--reapply),
#                                fixing the "hybrid Custom" half-applied theme
#   SteamWallpaperUI            logon  -> browser config UI on 127.0.0.1:8765
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File install_task.ps1
# Remove:
#   Unregister-ScheduledTask -TaskName SteamWallpaperTheme -Confirm:$false
#   Unregister-ScheduledTask -TaskName SteamWallpaperTheme-Unlock -Confirm:$false
#   Unregister-ScheduledTask -TaskName SteamWallpaperUI -Confirm:$false

$ErrorActionPreference = "Stop"
$dir = Split-Path -Parent $MyInvocation.MyCommand.Path

$pythonw = (Get-Command pythonw -ErrorAction SilentlyContinue).Source
if (-not $pythonw) {
    $py = (Get-Command python).Source
    $pythonw = Join-Path (Split-Path $py) "pythonw.exe"
}
if (-not (Test-Path $pythonw)) { throw "pythonw.exe not found" }

$main  = Join-Path $dir "main.py"
$webui = Join-Path $dir "webui.py"
$user  = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive

$logon  = New-ScheduledTaskTrigger -AtLogOn -User $user
$unlockClass = Get-CimClass -Namespace "root\Microsoft\Windows\TaskScheduler" `
    -ClassName "MSFT_TaskSessionStateChangeTrigger"
$unlock = New-CimInstance -CimClass $unlockClass -ClientOnly `
    -Property @{ StateChange = 8 }   # 8 = SessionUnlock

$svcSettings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries
$quickSettings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 5)

Register-ScheduledTask -TaskName "SteamWallpaperTheme" -Force `
    -Action (New-ScheduledTaskAction -Execute $pythonw -Argument "`"$main`"" -WorkingDirectory $dir) `
    -Trigger $logon -Settings $svcSettings -Principal $principal | Out-Null

try {
    Register-ScheduledTask -TaskName "SteamWallpaperTheme-Unlock" -Force `
        -Action (New-ScheduledTaskAction -Execute $pythonw -Argument "`"$main`" --reapply" -WorkingDirectory $dir) `
        -Trigger $unlock -Settings $quickSettings -Principal $principal | Out-Null
} catch [System.UnauthorizedAccessException], [Microsoft.Management.Infrastructure.CimException] {
    Write-Host "NOTE: unlock trigger needs an elevated shell; skipped."
    Write-Host "      Re-run this script as Administrator to add it (optional)."
}

Register-ScheduledTask -TaskName "SteamWallpaperUI" -Force `
    -Action (New-ScheduledTaskAction -Execute $pythonw -Argument "`"$webui`"" -WorkingDirectory $dir) `
    -Trigger $logon -Settings $svcSettings -Principal $principal | Out-Null

Write-Host ""
Write-Host "Installed:"
Write-Host "  SteamWallpaperTheme         (logon -> polling service)"
Write-Host "  SteamWallpaperTheme-Unlock  (unlock -> --reapply)"
Write-Host "  SteamWallpaperUI            (logon -> http://127.0.0.1:8765)"
Write-Host ""
Write-Host "Start now with:  schtasks /Run /TN SteamWallpaperUI"
Write-Host "                 schtasks /Run /TN SteamWallpaperTheme"
