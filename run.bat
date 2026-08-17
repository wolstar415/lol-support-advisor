@echo off
setlocal
set "ADVISOR_PYTHONW=%LOCALAPPDATA%\Programs\Python\Python314\pythonw.exe"
if exist "%ADVISOR_PYTHONW%" (
  start "" "%ADVISOR_PYTHONW%" "%~dp0app.py"
  exit /b
)

where pyw >nul 2>nul
if %errorlevel%==0 (
  start "" pyw -3 "%~dp0app.py"
  exit /b
)

where pythonw >nul 2>nul
if %errorlevel%==0 (
  start "" pythonw "%~dp0app.py"
  exit /b
)

echo Python 3.11 or newer is required.
pause
