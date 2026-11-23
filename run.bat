@echo off
rem Double-click to start the LoL Cooldown Sheet (no console window).
cd /d "%~dp0"
start "" pythonw lol_cd_sheet.py %*
