@echo off
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0rebuild-and-restart.ps1" %*
echo.
pause
