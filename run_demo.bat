@echo off
setlocal
set "ADVISOR_PYTHON=%LOCALAPPDATA%\Programs\Python\Python314\python.exe"
if exist "%ADVISOR_PYTHON%" (
  "%ADVISOR_PYTHON%" "%~dp0app.py" --demo
  exit /b
)
where py >nul 2>nul
if %errorlevel%==0 (
  py -3 "%~dp0app.py" --demo
  exit /b
)
where python >nul 2>nul
if %errorlevel%==0 (
  python "%~dp0app.py" --demo
  exit /b
)
echo Python 3.11 or newer is required.
pause
