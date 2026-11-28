# LoL Cooldown Sheet

Shows every enemy's Q/W/E/R cooldowns and ranges, tracks their level, and has a cooldown-guessing game.
Ability cooldowns update automatically with every patch (Data Dragon / CommunityDragon).

## Install (once)

1. Install Python 3: https://www.python.org/downloads/ — tick **"Add python.exe to PATH"** during setup.
2. Download this repo: green **Code** button → **Download ZIP**, then unpack it anywhere.

## Run

Double-click **`run.bat`**. It checks that Python is there and opens the sheet.

## Refresh ranges / skill orders / roles

`refresher.py` (or double-click **`refresher.bat`**) re-scrapes the LoL wiki and u.gg and rewrites the
static tables inside `lol_cd_sheet.py`:

- ability ranges
- the most popular skill order and primary role per champion

Run it when a patch changes ranges, when the popular leveling order shifts, or **when a new champion comes
out** — it lists any champion it couldn't add.

**This does NOT refresh cooldowns.** Cooldowns come from Riot's Data Dragon and update by themselves the
first time you launch the app after a patch. Nothing to do there.

## No Python?

The app itself is one Python file. If you want to hand it to someone without Python, build a single exe
once: `build_exe.bat` (needs Python, installs PyInstaller, produces `dist\LoLCooldownSheet.exe`). The
refresher still needs Python, but it's only for you.
