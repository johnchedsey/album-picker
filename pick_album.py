#!/usr/bin/env python3
"""
Album picker — randomly selects an album from albums.csv,
removes it from the file, and posts to Discord.

Usage: python3 pick_album.py
"""

import csv
import random
import urllib.request
import urllib.error
import json
import os
import sys
import time
from datetime import datetime

from envfile import WEBHOOK_KEY, get_env

# --- Config ---
CSV_PATH = "albums.csv"  # path to your CSV file
WEBHOOK_URL = get_env(WEBHOOK_KEY)  # from .env next to this script, or the environment
MAX_RETRIES = 5

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

# --- Pick a random album ---
idx = random.randint(0, len(data) - 1)
picked = data.pop(idx)
artist, album, date = picked

# --- Display name ("Barbarians of California, the" -> "The Barbarians of California") ---
def display_artist(name):
    """Folder name -> display name: "Barbarians of California, the" -> "The Barbarians of California"."""
    if name.lower().endswith(", the"):
        return "The " + name[:-5].rstrip()
    return name


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
message = f"💿 Up Next: **{display_artist(artist)}** — *{album}*"
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