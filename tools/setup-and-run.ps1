<#
.SYNOPSIS
  One-shot AegisDesk setup and launch for a single Windows PC.

.DESCRIPTION
  Does everything Part 1 of the guide does, without you typing anything:
    1. finds (or installs) Node.js and Python
    2. installs the relay and agent dependencies
    3. provisions the relay and reads back the credentials
    4. starts the relay and the agent in their own windows
    5. enrolls this PC and opens the viewer in your browser

  Safe to run more than once. It reuses what already exists rather than
  starting over: an already-enrolled agent keeps its device ID, and an
  existing admin password is left alone unless you ask for a new one.

.PARAMETER Port
  Relay port. Default 7443.

.PARAMETER DevicePassword
  Access password for this PC. Pass "" to require someone to click Allow
  for every session instead (that is the mode you want to see at least once).

.PARAMETER ResetPassword
  Generate a new admin password even though one already exists.

.PARAMETER SkipDeps
  Skip npm install / pip install. Much faster on later runs.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File tools\setup-and-run.ps1

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File tools\setup-and-run.ps1 -DevicePassword "" -ResetPassword
#>
param(
  [int]$Port = 7443,
  [string]$DevicePassword = "TestPass123!",
  [switch]$ResetPassword,
  [switch]$SkipDeps,
  [switch]$NoBrowser
)

$ErrorActionPreference = "Continue"
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$relayDir = Join-Path $root "relay"
$agentDir = Join-Path $root "agent"
$dataDir  = Join-Path $relayDir "data"

$step = 0
function Step($msg) {
  $script:step++
  Write-Host ""
  Write-Host "[$script:step] $msg" -ForegroundColor Cyan
}
function Ok($msg)   { Write-Host "    OK  $msg" -ForegroundColor Green }
function Info($msg) { Write-Host "        $msg" -ForegroundColor DarkGray }
function Warn($msg) { Write-Host "    !   $msg" -ForegroundColor Yellow }
function Die($msg) {
  Write-Host ""
  Write-Host "    STOPPED: $msg" -ForegroundColor Red
  Write-Host ""
  Write-Host "  Press Enter to close." -ForegroundColor DarkGray
  Read-Host | Out-Null
  exit 1
}

Write-Host ""
Write-Host "  AegisDesk - one-click local setup" -ForegroundColor White
Write-Host "  ---------------------------------" -ForegroundColor DarkGray
Write-Host "  Folder: $root" -ForegroundColor DarkGray

if (-not (Test-Path $relayDir) -or -not (Test-Path $agentDir)) {
  Die "this script must live in the aegisdesk folder, beside 'relay' and 'agent'."
}

# =====================================================================
Step "Checking Node.js"

function Test-Node {
  try {
    $v = & node --version 2>&1
    if ($LASTEXITCODE -eq 0 -and "$v" -match 'v(\d+)\.') {
      if ([int]$Matches[1] -ge 18) { return "$v".Trim() }
      Warn "found Node $v but 18+ is needed"
    }
  } catch { }
  return $null
}

$nodeVersion = Test-Node
if (-not $nodeVersion) {
  Warn "Node.js not found - installing (this takes a minute)"
  & winget install -e --id OpenJS.NodeJS.LTS --accept-package-agreements --accept-source-agreements 2>&1 | Out-Null
  $machinePath = [Environment]::GetEnvironmentVariable("Path", "Machine")
  $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
  $env:Path = "$machinePath;$userPath"
  $nodeVersion = Test-Node
}
if (-not $nodeVersion) {
  Die "could not install Node.js. Get it from https://nodejs.org (LTS), reopen this window, and run again."
}
Ok "Node $nodeVersion"

# =====================================================================
Step "Checking Python"

$pyCmd = $null
function Test-Python {
  $candidates = @(, @("py", "-3")) + @(, @("python")) + @(, @("python3"))
  foreach ($c in $candidates) {
    try {
      $exe = $c[0]
      $rest = @()
      if ($c.Count -gt 1) { $rest = $c[1..($c.Count - 1)] }
      $args2 = $rest + @("--version")
      $v = & $exe @args2 2>&1
      if ($LASTEXITCODE -eq 0 -and "$v" -match 'Python 3\.(\d+)' -and [int]$Matches[1] -ge 9) {
        Info "using '$exe $($rest -join ' ')' -> $("$v".Trim())"
        return , @($exe) + $rest
      }
    } catch { }
  }
  return $null
}

$pyCmd = Test-Python
if (-not $pyCmd) {
  Warn "Python 3.9+ not found - installing (this takes a couple of minutes)"
  & winget install -e --id Python.Python.3.12 --accept-package-agreements --accept-source-agreements 2>&1 | Out-Null
  $machinePath = [Environment]::GetEnvironmentVariable("Path", "Machine")
  $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
  $env:Path = "$machinePath;$userPath"
  $pyCmd = Test-Python
}
if (-not $pyCmd) {
  Die "could not install Python. Get it from https://www.python.org/downloads/windows/ (tick 'Add python.exe to PATH'), reopen this window, and run again."
}
$pyExe = $pyCmd[0]
$pyArgs = @()
if ($pyCmd.Count -gt 1) { $pyArgs = $pyCmd[1..($pyCmd.Count - 1)] }
Ok "Python ready"

# =====================================================================
Step "Installing dependencies"

if ($SkipDeps) {
  Info "skipped (-SkipDeps)"
} else {
  if (Test-Path (Join-Path $relayDir "node_modules\ws")) {
    Info "relay dependencies already present"
  } else {
    Info "npm install (about 15 seconds)"
    Push-Location $relayDir
    & npm install --no-audit --no-fund 2>&1 | Out-Null
    Pop-Location
    if (-not (Test-Path (Join-Path $relayDir "node_modules\ws"))) {
      Die "npm install failed. Run it by hand in the relay folder to see the error."
    }
  }
  Ok "relay dependencies"

  $probe = @()
  $probe += $pyArgs
  $probe += @("-c", "import mss, numpy, cryptography, websocket, PIL")
  & $pyExe @probe 2>&1 | Out-Null
  if ($LASTEXITCODE -eq 0) {
    Info "agent dependencies already present"
  } else {
    Info "pip install (2-3 minutes the first time - numpy and opencv are large)"
    $pipArgs = @()
    $pipArgs += $pyArgs
    $pipArgs += @("-m", "pip", "install", "-r", (Join-Path $agentDir "requirements.txt"), "--quiet", "--disable-pip-version-check")
    & $pyExe @pipArgs 2>&1 | Out-Null
    & $pyExe @probe 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) {
      Die "pip install did not complete. Run it by hand in the agent folder to see the error."
    }
  }
  Ok "agent dependencies"

  # These are optional. Missing wheels on a brand-new Python version are common
  # and the agent degrades gracefully, but you should know which you got.
  $extras = @{ "cv2" = "faster JPEG encoding"; "psutil" = "CPU/RAM metrics and process list"; "pystray" = "tray icon" }
  foreach ($mod in $extras.Keys) {
    $chk = @()
    $chk += $pyArgs
    $chk += @("-c", "import $mod")
    & $pyExe @chk 2>&1 | Out-Null
    if ($LASTEXITCODE -eq 0) { Info "optional: $mod present ($($extras[$mod]))" }
    else { Warn "optional: $mod missing - $($extras[$mod]) unavailable (everything else still works)" }
  }
}

# =====================================================================
Step "Provisioning the relay"

# The relay defaults its data dir to relay\data, which is exactly where we
# want it, so no path needs passing. That matters: paths with spaces are the
# main way these scripts break.
$initArgs = @("server.js", "--init")
if ($ResetPassword) { $initArgs += "--reset-admin" }

Push-Location $relayDir
$initOut = & node @initArgs 2>&1
Pop-Location

$cfg = @{}
foreach ($line in $initOut) {
  $text = "$line"
  if ($text -match '^AEGIS_([A-Z_]+)=(.*)$') { $cfg[$Matches[1]] = $Matches[2] }
}
if (-not $cfg.ContainsKey("ENROLL_KEY") -or [string]::IsNullOrWhiteSpace($cfg["ENROLL_KEY"])) {
  Write-Host ($initOut -join "`n") -ForegroundColor DarkGray
  Die "the relay would not provision. See the output above."
}

$adminUser = $cfg["ADMIN_USER"]
$adminPass = $cfg["ADMIN_PASSWORD"]
$enrollKey = $cfg["ENROLL_KEY"]

if ([string]::IsNullOrWhiteSpace($adminPass)) {
  $adminPass = "(unchanged from your last run)"
  Info "admin account already existed - password left alone"
  Info "use -ResetPassword if you have lost it"
} else {
  Ok "admin account ready"
}
Info "enrollment key: $enrollKey"

# =====================================================================
Step "Starting the relay"

function Test-Relay([int]$p) {
  try {
    $r = Invoke-RestMethod -Uri "http://127.0.0.1:$p/healthz" -TimeoutSec 3
    if ($r.ok) { return $r }
  } catch { }
  return $null
}

function Test-PortFree([int]$p) {
  try {
    $listening = Get-NetTCPConnection -LocalPort $p -State Listen -ErrorAction SilentlyContinue
    if ($listening) { return $false }
    return $true
  } catch {
    # Get-NetTCPConnection is absent on some builds - try connecting instead
    try {
      $c = New-Object Net.Sockets.TcpClient
      $c.Connect("127.0.0.1", $p)
      $c.Close()
      return $false
    } catch { return $true }
  }
}

$health = Test-Relay $Port
if (-not $health -and -not (Test-PortFree $Port)) {
  Warn "port $Port is held by something that is not AegisDesk"
  for ($p = $Port + 1; $p -le $Port + 12; $p++) {
    if (Test-PortFree $p) { $Port = $p; break }
  }
  Info "using port $Port instead"
  $health = Test-Relay $Port
}

if ($health) {
  Info "an AegisDesk relay is already serving port $Port - reusing it"
} else {
  # Start-Process joins -ArgumentList with spaces and does NOT quote, so a path
  # containing a space would arrive as two separate arguments. Pass none.
  Start-Process -FilePath "node" -ArgumentList @("server.js", "--port", "$Port") `
                -WorkingDirectory $relayDir -WindowStyle Normal
  $deadline = (Get-Date).AddSeconds(30)
  while (-not $health -and (Get-Date) -lt $deadline) {
    Start-Sleep -Milliseconds 600
    $health = Test-Relay $Port
  }
}

if (-not $health) {
  Warn "no answer on port $Port - re-running the relay in the foreground to capture the error"
  $outLog = Join-Path $env:TEMP "aegisdesk-relay.out.log"
  $errLog = Join-Path $env:TEMP "aegisdesk-relay.err.log"
  $proc = $null
  try {
    $proc = Start-Process -FilePath "node" -ArgumentList @("server.js", "--port", "$Port") `
              -WorkingDirectory $relayDir -WindowStyle Hidden -PassThru `
              -RedirectStandardOutput $outLog -RedirectStandardError $errLog
    Start-Sleep -Seconds 6
    if (-not $proc.HasExited) { try { $proc.Kill() } catch { } }
  } catch { }
  Write-Host ""
  foreach ($f in @($errLog, $outLog)) {
    if (Test-Path $f) {
      $txt = Get-Content $f -Raw -ErrorAction SilentlyContinue
      if ($txt -and $txt.Trim().Length -gt 0) {
        Write-Host "  ---- node said ----" -ForegroundColor DarkGray
        Write-Host $txt -ForegroundColor Yellow
      }
    }
  }
  Die "the relay would not start - the node output above says why. Two usual causes: the port is taken (run START.bat -Port 7500), or relay\node_modules is incomplete (delete it and run again)."
}
Ok "relay listening on http://localhost:$Port  (v$($health.version), $($health.devices) device(s) enrolled)"

# =====================================================================
Step "Configuring this PC as an agent"

$setupArgs = @()
$setupArgs += $pyArgs
$setupArgs += @("-m", "aegis_agent", "setup",
                "--relay", "ws://127.0.0.1:$Port",
                "--enroll-key", $enrollKey,
                "--name", "$env:COMPUTERNAME (this PC)")
$clearPassword = [string]::IsNullOrEmpty($DevicePassword)
if ($clearPassword) {
  # PowerShell can swallow an empty string argument to a native command, so
  # clear the password with its own subcommand instead of passing --password ""
  $setupArgs += @("--no-password-prompt")
  Info "no access password - every session will ask for consent on screen"
} else {
  $setupArgs += @("--password", $DevicePassword)
  Info "access password set to: $DevicePassword"
}

Push-Location $agentDir
& $pyExe @setupArgs 2>&1 | Out-Null
$setupRc = $LASTEXITCODE
if ($setupRc -eq 0 -and $clearPassword) {
  $clearArgs = @()
  $clearArgs += $pyArgs
  $clearArgs += @("-m", "aegis_agent", "password", "--clear")
  & $pyExe @clearArgs 2>&1 | Out-Null
}
Pop-Location
if ($setupRc -ne 0) {
  Die "agent setup failed. Run this by hand in the agent folder to see why: $pyExe -m aegis_agent status"
}
Ok "agent configured"

# =====================================================================
Step "Starting the agent"

$runArgs = @()
$runArgs += $pyArgs
$runArgs += @("-m", "aegis_agent", "run")
Start-Process -FilePath $pyExe -ArgumentList $runArgs -WorkingDirectory $agentDir -WindowStyle Normal

$online = $false
$deadline = (Get-Date).AddSeconds(40)
while (-not $online -and (Get-Date) -lt $deadline) {
  Start-Sleep -Milliseconds 800
  $h = Test-Relay $Port
  if ($h -and $h.online -ge 1) { $online = $true }
}
if ($online) {
  Ok "agent connected and showing as online"
} else {
  Warn "the agent has not connected yet - check its window for errors"
  Warn "the relay and viewer are still fine; the agent may just be slow to start"
}

# =====================================================================
Step "Opening the viewer"

$url = "http://localhost:$Port"
if (-not $NoBrowser) { Start-Process $url }

$line = "=" * 66
Write-Host ""
Write-Host "  $line" -ForegroundColor DarkGray
Write-Host "   Everything is running." -ForegroundColor Green
Write-Host ""
Write-Host "     Open:            $url" -ForegroundColor White
Write-Host "     Sign in as:      $adminUser" -ForegroundColor White
Write-Host "     Password:        $adminPass" -ForegroundColor White
if (-not [string]::IsNullOrEmpty($DevicePassword)) {
  Write-Host "     Device password:  $DevicePassword   (asked when you click Connect)" -ForegroundColor White
}
Write-Host ""
Write-Host "     Enrollment key:  $enrollKey" -ForegroundColor DarkGray
Write-Host "     (needed only when adding OTHER computers)" -ForegroundColor DarkGray
Write-Host "  $line" -ForegroundColor DarkGray
Write-Host ""
Write-Host "   Two new windows opened - the relay and the agent. Both must stay" -ForegroundColor DarkGray
Write-Host "   open while you use it. Run STOP.bat to shut everything down." -ForegroundColor DarkGray
Write-Host ""
Write-Host "   Tip: open Notepad and put it beside the browser before you connect." -ForegroundColor DarkGray
Write-Host "   You are viewing your own screen, so you will see a hall of mirrors -" -ForegroundColor DarkGray
Write-Host "   that is normal. Notepad gives you somewhere safe to test typing." -ForegroundColor DarkGray
Write-Host ""
Write-Host "  Press Enter to close this window (the relay and agent keep running)." -ForegroundColor DarkGray
Read-Host | Out-Null
