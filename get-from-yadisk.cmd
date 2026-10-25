@echo off
setlocal
set "PYTHON=%~dp0.venv\Scripts\python.exe"
if not exist "%PYTHON%" set "PYTHON=python"
"%PYTHON%" -c "import textual" >nul 2>&1
if errorlevel 1 (
  echo Install dependencies first:
  echo   python -m venv .venv
  echo   .venv\Scripts\python.exe -m pip install -r requirements.txt
  exit /b 1
)
"%PYTHON%" "%~dp0tui.py" %*
exit /b %ERRORLEVEL%
