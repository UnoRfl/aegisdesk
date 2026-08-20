# Builds a single-file AegisDeskAgent.exe so the restaurant machines don't
# need Python installed at all. Run this once on a Windows box, then copy
# dist\AegisDeskAgent.exe to the other machines.
param([switch]$Console)

$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $MyInvocation.MyCommand.Path)

python -m pip install --upgrade pyinstaller --quiet
python -m pip install -r requirements.txt --quiet

$mode = if ($Console) { "--console" } else { "--windowed" }

python -m PyInstaller `
  --name AegisDeskAgent `
  --onefile $mode `
  --clean --noconfirm `
  --collect-all mss `
  --collect-all pystray `
  --collect-all PIL `
  --hidden-import websocket `
  --hidden-import cryptography.hazmat.backends.openssl `
  --hidden-import tkinter `
  --exclude-module matplotlib `
  --exclude-module pytest `
  agent-entry.py

Write-Host ""
Write-Host "  Built: dist\AegisDeskAgent.exe" -ForegroundColor Green
Write-Host "  Usage on a target machine:" -ForegroundColor DarkGray
Write-Host "    AegisDeskAgent.exe setup --relay wss://your-relay --enroll-key <key>" -ForegroundColor DarkGray
Write-Host "    AegisDeskAgent.exe install" -ForegroundColor DarkGray
Write-Host "    AegisDeskAgent.exe run" -ForegroundColor DarkGray
Write-Host ""
