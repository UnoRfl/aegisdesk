@echo off
REM Restarts with NO access password, so every connection asks permission
REM on screen. This is the mode you want on a staff laptop -- and it is
REM worth seeing once so you know what the other person sees.
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\setup-and-run.ps1" -DevicePassword "" -SkipDeps
