@echo off
REM run.bat - NINA's entry point. Sets up .\venv the first time (and again
REM whenever requirements.txt changes), then starts nina.py: the fleet watcher
REM and her page on http://127.0.0.1:5012/
setlocal
cd /d "%~dp0"
set "VENV=%~dp0venv"
set "PY=%VENV%\Scripts\python.exe"

if not exist "%PY%" (
    echo Creating venv ...
    py -3 -m venv "%VENV%" 2>nul || python -m venv "%VENV%"
)
if not exist "%PY%" goto :nopython

fc /b requirements.txt "%VENV%\requirements.installed" >nul 2>&1
if errorlevel 1 (
    echo Installing requirements ...
    "%PY%" -m pip install -r requirements.txt || goto :fail
    copy /y requirements.txt "%VENV%\requirements.installed" >nul
)

"%PY%" nina.py %*
if errorlevel 1 pause
exit /b

:nopython
echo Python was not found. Install it from https://www.python.org/downloads/
pause
exit /b 1

:fail
echo Setup failed - see the errors above.
pause
exit /b 1
