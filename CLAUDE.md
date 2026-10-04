# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

Two standalone Python 3 scripts (stdlib only, no dependencies, no tests, no build step) that together form an "album of the day" picker for a music library laid out as `<root>/<Artist>/<Album>/`.

## Commands

```bash
# 1. Build the queue: scan a music library into albums.csv
python3 albumfinder.py [start_path] [output_file]   # defaults: cwd, ./albums.csv

# 2. Pick one album at random, remove it from albums.csv, post it to Discord
python3 pick_album.py
```

## How the pieces fit together

- `albumfinder.py` walks exactly two directory levels (artist → album) and writes `albums.csv` with columns `First_Level_Directory, Second_Level_Directory, Created_Date` (date from `st_ctime`, format `%Y-%m-%d %H:%M:%S`). Running it again overwrites the CSV, which refills the queue.
- `pick_album.py` treats `albums.csv` as a consumable queue: it reads it (relative to the **current working directory**), unpacks each row positionally as `artist, album, date`, pops one at random, and **rewrites the file in place** without that row. It then posts the pick to a Discord webhook, retrying on HTTP 429 using `retry_after`.
- The CSV is the contract between the two scripts. Changing the columns in `albumfinder.py` (e.g. enabling the commented-out `Modified_Date`/`Full_Path` fields) will break the 3-way unpack in `pick_album.py`, and `time_since()` relies on the exact date format.
- `pick_album.py` is a top-level script (no `main()`), so importing it runs it and mutates `albums.csv`. Back up the CSV before experimenting.
- Config (`CSV_PATH`, `MAX_RETRIES`) is set as constants at the top of `pick_album.py`. The Discord webhook URL is a secret: it lives only in a git-ignored `.env` next to the scripts (`DISCORD_WEBHOOK_URL=...`, template in `.env.example`), read via `envfile.py`; a real environment variable of the same name overrides it. Never hard-code the URL or put it in any committed file.

## Windows GUI (`album_picker_gui.pyw`)

Build a single-file `AlbumPicker.exe` with `python build_exe.py` (needs `pip install pyinstaller`; uses `album_picker.ico`). The exe is copied to the project root and looks for `.env` and `album_picker_config.json` beside itself, so it must stay next to them. Rebuild after any code change.

Stdlib tkinter app combining both scripts. Settings (library folder, queue CSV path, Discord on/off) live in the git-ignored `album_picker_config.json` next to the script; the webhook field in the Settings dialog reads and writes `.env` instead. It reads/writes the queue CSV in the same format, so the CLI scripts stay compatible. Differences from the CLI flow:
- Every pick (and every album the user declines during an update) is appended to `played.csv` next to the queue CSV, with columns `Artist, Album, Created_Date, Picked_Date, Status` (`played`/`skipped`).
- "Refresh Library" never rebuilds the queue: it offers to add albums that are in neither the queue nor `played.csv`, and to remove queued albums whose folders are gone (`compute_changes()`). Albums under unreadable artist folders are never treated as deleted, and a scan that finds zero albums is aborted so an unplugged drive can't empty the queue. Picks made with `pick_album.py` are not logged to `played.csv`, so those albums can come back on the next update.
