#!/usr/bin/env python3
"""
Album picker — randomly selects an album from albums.csv,
removes it from the file, and posts to Discord.

Usage: python3 pick_album.py
"""

import csv
import random
import re
import urllib.request
import urllib.error
import json
import os
import sys
import time
from datetime import datetime

from envfile import WEBHOOK_KEY, get_env
from version import __version__

if "--version" in sys.argv[1:]:
    print(f"Album Picker v{__version__}")
    sys.exit(0)

# --- Config ---
CSV_PATH = "albums.csv"  # path to your CSV file
WEBHOOK_URL = get_env(WEBHOOK_KEY)  # from .env next to this script, or the environment
MAX_RETRIES = 5
# "album": any queued album, all equally likely.
# "artist": a random artist (each equally likely), then their oldest queued album by Created_Date.
# "artist_newest": the same, but their newest queued album.
PICK_MODE = "album"
# Options as in the GUI's Settings → "Random pick mode". "artist"/"artist_newest" modes only:
ARTIST_WEIGHT = "equal"     # "equal", "count" (by number of queued albums) or "sqrt" (gentler)
ARTIST_FAVOR_WAIT = False  # favor artists whose oldest queued album has waited longest
# Both modes:
ARTIST_SKIP_RECENT = 0      # skip artists of the last N picks in played.csv beside CSV_PATH (0 = off;
                            # only the GUI writes played.csv, so CLI picks aren't counted)

if not WEBHOOK_URL:
    print(f"Error: {WEBHOOK_KEY} is not set. Add it to .env (see .env.example).")
    sys.exit(1)

# --- Load CSV ---
if not os.path.exists(CSV_PATH):
    print(f"Error: {CSV_PATH} not found.")
    sys.exit(1)

with open(CSV_PATH, newline='', encoding='utf-8-sig') as f:
    rows = list(csv.reader(f))

header = rows[0]
data = rows[1:]

if not data:
    print("No albums left!")
    sys.exit(0)

# --- Pick an album ---
DATE_FMT = "%Y-%m-%d %H:%M:%S"


def recent_artists(history, count):
    """Artist folders of the last `count` played picks in played.csv rows (skipped albums don't count)."""
    if count <= 0:
        return set()
    played = [r[0] for r in history if len(r) > 4 and r[4] == "played"]
    return set(played[-count:])


def random_album(rows, skip=frozenset()):
    """Index of any row at random, leaving out artists in skip unless that would leave none.

    Duplicated in album_picker_gui.pyw — keep in sync.
    """
    pool = [i for i, r in enumerate(rows) if r[0] not in skip] or list(range(len(rows)))
    return random.choice(pool)


def random_artist_album(rows, weight="equal", favor_wait=False, skip=frozenset(), newest=False):
    """Index of the oldest (or, with newest, the newest) album by Created_Date of a random
    artist; ties broken at random.

    weight: "equal" (every artist the same), "count" (in proportion to queued albums) or
    "sqrt" (square root of queued albums). favor_wait multiplies that by the days the
    artist's oldest queued album has waited. Artists in skip are left out, unless that
    would leave none. Duplicated in album_picker_gui.pyw — keep in sync.
    """
    groups = {}
    for i, r in enumerate(rows):
        groups.setdefault(r[0], []).append(i)
    pool = sorted(a for a in groups if a not in skip) or sorted(groups)
    now = datetime.now()
    weights = []
    for artist in pool:
        n = len(groups[artist])
        w = {"count": n, "sqrt": n ** 0.5}.get(weight, 1.0)
        if favor_wait:
            oldest = min(rows[i][2] for i in groups[artist])
            try:
                w *= max((now - datetime.strptime(oldest, DATE_FMT)).total_seconds() / 86400, 0) + 1
            except ValueError:
                pass  # unreadable date: leave the weight as it is
        weights.append(w)
    mine = groups[random.choices(pool, weights)[0]]
    target = (max if newest else min)(rows[i][2] for i in mine)  # DATE_FMT sorts correctly as text
    return random.choice([i for i in mine if rows[i][2] == target])


if PICK_MODE not in ("album", "artist", "artist_newest"):
    print(f'Error: PICK_MODE must be "album", "artist" or "artist_newest", not {PICK_MODE!r}.')
    sys.exit(1)
if ARTIST_WEIGHT not in ("equal", "count", "sqrt"):
    print(f'Error: ARTIST_WEIGHT must be "equal", "count" or "sqrt", not {ARTIST_WEIGHT!r}.')
    sys.exit(1)
skip = set()
history_file = os.path.join(os.path.dirname(CSV_PATH), "played.csv")
if ARTIST_SKIP_RECENT > 0 and os.path.exists(history_file):
    with open(history_file, newline='', encoding='utf-8-sig') as f:
        skip = recent_artists(list(csv.reader(f))[1:], ARTIST_SKIP_RECENT)
if PICK_MODE in ("artist", "artist_newest"):
    idx = random_artist_album(data, ARTIST_WEIGHT, ARTIST_FAVOR_WAIT, skip, newest=PICK_MODE == "artist_newest")
else:
    idx = random_album(data, skip)
picked = data.pop(idx)
artist, album, date = picked

# --- Display name ("Barbarians of California, the" -> "The Barbarians of California") ---
def display_artist(name):
    """Folder name -> display name: "Barbarians of California, the" -> "The Barbarians of California"."""
    if name.lower().endswith(", the"):
        return "The " + name[:-5].rstrip()
    return name


def display_album(name):
    """Folder name -> Discord name: drop "[FLAC]" / "{WEB - FLAC}" tags ("Abbey Road [FLAC]" -> "Abbey Road")."""
    return re.sub(r"\s{2,}", " ", re.sub(r"\[flac\]|\{web\s*-\s*flac\}", "", name, flags=re.IGNORECASE)).strip() or name


# --- Calculate time since added ---
def time_since(date_str):
    added = datetime.strptime(date_str, "%Y-%m-%d %H:%M:%S")
    now = datetime.now()
    years = now.year - added.year
    months = now.month - added.month
    days = now.day - added.day
    if days < 0:
        months -= 1
        from calendar import monthrange
        prev_month = (now.month - 1) or 12
        prev_year = now.year if now.month > 1 else now.year - 1
        days += monthrange(prev_year, prev_month)[1]
    if months < 0:
        years -= 1
        months += 12
    parts = []
    if years:
        parts.append(f"{years} year{'s' if years != 1 else ''}")
    if months:
        parts.append(f"{months} month{'s' if months != 1 else ''}")
    if days or not parts:
        parts.append(f"{days} day{'s' if days != 1 else ''}")
    return ", ".join(parts)

elapsed = time_since(date)

print(f"\n💿 Selected: {display_artist(artist)} — {album}")
print(f"   Added: {date} ({elapsed} ago)\n")

# --- Write updated CSV back ---
with open(CSV_PATH, 'w', newline='', encoding='utf-8') as f:
    writer = csv.writer(f, quoting=csv.QUOTE_ALL)
    writer.writerow(header)
    writer.writerows(data)

print(f"{len(data)} albums remaining in queue.")

# --- Post to Discord (with retry on 429) ---
message = f"💿 Up Next: **{display_artist(artist)}** — *{display_album(album)}*"
payload = json.dumps({"content": message}).encode("utf-8")

for attempt in range(1, MAX_RETRIES + 1):
    req = urllib.request.Request(
        WEBHOOK_URL,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "AlbumPickerScript/1.0"
        },
        method="POST"
    )
    try:
        with urllib.request.urlopen(req) as res:
            print(f"Posted to Discord (status {res.status})")
            break
    except urllib.error.HTTPError as e:
        if e.code == 429:
            try:
                error_body = json.loads(e.read().decode("utf-8"))
                retry_after = float(error_body.get("retry_after", 5))
            except Exception:
                retry_after = 5
            print(f"Rate limited by Discord. Retrying in {retry_after:.1f}s (attempt {attempt}/{MAX_RETRIES})...")
            time.sleep(retry_after)
        else:
            print(f"Discord post failed: {e}")
            break
    except Exception as e:
        print(f"Discord post failed: {e}")
        break
else:
    print("Failed to post to Discord after maximum retries.")