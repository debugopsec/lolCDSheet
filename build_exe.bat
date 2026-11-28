@echo off
rem Optional: build a single LoLCooldownSheet.exe that runs without Python.
rem Needs Python once; the .exe itself needs nothing.
cd /d "%~dp0"
where python >nul 2>nul
if errorlevel 1 (
    echo.
    echo   Python 3 is required to BUILD the exe.
    echo   Install it from https://www.python.org/downloads/ and tick
    echo   "Add python.exe to PATH", then run this file again.
    echo.
    pause
    exit /b 1
)
python -m pip install --upgrade --quiet pyinstaller || goto :err
python -m PyInstaller --noconfirm --onefile --windowed --name LoLCooldownSheet lol_cd_sheet.py || goto :err
echo.
echo Built: dist\LoLCooldownSheet.exe  (copy it anywhere; it keeps its cache next to itself)
pause
exit /b 0
:err
echo.
echo Build failed.
pause
exit /b 1
