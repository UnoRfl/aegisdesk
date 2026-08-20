<#
.SYNOPSIS
  One-shot AegisDesk agent installer for Windows 10/11.

.DESCRIPTION
  Installs Python if missing, installs the agent's dependencies, enrolls this
  computer with your relay, and registers it to start at logon.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File install-windows.ps1 `
      -Relay wss://desk.myrestaurant.com -EnrollKey abc123 -Name "POS-01" -Password "Front0fH0use!"

.NOTES
  Leave -Password out to require someone at the computer to click Allow for
  every session. Supply it to allow unattended access.
#>
param(
  [Parameter(Mandatory = $true)][string]$Relay,
  [Parameter(Mandatory = $true)][string]$EnrollKey,
  [string]$Name = $env:COMPUTERNAME,
  [string]$Password = "",
  [switch]$Elevated,
  [switch]$SkipDeps
)

$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $here

function Say($msg, $colour = "Cyan") { Write-Host "  $msg" -ForegroundColor $colour }

Write-Host ""
Write-Host "  AegisDesk agent installer" -ForegroundColor White
Write-Host "  =========================" -ForegroundColor DarkGray
Write-Host ""

# ---------------------------------------------------------------- python
$py = $null
foreach ($cand in @("py -3", "python", "python3")) {
  try {
    $parts = $cand.Split(" ")
    $v = & $parts[0] $parts[1..99] --version 2>&1
    if ($LASTEXITCODE -eq 0 -and $v -match "Python 3\.(\d+)" -and [int]$Matches[1] -ge 9) {
      $py = $cand; Say "found $v"; break
    }
  } catch { }
}

if (-not $py) {
  Say "Python 3.9+ not found. Installing via winget..." "Yellow"
  try {
    winget install -e --id Python.Python.3.12 --scope machine --accept-package-agreements --accept-source-agreements
    $env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
                [System.Environment]::GetEnvironmentVariable("Path", "User")
    $py = "py -3"
    & py -3 --version | Out-Null
    Say "Python installed."
  } catch {
    Say "Could not install Python automatically." "Red"
    Say "Install it from https://www.python.org/downloads/windows/ (tick 'Add python.exe to PATH'), then re-run this script." "Red"
    exit 1
  }
}

$pyParts = $py.Split(" ")
function Py { & $pyParts[0] $pyParts[1..99] @args }

# ---------------------------------------------------------------- deps
if (-not $SkipDeps) {
  Say "installing dependencies (this takes a minute)..."
  Py -m pip install --upgrade pip --quiet
  Py -m pip install -r (Join-Path $here "requirements.txt") --quiet
  if ($LASTEXITCODE -ne 0) { Say "dependency install failed" "Red"; exit 1 }
  Say "dependencies installed."
}

# ---------------------------------------------------------------- enroll
Say "enrolling with $Relay ..."
$setupArgs = @("-m", "aegis_agent", "setup", "--relay", $Relay, "--enroll-key", $EnrollKey, "--name", $Name)
if ($Password) { $setupArgs += @("--password", $Password) } else { $setupArgs += "--no-password-prompt" }
Py @setupArgs
if ($LASTEXITCODE -ne 0) { Say "setup failed" "Red"; exit 1 }

# ---------------------------------------------------------------- autostart
Say "registering logon task..."
if ($Elevated) { Py -m aegis_agent install --elevated } else { Py -m aegis_agent install }

# ---------------------------------------------------------------- firewall (outbound only, usually already allowed)
try {
  $exe = (Get-Command python -ErrorAction SilentlyContinue).Source
  if ($exe) {
    New-NetFirewallRule -DisplayName "AegisDesk agent (outbound)" -Direction Outbound `
      -Program $exe -Action Allow -ErrorAction SilentlyContinue | Out-Null
  }
} catch { }

# ---------------------------------------------------------------- start
Say "starting the agent..."
Start-Process -WindowStyle Hidden -FilePath $pyParts[0] -ArgumentList (@($pyParts[1..99]) + @("-m", "aegis_agent", "run"))
Start-Sleep -Seconds 6

Write-Host ""
Py -m aegis_agent status
Write-Host ""
Say "Done. This computer should now appear in your AegisDesk fleet list." "Green"
if (-not $Password) {
  Say "No unattended password was set, so someone at this computer must click Allow for each session." "Yellow"
  Say "To set one later:  $py -m aegis_agent password" "DarkGray"
}
Write-Host ""
