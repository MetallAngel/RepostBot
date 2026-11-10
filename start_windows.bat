@echo off
setlocal
cd /d "%~dp0"
chcp 65001 >nul
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start_windows.ps1"
set "repostbot_exit=%ERRORLEVEL%"
if not "%repostbot_exit%"=="0" (
  echo.
  echo Startup failed. Read the message above.
  pause
)
exit /b %repostbot_exit%
