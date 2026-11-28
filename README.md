# LoL Cooldown Sheet

Shows every enemy's Q/W/E/R cooldowns and ranges, tracks their level, and has a cooldown-guessing game.
Ability cooldowns update automatically with every patch.

## Easiest way: no Python

Download **`LoLCooldownSheet.exe`** from the [latest release](../../releases/latest) and run it.

## From this folder (needs Python)

1. Install Python 3 from https://www.python.org/downloads/ — tick **"Add python.exe to PATH"**.
2. Double-click **`run.bat`**.

## What's in here

| Path | What it is |
|---|---|
| `run.bat` | Starts the sheet |
| `lol_cd_sheet.py` | The whole program |
| `refresher/` | `refresher.bat` — updates ranges, skill orders and roles after a patch or a new champion |
| `tools/` | `build_exe.bat` — builds the standalone .exe with PyInstaller |

## Refreshing after a patch

Double-click **`refresher/refresher.bat`** (or `python refresher/refresher.py`) when:

- a patch changes ability ranges,
- the popular leveling order shifts,
- **a new champion comes out** — it lists any champion it couldn't add.

It rewrites the ranges, skill order and role tables inside `lol_cd_sheet.py` from the LoL wiki and u.gg.
It refuses to write if a site fails, and `--ranges-only` refreshes just the ranges.

**Cooldowns are not refreshed by it.** They come from Riot's Data Dragon and update by themselves the first
time you launch the app after a patch.
