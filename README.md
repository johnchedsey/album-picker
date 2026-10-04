# Album Picker

Picks an "album of the day" at random from your folder of downloaded, not-yet-listened-to albums and, if you like, posts it to a Discord channel. Albums are never picked twice: once you've listened to one, you move it into your main library and it's gone from the queue for good.

## The story

If you are like me, you have plundered Bandcamp and perhaps even downloaded some obscure shoegazer EPs from a special source and now have hundreds or perhaps thousands of unlistened albums in your download folder. My download folder got so unweidly that I needed a tool to help me pick what to play next and decided to whip up a pair of python scripts to scan my Download folder for albums and then a second one to randomly pick one from the CSV created by the previous script. Eventually this turned into a launchable Windows exe file to harmonize the scripts into a single GUI, rather than running scripts from a terminal window.

The Discord feature is if you want to spam your music nerd friends in a Discord channel. 

## How it works

Point Album Picker at your downloads (or "incoming") folder, laid out as `<incoming>/<Artist>/<Album>/`. It scans those folders into a queue (`albums.csv`). This does require you to organize your Downloads folder into a structured format. 

Each pick removes that album from the queue for good, and it's never picked again. The idea is that after you've listened to the day's album, you move its folder out of the incoming folder and into your main music library. Album Picker doesn't move anything itself; that step is yours.

The queue only ever holds albums you haven't heard yet. Picks are also logged to `played.csv`, so an album you picked but haven't moved yet still won't come back when you refresh.

## Requirements

- Python 3 (standard library only; nothing to `pip install`)
- Windows for the GUI and the `.exe`. The command-line scripts run anywhere Python does.
- A Discord webhook URL, if you want picks posted to Discord (optional)

## Setup

1. Clone the repo:
   ```bash
   git clone https://github.com/<your-username>/album-picker.git
   cd album-picker
   ```
2. *(Optional, for Discord)* Copy `.env.example` to `.env` and paste in your webhook URL:
   ```
   DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...
   ```
   `.env` is git-ignored, so keep the URL there and never in the code. A real `DISCORD_WEBHOOK_URL` environment variable overrides the file.

## Usage

### Windows app

```bash
python album_picker_gui.pyw
```

The first time it runs, the Settings window opens so you can choose your incoming folder (the app calls it "Music library folder"), where to keep the queue CSV, and whether to post to Discord.

- **🎲 Random Pick!:** picks an album, removes it from the queue, and posts it to Discord if that's turned on.
- **🔍 Manually pick:** search the queue by artist folder name and choose the next album yourself (Select, or double-click). It's handled like a random pick.
- **🔄 Refresh Library:** finds albums added since the last scan and offers to queue them. It also offers to drop queued albums whose folders are gone. Albums you've already played don't come back.
- **Recent picks:** your latest picks, read from `played.csv`.

Every pick is logged to `played.csv` beside the queue. If you turn down a new album during a refresh, it's logged there as `skipped` so it isn't offered again.

### Command line

```bash
# Scan a library into albums.csv (overwrites it, which refills the queue)
python albumfinder.py "D:\Incoming" albums.csv

# Pick one album, remove it from albums.csv, and post it to Discord
python pick_album.py
```

`pick_album.py` reads `albums.csv` from the current directory. It doesn't write to `played.csv`, so albums picked this way can be offered again the next time the GUI refreshes.

## Building the .exe

```bash
pip install pyinstaller
python build_exe.py
```

This builds `AlbumPicker.exe` in the project folder. The exe looks for `.env` and `album_picker_config.json` in the folder it runs from, so keep it next to them.

## License

[MIT](LICENSE)
