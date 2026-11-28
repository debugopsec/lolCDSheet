@echo off
rem Refresh ability ranges, skill orders and roles (not cooldowns - those update automatically).
cd /d "%~dp0.."
where python >nul 2>nul
if errorlevel 1 (
    echo.
    echo   Python 3 is required but was not found.
    echo   Install it from https://www.python.org/downloads/ and tick
    echo   "Add python.exe to PATH", then run this file again.
    echo.
    pause
    exit /b 1
)
python refresher\refresher.py %*
echo.
pause
