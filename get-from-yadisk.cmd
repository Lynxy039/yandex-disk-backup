@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0get-from-yadisk.ps1" %*
set "RESULT=%ERRORLEVEL%"
if not "%RESULT%"=="0" if not defined CI pause
exit /b %RESULT%
