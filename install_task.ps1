# Registers "SteamWallpaperTheme" as a scheduled task with two triggers:
#   1. At logon          -> runs the polling service (pythonw main.py)
#   2. Workstation unlock -> re-applies the cached theme (--reapply),
#      fixing the "hybrid Custom" half-applied theme Windows can leave behind
#      when a theme is applied while the session was locked.
#
# Run from an elevated or normal PowerShell prompt (user-level task, no admin needed):
#   powershell -ExecutionPolicy Bypass -File install_task.ps1
#
# Remove with:
#   schtasks /Delete /TN "SteamWallpaperTheme" /F
#   schtasks /Delete /TN "SteamWallpaperTheme-Unlock" /F

$ErrorActionPreference = "Stop"
$dir = Split-Path -Parent $MyInvocation.MyCommand.Path

$pythonw = (Get-Command pythonw -ErrorAction SilentlyContinue).Source
if (-not $pythonw) {
    $py = (Get-Command python).Source
    $pythonw = Join-Path (Split-Path $py) "pythonw.exe"
}
if (-not (Test-Path $pythonw)) { throw "pythonw.exe not found" }

$main = Join-Path $dir "main.py"

# ---- Task 1: service at logon ------------------------------------------------
$xml1 = @"
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.3" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <Triggers>
    <LogonTrigger><Enabled>true</Enabled></LogonTrigger>
  </Triggers>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
  </Settings>
  <Actions>
    <Exec>
      <Command>$pythonw</Command>
      <Arguments>"$main"</Arguments>
      <WorkingDirectory>$dir</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"@

# ---- Task 2: re-apply on workstation unlock ----------------------------------
$xml2 = @"
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.3" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <Triggers>
    <SessionStateChangeTrigger>
      <Enabled>true</Enabled>
      <StateChange>SessionUnlock</StateChange>
    </SessionStateChangeTrigger>
  </Triggers>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <ExecutionTimeLimit>PT5M</ExecutionTimeLimit>
  </Settings>
  <Actions>
    <Exec>
      <Command>$pythonw</Command>
      <Arguments>"$main" --reapply</Arguments>
      <WorkingDirectory>$dir</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"@

$f1 = Join-Path $env:TEMP "swt_logon.xml"
$f2 = Join-Path $env:TEMP "swt_unlock.xml"
[IO.File]::WriteAllText($f1, $xml1, [Text.Encoding]::Unicode)
[IO.File]::WriteAllText($f2, $xml2, [Text.Encoding]::Unicode)

schtasks /Create /TN "SteamWallpaperTheme" /XML $f1 /F
schtasks /Create /TN "SteamWallpaperTheme-Unlock" /XML $f2 /F
Remove-Item $f1, $f2

Write-Host ""
Write-Host "Installed:"
Write-Host "  SteamWallpaperTheme         (logon -> service, $pythonw `"$main`")"
Write-Host "  SteamWallpaperTheme-Unlock  (unlock -> --reapply)"
