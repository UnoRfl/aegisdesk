@echo off
REM Builds AegisDesk-Support.exe -- the single file you send to someone who
REM needs help. They double-click it and read you two numbers. Nothing is
REM installed on their computer.
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\build-support-tool.ps1" %*
