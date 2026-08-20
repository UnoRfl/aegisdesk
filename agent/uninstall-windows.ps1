# Removes the AegisDesk agent's autostart entry and stops it.
# The config (device ID, password hash) stays in C:\ProgramData\AegisDesk
# unless you pass -Purge.
param([switch]$Purge)

$ErrorActionPreference = "Continue"
Write-Host "`n  Removing AegisDesk agent..." -ForegroundColor Cyan

schtasks /Delete /TN AegisDeskAgent /F 2>$null
Get-Process python, pythonw -ErrorAction SilentlyContinue |
  Where-Object { $_.CommandLine -like "*aegis_agent*" } |
  Stop-Process -Force -ErrorAction SilentlyContinue
Get-WmiObject Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" |
  Where-Object { $_.CommandLine -like "*aegis_agent*" } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

Remove-NetFirewallRule -DisplayName "AegisDesk agent (outbound)" -ErrorAction SilentlyContinue

if ($Purge) {
  $dir = Join-Path $env:ProgramData "AegisDesk"
  if (Test-Path $dir) { Remove-Item -Recurse -Force $dir; Write-Host "  purged $dir" -ForegroundColor Yellow }
  Write-Host "  Remember to remove this device from the relay's admin page too." -ForegroundColor Yellow
}

Write-Host "  Done.`n" -ForegroundColor Green
