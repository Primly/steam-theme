# Registers three user-level scheduled tasks:
#   SteamTheme         logon  -> polling service (pythonw main.py)
#   SteamTheme-Unlock  unlock -> re-apply cached theme (--reapply),
#                                fixing the "hybrid Custom" half-applied theme
#   SteamThemeUI       logon  -> browser config UI on 127.0.0.1:8765
#
# Also migrates tasks registered under the app's previous name
# (SteamWallpaperTheme / SteamWallpaperTheme-Unlock / SteamWallpaperUI).
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File install_task.ps1
# Remove:
#   Unregister-ScheduledTask -TaskName SteamTheme -Confirm:$false
#   Unregister-ScheduledTask -TaskName SteamTheme-Unlock -Confirm:$false
#   Unregister-ScheduledTask -TaskName SteamThemeUI -Confirm:$false

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

function Remove-LegacyTask($name) {
    if (Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue) {
        # stop first if running, then remove
        Get-ScheduledTask -TaskName $name | Stop-ScheduledTask -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $name -Confirm:$false -ErrorAction SilentlyContinue
        Write-Host "Migrated away from legacy task '$name'."
    }
}

Register-ScheduledTask -TaskName "SteamTheme" -Force `
    -Action (New-ScheduledTaskAction -Execute $pythonw -Argument "`"$main`"" -WorkingDirectory $dir) `
    -Trigger $logon -Settings $svcSettings -Principal $principal | Out-Null
Remove-LegacyTask "SteamWallpaperTheme"

$unlockInstalled = $false
try {
    Register-ScheduledTask -TaskName "SteamTheme-Unlock" -Force `
        -Action (New-ScheduledTaskAction -Execute $pythonw -Argument "`"$main`" --reapply" -WorkingDirectory $dir) `
        -Trigger $unlock -Settings $quickSettings -Principal $principal | Out-Null
    $unlockInstalled = $true
    Remove-LegacyTask "SteamWallpaperTheme-Unlock"
} catch [System.UnauthorizedAccessException], [Microsoft.Management.Infrastructure.CimException] {
    Write-Host "NOTE: unlock trigger needs an elevated shell; skipped."
    Write-Host "      Re-run this script as Administrator to add it (optional)."
    # leave any legacy SteamWallpaperTheme-Unlock task in place: it points at
    # the same main.py and still works
}

Register-ScheduledTask -TaskName "SteamThemeUI" -Force `
    -Action (New-ScheduledTaskAction -Execute $pythonw -Argument "`"$webui`"" -WorkingDirectory $dir) `
    -Trigger $logon -Settings $svcSettings -Principal $principal | Out-Null
Remove-LegacyTask "SteamWallpaperUI"

Write-Host ""
Write-Host "Installed:"
Write-Host "  SteamTheme         (logon -> polling service)"
if ($unlockInstalled) { Write-Host "  SteamTheme-Unlock  (unlock -> --reapply)" }
Write-Host "  SteamThemeUI       (logon -> http://127.0.0.1:8765)"
Write-Host ""
Write-Host "Start now with:  schtasks /Run /TN SteamThemeUI"
Write-Host "                 schtasks /Run /TN SteamTheme"
