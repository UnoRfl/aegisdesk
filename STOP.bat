@echo off
REM Shuts down the AegisDesk relay and agent started by START.bat.
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\stop.ps1"
