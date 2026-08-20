@echo off
REM =====================================================================
REM  AegisDesk - double-click this to set up and start everything.
REM  Installs Node and Python if needed, starts the relay and the agent,
REM  and opens the viewer in your browser. Safe to run again any time.
REM =====================================================================
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\setup-and-run.ps1" %*
