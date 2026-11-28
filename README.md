# LoL Cooldown Sheet

Shows every enemy's Q/W/E/R cooldowns and ranges, tracks their level, and has a cooldown-guessing game.

---

> # ⚠️ READ THIS BEFORE TOUCHING THE REFRESHER
>
> **Ability cooldowns update by themselves after every patch. You never need the refresher for a patch.**
>
> Run `refresher/refresher.bat` **ONLY** when one of these three things happens:
>
> 1. **A range changed.** A number in the `↔` column looks wrong after a patch.
> 2. **The leveling order changed.** The `now` cooldowns look wrong for a champion because players
>    started maxing a different ability.
> 3. **A new champion came out.**
>
> That's it. If none of those happened, **do not run it.** It takes 5–10 minutes, hits two websites, and
> changes nothing useful.
>
> It does **not** touch cooldowns. Ever. Those come from Riot's Data Dragon and are already current.

---

## Easiest way: no Python

Download **`LoLCooldownSheet.exe`** from the [latest release](../../releases/latest) and double-click it.

Windows will show a **SmartScreen warning** ("Windows protected your PC") the first time, because the exe
is unsigned. Click **More info → Run anyway**.

**Don't want that warning? Use the zip instead.** Running through Python never triggers SmartScreen — it
just needs Python installed (below).

## From this folder (needs Python)

1. Install Python 3 from https://www.python.org/downloads/ — tick **"Add python.exe to PATH"**.
2. Double-click **`run.bat`**.

## What's in here

| Path | What it is | Do you need it? |
|---|---|---|
| `run.bat` | Starts the sheet | **Yes, every time** |
| `lol_cd_sheet.py` | The whole program | Don't edit by hand |
| `refresher/` | Updates ranges, skill orders and roles | **Only for the 3 cases above** |
| `tools/` | Builds the standalone .exe | Only if you want to rebuild the exe |

## Running the refresher (only when needed)

Double-click **`refresher/refresher.bat`** (or `python refresher/refresher.py`).

It rewrites the ranges, skill order and role tables inside `lol_cd_sheet.py`, scraped from the LoL wiki
and u.gg. It prints any champion it couldn't add, and refuses to write if a site fails.

`--ranges-only` refreshes just the ranges, for when only case 1 happened.

**Again: it does not refresh cooldowns.** There is nothing to refresh — they're pulled from Riot's
Data Dragon and update automatically the first time you launch the app after a patch.
