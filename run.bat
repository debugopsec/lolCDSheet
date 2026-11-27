@echo off
rem Double-click to start the LoL Cooldown Sheet. Needs Python 3 (python.org installer).
cd /d "%~dp0"
where pythonw >nul 2>nul
if errorlevel 1 (
    echo.
    echo   Python 3 is required but was not found.
    echo   Install it from https://www.python.org/downloads/ and tick
    echo   "Add python.exe to PATH", then run this file again.
    echo.
    pause
    exit /b 1
)
start "" pythonw lol_cd_sheet.py %*
