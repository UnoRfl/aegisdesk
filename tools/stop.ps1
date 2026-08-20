<#
.SYNOPSIS
  Stops the AegisDesk relay and agent.

.DESCRIPTION
  Finds only the processes belonging to this checkout by matching their
  command lines, so it will not touch other Node or Python work you have
  running.
#>
param([switch]$Purge)

$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)

Write-Host ""
Write-Host "  Stopping AegisDesk" -ForegroundColor Cyan
Write-Host ""

$killed = 0

function Stop-Matching($namePattern, $cmdPattern, $label) {
  $procs = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -match $namePattern -and $_.CommandLine -and $_.CommandLine -match $cmdPattern }
  foreach ($p in $procs) {
    try {
      Stop-Process -Id $p.ProcessId -Force -ErrorAction Stop
      Write-Host "    stopped $label (pid $($p.ProcessId))" -ForegroundColor Green
      $script:killed++
    } catch {
      Write-Host "    could not stop pid $($p.ProcessId): $($_.Exception.Message)" -ForegroundColor Yellow
    }
  }
}

Stop-Matching 'node\.exe' 'server\.js' 'relay'
Stop-Matching '^(python|pythonw)\.exe$' 'aegis_agent' 'agent'

if ($killed -eq 0) {
  Write-Host "    nothing was running" -ForegroundColor DarkGray
}

if ($Purge) {
  Write-Host ""
  Write-Host "  Purging local state" -ForegroundColor Yellow
  $agentCfg = Join-Path $env:ProgramData "AegisDesk"
  $relayData = Join-Path $root "relay\data"
  foreach ($d in @($agentCfg, $relayData)) {
    if (Test-Path $d) {
      Remove-Item -Recurse -Force $d -ErrorAction SilentlyContinue
      Write-Host "    removed $d" -ForegroundColor DarkGray
    }
  }
  Write-Host "    next START.bat will set up from scratch with new credentials" -ForegroundColor DarkGray
}

Write-Host ""
Write-Host "  Done. Press Enter to close." -ForegroundColor DarkGray
Read-Host | Out-Null
