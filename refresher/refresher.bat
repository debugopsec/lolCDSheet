@echo off
rem Refresh ability ranges, skill orders and roles.
rem ONLY needed when a range changed, the popular leveling order changed, or a new champion came out.
rem Cooldowns are NOT refreshed here - those update automatically.
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
echo.
echo   Reminder: run this ONLY when a range changed, the leveling order changed,
echo   or a new champion came out. Cooldowns update by themselves after a patch.
echo.
python refresher\refresher.py %*
echo.
pause
