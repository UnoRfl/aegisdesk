<#
.SYNOPSIS
  Builds the file you send to people who need help.

.DESCRIPTION
  Reads the enrollment key out of your own relay, asks for your relay's public
  address, and produces dist\AegisDesk-Support.exe with both compiled in.

  Whoever receives that file double-clicks it and reads you two numbers. They
  install nothing, need no admin rights, and nothing is left on their computer.
#>
param(
  [string]$Relay = "",
  [string]$Brand = "",
  [string]$Phone = ""
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$relayDir = Join-Path $root "relay"
$agentDir = Join-Path $root "agent"

function Say($m, $c = "Cyan") { Write-Host "  $m" -ForegroundColor $c }
function Die($m) {
  Write-Host ""; Write-Host "  STOPPED: $m" -ForegroundColor Red; Write-Host ""
  Write-Host "  Press Enter to close." -ForegroundColor DarkGray; Read-Host | Out-Null; exit 1
}

Write-Host ""
Write-Host "  Build the support tool" -ForegroundColor White
Write-Host "  ----------------------" -ForegroundColor DarkGray
Write-Host ""

# ---- enrollment key straight from your relay ----
Push-Location $relayDir
$initOut = & node "server.js" "--init" 2>&1
Pop-Location
$enroll = ""
foreach ($line in $initOut) { if ("$line" -match '^AEGIS_ENROLL_KEY=(.*)$') { $enroll = $Matches[1] } }
if (-not $enroll) {
  Write-Host ($initOut -join "`n") -ForegroundColor DarkGray
  Die "could not read the enrollment key from your relay. Run START.bat once first."
}
Say "enrollment key read from your relay"

# ---- the address people will connect through ----
if (-not $Relay) {
  $guess = ""
  try {
    $ts = & tailscale status --json 2>$null | ConvertFrom-Json
    if ($ts -and $ts.Self -and $ts.Self.DNSName) { $guess = "wss://" + $ts.Self.DNSName.TrimEnd('.') }
  } catch { }

  Write-Host ""
  Say "What address will people reach your relay on?" "White"
  Say "This must work from OUTSIDE your own PC. localhost will not do." "DarkGray"
  if ($guess) { Say "Your Tailscale address looks like: $guess" "DarkGray" }
  Say "If you have not set that up yet, close this and see QUICKSTART.md Part 2." "DarkGray"
  Write-Host ""
  $prompt = "  Relay address"
  if ($guess) { $prompt += " [$guess]" }
  $Relay = Read-Host $prompt
  if (-not $Relay -and $guess) { $Relay = $guess }
}
if (-not $Relay) { Die "no relay address given." }
if ($Relay -match 'localhost|127\.0\.0\.1') {
  Die "that address only works on this PC, so the tool would be useless to anyone else. Set up Tailscale first (QUICKSTART.md Part 2)."
}

if (-not $Brand) {
  $Brand = Read-Host "  Heading to show in the window [Remote Support]"
  if (-not $Brand) { $Brand = "Remote Support" }
}
if (-not $Phone) { $Phone = Read-Host "  Phone number to show, or blank for none" }

Write-Host ""
Say "building - this takes a few minutes the first time"
Write-Host ""

$buildArgs = @(
  "-ExecutionPolicy", "Bypass", "-File", (Join-Path $agentDir "build-support-exe.ps1"),
  "-Relay", $Relay, "-EnrollKey", $enroll, "-Brand", $Brand
)
if ($Phone) { $buildArgs += @("-Phone", $Phone) }
& powershell @buildArgs

$exe = Join-Path $agentDir "dist\AegisDesk-Support.exe"
Write-Host ""
if (Test-Path $exe) {
  Say "Done: agent\dist\AegisDesk-Support.exe" "Green"
  Write-Host ""
  Say "Give that file to anyone who needs help. Email it, put it on a shared" "DarkGray"
  Say "drive, or publish it on GitHub Releases so they can fetch it themselves." "DarkGray"
  Write-Host ""
  Say "Keep it reasonably private: it lets a machine offer its screen to your" "Yellow"
  Say "relay. It does NOT let anyone view screens - that needs an operator login." "Yellow"
} else {
  Say "the build did not produce an .exe - see the output above" "Red"
}
Write-Host ""
Write-Host "  Press Enter to close." -ForegroundColor DarkGray
Read-Host | Out-Null
