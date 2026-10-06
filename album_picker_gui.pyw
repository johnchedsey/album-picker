"""
Album Picker GUI - Windows front end for albumfinder.py + pick_album.py.

  * Refresh Library: scans <library>/<Artist>/<Album>/, offers to add albums
    that are not already queued and have not been picked before, and offers to
    remove queued albums whose folders have been deleted.
  * Random Pick!: pops a random album from the queue CSV, logs it to the
    history file, and posts it to Discord.

The queue CSV keeps the same format albumfinder.py writes, so pick_album.py
still works against it. Picks are logged to played.csv next to the queue.

Usage: double-click album_picker_gui.pyw (or: pythonw album_picker_gui.pyw)
"""

import calendar
import csv
import json
import os
import random
import subprocess
import sys
import threading
import time
import tkinter as tk
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, font, messagebox, ttk

from audiolength import AUDIO_EXTS, album_length, format_length
from envfile import WEBHOOK_KEY, get_env, set_env

APP_DIR = (
    Path(sys.executable).parent if getattr(sys, "frozen", False)
    else Path(__file__).resolve().parent
)
CONFIG_PATH = APP_DIR / "album_picker_config.json"
QUEUE_HEADER = ["First_Level_Directory", "Second_Level_Directory", "Created_Date"]
HISTORY_HEADER = ["Artist", "Album", "Created_Date", "Picked_Date", "Status"]
DATE_FMT = "%Y-%m-%d %H:%M:%S"  # stored in the CSVs; pick_album.py parses this exact format
MAX_RETRIES = 5
RECENT_COUNT = 25

# Ratio of the screen's DPI to the classic 96 DPI; set in main() once Tk is up.
SCALE = 1.0


def px(n: float) -> int:
    """Scale a layout size given in 96-DPI pixels to the current display."""
    return round(n * SCALE)


DEFAULT_CONFIG = {
    "library_dir": "",
    "csv_path": str(APP_DIR / "albums.csv"),
    "webhook_url": "",
    "post_to_discord": True,
    "window_size": "",  # "WxH" in screen pixels, remembered between runs
    "window_zoomed": False,
    "recent_columns": {},  # Recent picks column widths in screen pixels, by column id
}


# --- Config ---

def load_config() -> dict:
    """Settings come from the JSON config; the webhook URL (a secret) comes from .env."""
    config = dict(DEFAULT_CONFIG)
    saved = {}
    if CONFIG_PATH.exists():
        try:
            saved = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
    config.update(saved)
    # Older configs stored the webhook in the JSON; move it into .env.
    legacy_url = saved.get("webhook_url", "")
    if legacy_url and not get_env(WEBHOOK_KEY):
        set_env(WEBHOOK_KEY, legacy_url)
    if "webhook_url" in saved:
        save_config(config)
    config["webhook_url"] = get_env(WEBHOOK_KEY) or legacy_url
    return config


def save_config(config: dict) -> None:
    public = {k: v for k, v in config.items() if k != "webhook_url"}
    CONFIG_PATH.write_text(json.dumps(public, indent=2), encoding="utf-8")
    if config.get("webhook_url", "") != get_env(WEBHOOK_KEY):
        set_env(WEBHOOK_KEY, config.get("webhook_url", ""))


# --- CSV files ---

def history_path(csv_path: Path) -> Path:
    return csv_path.with_name("played.csv")


def read_rows(path: Path) -> list[list[str]]:
    """Return data rows (header skipped) as lists, or [] if the file is missing."""
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8-sig") as f:
        rows = [r for r in csv.reader(f) if r]
    return rows[1:]


def write_queue(path: Path, rows: list[list[str]]) -> None:
    # Write to a temp file first so a crash can't leave a half-written queue.
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f, quoting=csv.QUOTE_ALL)
        writer.writerow(QUEUE_HEADER)
        writer.writerows(rows)
    tmp.replace(path)


def append_history(path: Path, rows: list[list[str]]) -> None:
    new_file = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f, quoting=csv.QUOTE_ALL)
        if new_file:
            writer.writerow(HISTORY_HEADER)
        writer.writerows(rows)


def album_key(row: list[str]) -> tuple[str, str]:
    return (row[0], row[1])


def compute_changes(found, unreadable, queue, history):
    """Compare a library scan with the queue and history.

    Returns (new, missing): albums on disk that are neither queued nor in the
    history (newest first), and queued albums whose folder is gone. Albums under
    an artist folder that couldn't be read are never reported as missing.
    """
    on_disk = {album_key(r) for r in found}
    known = {album_key(r) for r in queue} | {album_key(r) for r in history}
    new = sorted((r for r in found if album_key(r) not in known), key=lambda r: r[2], reverse=True)
    missing = [r for r in queue if album_key(r) not in on_disk and r[0] not in unreadable]
    return new, missing


# --- Library scan (same rules as albumfinder.py) ---

def scan_library(root: Path) -> tuple[list[list[str]], set[str]]:
    """Return (albums found, names of artist folders that couldn't be read)."""
    results = []
    unreadable = set()
    for artist in sorted((d for d in root.iterdir() if d.is_dir()), key=lambda d: d.name.lower()):
        try:
            albums = sorted((d for d in artist.iterdir() if d.is_dir()), key=lambda d: d.name.lower())
        except OSError:
            unreadable.add(artist.name)
            continue
        for album in albums:
            created = datetime.fromtimestamp(album.stat().st_ctime)
            results.append([artist.name, album.name, created.strftime(DATE_FMT)])
    return results, unreadable


def folder_size(path: Path) -> int:
    """Total size in bytes of the files under path (scandir reuses directory listings, so this is quick)."""
    total, pending = 0, [path]
    while pending:
        with os.scandir(pending.pop()) as entries:
            for entry in entries:
                if entry.is_dir(follow_symlinks=False):
                    pending.append(entry.path)
                elif entry.is_file(follow_symlinks=False):
                    total += entry.stat(follow_symlinks=False).st_size
    return total


def audio_check(path: Path) -> str | None:
    """None if the folder holds at least one audio file, else a short note on what it does hold."""
    files = size = 0
    pending = [path]
    while pending:
        with os.scandir(pending.pop()) as entries:
            for entry in entries:
                if entry.is_dir(follow_symlinks=False):
                    pending.append(entry.path)
                elif entry.is_file(follow_symlinks=False):
                    if os.path.splitext(entry.name)[1].lower() in AUDIO_EXTS and not entry.name.startswith("._"):
                        return None
                    files += 1
                    size += entry.stat(follow_symlinks=False).st_size
    if not files:
        return "Empty folder"
    return f"{files} file{'s' if files != 1 else ''}, {format_size(size)}, no audio"


def find_albums_without_audio(root: Path, albums: list[list[str]]) -> list[tuple[list[str], str]]:
    """(album row, note) for each album folder that contains no audio files."""
    results = []
    for row in albums:
        try:
            note = audio_check(root / row[0] / row[1])
        except OSError:
            continue  # unreadable or just deleted; not our concern here
        if note:
            results.append((row, note))
    return results


def format_size(size: int) -> str:
    if size >= 1024 ** 3:
        return f"{size / 1024 ** 3:.2f} GB"
    return f"{size / 1024 ** 2:.1f} MB"


# --- Pick helpers (from pick_album.py) ---

def short_time(date_str: str) -> str:
    """Display form of a stored timestamp: drop the seconds (YYYY-MM-DD HH:MM)."""
    return date_str[:16]


def display_artist(name: str) -> str:
    """Folder name -> display name: "Barbarians of California, the" -> "The Barbarians of California"."""
    if name.lower().endswith(", the"):
        return "The " + name[:-5].rstrip()
    return name


def time_since(date_str: str) -> str:
    added = datetime.strptime(date_str, DATE_FMT)
    now = datetime.now()
    years = now.year - added.year
    months = now.month - added.month
    days = now.day - added.day
    if days < 0:
        months -= 1
        prev_month = (now.month - 1) or 12
        prev_year = now.year if now.month > 1 else now.year - 1
        days += calendar.monthrange(prev_year, prev_month)[1]
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


def post_to_discord(webhook_url: str, message: str) -> str:
    """Post a message, retrying on HTTP 429. Returns a status string."""
    payload = json.dumps({"content": message}).encode("utf-8")
    for _ in range(MAX_RETRIES):
        req = urllib.request.Request(
            webhook_url,
            data=payload,
            headers={"Content-Type": "application/json", "User-Agent": "AlbumPickerScript/1.0"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as res:
                return f"Posted to Discord (status {res.status})"
        except urllib.error.HTTPError as e:
            if e.code != 429:
                return f"Discord post failed: {e}"
            try:
                retry_after = float(json.loads(e.read().decode("utf-8")).get("retry_after", 5))
            except Exception:
                retry_after = 5
            time.sleep(retry_after)
        except Exception as e:
            return f"Discord post failed: {e}"
    return "Failed to post to Discord after maximum retries."


# --- Cover art ---

COVER_NAMES = ("cover.jpg", "folder.jpg", "albumart.jpg")
COVER_SIZE = 140  # longest side, in 96-DPI pixels

_gdiplus = None
_gdiplus_lock = threading.Lock()


def _gdiplus_dll():
    """Start GDI+ once and return the DLL (Tk 8.6 can't decode JPEG on its own)."""
    global _gdiplus
    with _gdiplus_lock:
        if _gdiplus is None:
            import ctypes

            class StartupInput(ctypes.Structure):
                _fields_ = [("version", ctypes.c_uint32), ("callback", ctypes.c_void_p),
                            ("no_thread", ctypes.c_int), ("no_codecs", ctypes.c_int)]

            dll = ctypes.windll.gdiplus
            token = ctypes.c_size_t()
            if dll.GdiplusStartup(ctypes.byref(token), ctypes.byref(StartupInput(1, None, 0, 0)), None):
                raise OSError("GDI+ failed to start")
            _gdiplus = dll
    return _gdiplus


def find_cover(album_dir: Path) -> Path | None:
    for name in COVER_NAMES:
        path = album_dir / name
        if path.is_file():
            return path
    return None


def load_cover_ppm(path: Path, max_side: int) -> bytes:
    """Decode an image with GDI+, scale it to fit max_side, and return it as PPM data."""
    import ctypes

    class BitmapData(ctypes.Structure):
        _fields_ = [("width", ctypes.c_uint), ("height", ctypes.c_uint), ("stride", ctypes.c_int),
                    ("format", ctypes.c_int), ("scan0", ctypes.c_void_p), ("reserved", ctypes.c_void_p)]

    argb32 = 0x0026200A  # PixelFormat32bppARGB
    gdi = _gdiplus_dll()
    src, dst, graphics = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p()
    try:
        if gdi.GdipCreateBitmapFromFile(ctypes.c_wchar_p(str(path)), ctypes.byref(src)):
            raise OSError(f"Could not decode {path}")
        w, h = ctypes.c_uint(), ctypes.c_uint()
        gdi.GdipGetImageWidth(src, ctypes.byref(w))
        gdi.GdipGetImageHeight(src, ctypes.byref(h))
        if not w.value or not h.value:
            raise OSError(f"Empty image {path}")
        ratio = max_side / max(w.value, h.value)
        tw, th = max(1, round(w.value * ratio)), max(1, round(h.value * ratio))

        gdi.GdipCreateBitmapFromScan0(tw, th, 0, argb32, None, ctypes.byref(dst))
        gdi.GdipGetImageGraphicsContext(dst, ctypes.byref(graphics))
        gdi.GdipSetInterpolationMode(graphics, 7)  # high-quality bicubic
        gdi.GdipDrawImageRectI(graphics, src, 0, 0, tw, th)

        rect = (ctypes.c_int * 4)(0, 0, tw, th)
        data = BitmapData()
        if gdi.GdipBitmapLockBits(dst, rect, 1, argb32, ctypes.byref(data)):
            raise OSError(f"Could not read pixels of {path}")
        try:
            raw = ctypes.string_at(data.scan0, data.stride * th)
        finally:
            gdi.GdipBitmapUnlockBits(dst, ctypes.byref(data))
    finally:
        if graphics:
            gdi.GdipDeleteGraphics(graphics)
        for image in (dst, src):
            if image:
                gdi.GdipDisposeImage(image)

    # BGRA rows (possibly padded) -> packed RGB.
    rgb = bytearray(tw * th * 3)
    for y in range(th):
        row = raw[y * data.stride: y * data.stride + tw * 4]
        out = y * tw * 3
        rgb[out: out + tw * 3: 3] = row[2::4]
        rgb[out + 1: out + tw * 3: 3] = row[1::4]
        rgb[out + 2: out + tw * 3: 3] = row[0::4]
    return f"P6\n{tw} {th}\n255\n".encode("ascii") + bytes(rgb)


# --- Dialogs ---

class SettingsDialog(tk.Toplevel):
    def __init__(self, parent: tk.Tk, config: dict):
        super().__init__(parent)
        self.title("Settings")
        self.transient(parent)
        self.resizable(True, False)
        self.result = None
        self.rebuild = False  # set when closed with "Rebuild queue…"

        self.library = tk.StringVar(value=config["library_dir"])
        self.csv_path = tk.StringVar(value=config["csv_path"])
        self.webhook = tk.StringVar(value=config["webhook_url"])
        self.post = tk.BooleanVar(value=config["post_to_discord"])

        frm = ttk.Frame(self, padding=px(12))
        frm.pack(fill="both", expand=True)
        frm.columnconfigure(1, weight=1)

        ttk.Label(frm, text="Music library folder:").grid(row=0, column=0, sticky="w", pady=px(4))
        ttk.Entry(frm, textvariable=self.library, width=60).grid(row=0, column=1, sticky="ew", padx=px(6))
        ttk.Button(frm, text="Browse...", command=self.browse_library).grid(row=0, column=2)

        ttk.Label(frm, text="Queue CSV file:").grid(row=1, column=0, sticky="w", pady=px(4))
        ttk.Entry(frm, textvariable=self.csv_path, width=60).grid(row=1, column=1, sticky="ew", padx=px(6))
        ttk.Button(frm, text="Browse...", command=self.browse_csv).grid(row=1, column=2)

        ttk.Label(frm, text="Discord webhook URL:").grid(row=2, column=0, sticky="w", pady=px(4))
        ttk.Entry(frm, textvariable=self.webhook, width=60, show="•").grid(row=2, column=1, sticky="ew", padx=px(6))

        ttk.Checkbutton(frm, text="Post picks to Discord", variable=self.post).grid(
            row=3, column=1, sticky="w", pady=px(4), padx=px(6))

        btns = ttk.Frame(frm)
        btns.grid(row=4, column=0, columnspan=3, sticky="ew", pady=(px(10), 0))
        ttk.Button(btns, text="Rebuild queue…", command=self.on_rebuild).pack(side="left")
        ttk.Button(btns, text="Save", command=self.on_save).pack(side="right", padx=px(4))
        ttk.Button(btns, text="Cancel", command=self.destroy).pack(side="right")

        self.bind("<Escape>", lambda e: self.destroy())
        self.grab_set()

    def browse_library(self):
        path = filedialog.askdirectory(parent=self, initialdir=self.library.get() or None)
        if path:
            self.library.set(str(Path(path)))

    def browse_csv(self):
        current = Path(self.csv_path.get()) if self.csv_path.get() else APP_DIR / "albums.csv"
        path = filedialog.asksaveasfilename(
            parent=self, initialdir=current.parent, initialfile=current.name,
            defaultextension=".csv", filetypes=[("CSV files", "*.csv")],
            confirmoverwrite=False, title="Choose queue CSV (existing or new)",
        )
        if path:
            self.csv_path.set(str(Path(path)))

    def on_save(self):
        if not Path(self.library.get()).is_dir():
            messagebox.showerror("Settings", "Please choose a valid music library folder.", parent=self)
            return
        if not self.csv_path.get().strip():
            messagebox.showerror("Settings", "Please choose a queue CSV file.", parent=self)
            return
        self.result = {
            "library_dir": self.library.get().strip(),
            "csv_path": self.csv_path.get().strip(),
            "webhook_url": self.webhook.get().strip(),
            "post_to_discord": self.post.get(),
        }
        self.destroy()

    def on_rebuild(self):
        """Save the settings, then have the main window rebuild the queue from a full rescan."""
        self.on_save()
        self.rebuild = self.result is not None


class NoAudioDialog(tk.Toplevel):
    """List album folders with no audio files so the user can delete them in File Explorer.

    Not modal, so it can stay open while folders are deleted; "Check again" drops the ones that are gone.
    """

    def __init__(self, parent: tk.Tk, library: Path, albums: list[tuple[list[str], str]]):
        super().__init__(parent)
        self.title("Album folders with no audio")
        self.geometry(f"{px(760)}x{px(420)}")
        self.library = library
        self.albums = albums

        frm = ttk.Frame(self, padding=px(12))
        frm.pack(fill="both", expand=True)
        self.message_var = tk.StringVar()
        ttk.Label(frm, textvariable=self.message_var, foreground="gray", wraplength=px(720), justify="left").pack(
            anchor="w", pady=(0, px(4)))

        list_frame = ttk.Frame(frm)
        list_frame.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(list_frame, columns=("artist", "album", "contents"), show="headings",
                                 selectmode="browse")
        for col, label, width in (("artist", "Artist", 180), ("album", "Album", 320), ("contents", "Contents", 180)):
            self.tree.heading(col, text=label)
            self.tree.column(col, width=px(width), anchor="w")
        self.tree.tag_configure("odd", background="#f0f0f0")
        scroll = ttk.Scrollbar(list_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.tree.bind("<Double-1>", lambda e: self.open_folder())
        self.tree.bind("<<TreeviewSelect>>", lambda e: self.update_buttons())

        btns = ttk.Frame(frm)
        btns.pack(fill="x", pady=(px(10), 0))
        self.open_btn = ttk.Button(btns, text="Open folder", command=self.open_folder)
        self.open_btn.pack(side="left")
        ttk.Button(btns, text="Close", command=self.destroy).pack(side="right")
        ttk.Button(btns, text="Check again", command=self.recheck).pack(side="right", padx=px(4))

        self.bind("<Escape>", lambda e: self.destroy())
        self.fill()

    def fill(self) -> None:
        self.tree.delete(*self.tree.get_children())
        for i, ((artist, album, *_), note) in enumerate(self.albums):
            self.tree.insert("", "end", iid=str(i), values=(artist, album, note), tags=("odd",) if i % 2 else ())
        if self.albums:
            self.message_var.set(
                f"{len(self.albums)} album folder(s) contain no audio files. Use Open folder (or double-click) to "
                "check and delete them, then click Check again. Refresh Library removes deleted folders from the queue.")
        else:
            self.message_var.set("Every album folder contains audio files.")
        self.update_buttons()

    def update_buttons(self) -> None:
        self.open_btn.state(["!disabled"] if self.tree.selection() else ["disabled"])

    def selected_path(self) -> Path | None:
        selected = self.tree.selection()
        if not selected:
            return None
        artist, album = self.albums[int(selected[0])][0][:2]
        return self.library / artist / album

    def open_folder(self) -> None:
        path = self.selected_path()
        if not path:
            return
        try:
            os.startfile(path)
        except OSError as e:
            messagebox.showerror("Open folder", f"Could not open {path}:\n{e}", parent=self)

    def recheck(self) -> None:
        self.albums = find_albums_without_audio(self.library, [row for row, _ in self.albums])
        self.fill()


class SyncDialog(tk.Toplevel):
    """Review changes found by a library scan: new albums and deleted albums."""

    def __init__(self, parent: tk.Tk, new: list[list[str]], missing: list[list[str]]):
        super().__init__(parent)
        self.title("Library changes")
        self.transient(parent)
        self.geometry(f"{px(680)}x{px(600 if new and missing else 420)}")
        self.minsize(px(480), px(360 if new and missing else 260))
        self.new = new
        self.missing = missing
        self.result = None  # (to_add, to_skip, to_remove)
        self.new_list = self.missing_list = None

        frm = ttk.Frame(self, padding=px(12))
        frm.pack(fill="both", expand=True)

        # Buttons are packed first so the expanding lists below can never squeeze them out of view.
        btns = ttk.Frame(frm)
        btns.pack(side="bottom", fill="x", pady=(px(10), 0))
        ttk.Button(btns, text="Apply changes", command=self.on_ok).pack(side="right", padx=px(4))
        ttk.Button(btns, text="Cancel", command=self.destroy).pack(side="right")
        actions = []
        if new:
            actions.append("add the selected new albums to the queue")
        if missing:
            actions.append("remove the selected deleted albums from it")
        ttk.Label(btns, text="Apply changes will " + " and ".join(actions) + ".", foreground="gray").pack(
            side="left")

        if new:
            self.new_list = self._section(
                frm, f"New albums ({len(new)})",
                "Selected albums will be added to the queue. Unselected albums are remembered "
                "as already heard, so they won't show up again.",
                [f"{a} — {b}    (added {c[:10]})" for a, b, c in new],
            )
        if missing:
            self.missing_list = self._section(
                frm, f"Deleted from library ({len(missing)})",
                "These queued albums no longer exist in the library folder. "
                "Selected albums will be removed from the queue.",
                [f"{a} — {b}" for a, b, *_ in missing],
            )

        self.bind("<Escape>", lambda e: self.destroy())
        self.grab_set()

    def _section(self, parent, title: str, help_text: str, items: list[str]) -> tk.Listbox:
        box = ttk.LabelFrame(parent, text=title, padding=px(8))
        box.pack(fill="both", expand=True, pady=(0, px(8)))
        ttk.Label(box, text=help_text, wraplength=px(620), justify="left").pack(anchor="w", pady=(0, px(6)))

        bar = ttk.Frame(box)
        bar.pack(side="bottom", fill="x", pady=(px(6), 0))
        list_frame = ttk.Frame(box)
        list_frame.pack(fill="both", expand=True)
        # exportselection=False keeps each list's selection independent of the other.
        listbox = tk.Listbox(list_frame, selectmode="extended", activestyle="none", exportselection=False, height=4)
        scroll = ttk.Scrollbar(list_frame, orient="vertical", command=listbox.yview)
        listbox.configure(yscrollcommand=scroll.set)
        listbox.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        for item in items:
            listbox.insert("end", item)
        listbox.selection_set(0, "end")

        ttk.Button(bar, text="Select all", command=lambda: listbox.selection_set(0, "end")).pack(side="left")
        ttk.Button(bar, text="Select none", command=lambda: listbox.selection_clear(0, "end")).pack(side="left", padx=px(4))
        return listbox

    @staticmethod
    def _split(listbox: tk.Listbox | None, rows: list[list[str]]):
        selected = set(listbox.curselection()) if listbox else set()
        return ([r for i, r in enumerate(rows) if i in selected],
                [r for i, r in enumerate(rows) if i not in selected])

    def on_ok(self):
        to_add, to_skip = self._split(self.new_list, self.new)
        to_remove, _ = self._split(self.missing_list, self.missing)
        self.result = (to_add, to_skip, to_remove)
        self.destroy()


class PickListMixin:
    """Select/double-click handling for a dialog with self.tree, self.select_btn and self.matches."""

    def update_select_button(self) -> None:
        self.select_btn.state(["!disabled"] if self.tree.selection() else ["disabled"])

    def on_double_click(self, event) -> None:
        iid = self.tree.identify_row(event.y)
        if iid:
            self.tree.selection_set(iid)
            self.on_select()

    def on_select(self) -> None:
        selected = self.tree.selection()
        if not selected:
            return
        self.result = self.matches[int(selected[0])]
        self.destroy()


class ShortlistDialog(PickListMixin, tk.Toplevel):
    """Show a short ranked list of queued albums; pick one with Select or a double-click.

    rows are queue rows; extra is an optional (column label, values) shown as a last column.
    """

    def __init__(self, parent: tk.Tk, title: str, rows: list[list[str]], extra: tuple[str, list[str]] | None = None):
        super().__init__(parent)
        self.title(title)
        self.transient(parent)
        self.geometry(f"{px(720)}x{px(400)}")
        self.matches = rows
        self.result = None  # the chosen queue row

        frm = ttk.Frame(self, padding=px(12))
        frm.pack(fill="both", expand=True)
        ttk.Label(frm, text="Select an album and click Select (or double-click it).", foreground="gray").pack(
            anchor="w", pady=(0, px(4)))

        columns = [("artist", "Artist", 180), ("album", "Album", 280), ("added", "Date Added", 110)]
        if extra:
            columns.append(("extra", extra[0], 90))
        list_frame = ttk.Frame(frm)
        list_frame.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(list_frame, columns=[c[0] for c in columns], show="headings", selectmode="browse")
        for col, label, width in columns:
            self.tree.heading(col, text=label)
            self.tree.column(col, width=px(width), anchor="e" if col == "extra" else "w", stretch=col != "extra")
        self.tree.tag_configure("odd", background="#f0f0f0")
        scroll = ttk.Scrollbar(list_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        for i, (artist, album, created, *_) in enumerate(rows):
            values = (artist, album, created[:10]) + ((extra[1][i],) if extra else ())
            self.tree.insert("", "end", iid=str(i), values=values, tags=("odd",) if i % 2 else ())
        self.tree.bind("<Double-1>", self.on_double_click)
        self.tree.bind("<Return>", lambda e: self.on_select())
        self.tree.bind("<<TreeviewSelect>>", lambda e: self.update_select_button())

        btns = ttk.Frame(frm)
        btns.pack(fill="x", pady=(px(10), 0))
        self.select_btn = ttk.Button(btns, text="Select", command=self.on_select)
        self.select_btn.pack(side="right", padx=px(4))
        ttk.Button(btns, text="Cancel", command=self.destroy).pack(side="right")
        self.update_select_button()

        self.bind("<Escape>", lambda e: self.destroy())
        self.tree.focus_set()
        self.grab_set()


class ManualPickDialog(PickListMixin, tk.Toplevel):
    """Search queued albums by artist folder name; pick one with Select or a double-click."""

    def __init__(self, parent: tk.Tk, queue: list[list[str]]):
        super().__init__(parent)
        self.title("Manually pick")
        self.transient(parent)
        self.geometry(f"{px(640)}x{px(460)}")
        self.queue = queue
        self.matches: list[list[str]] = []
        self.result = None  # the chosen queue row

        frm = ttk.Frame(self, padding=px(12))
        frm.pack(fill="both", expand=True)

        bar = ttk.Frame(frm)
        bar.pack(fill="x")
        ttk.Label(bar, text="Artist folder:").pack(side="left")
        self.query = tk.StringVar()
        entry = ttk.Entry(bar, textvariable=self.query)
        entry.pack(side="left", fill="x", expand=True, padx=px(6))
        entry.bind("<Return>", lambda e: self.search())
        ttk.Button(bar, text="Search", command=self.search).pack(side="left")
        self.query.trace_add("write", lambda *_: self.on_query_changed())

        self.message_var = tk.StringVar(value="Type at least 3 letters to see matches, then select an album and click "
                                              "Select (or double-click it).")
        ttk.Label(frm, textvariable=self.message_var, foreground="gray").pack(anchor="w", pady=(px(8), px(4)))

        list_frame = ttk.Frame(frm)
        list_frame.pack(fill="both", expand=True)
        # "browse" allows exactly one selected row at a time.
        self.tree = ttk.Treeview(list_frame, columns=("artist", "album", "added"), show="headings",
                                 selectmode="browse")
        for col, label, width in (("artist", "Artist", 180), ("album", "Album", 280), ("added", "Date Added", 110)):
            self.tree.heading(col, text=label)
            self.tree.column(col, width=px(width), anchor="w")
        self.tree.tag_configure("odd", background="#f0f0f0")
        scroll = ttk.Scrollbar(list_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.tree.bind("<Double-1>", self.on_double_click)
        self.tree.bind("<Return>", lambda e: self.on_select())
        self.tree.bind("<<TreeviewSelect>>", lambda e: self.update_select_button())

        btns = ttk.Frame(frm)
        btns.pack(fill="x", pady=(px(10), 0))
        self.select_btn = ttk.Button(btns, text="Select", command=self.on_select)
        self.select_btn.pack(side="right", padx=px(4))
        ttk.Button(btns, text="Cancel", command=self.destroy).pack(side="right")
        self.update_select_button()

        self.bind("<Escape>", lambda e: self.destroy())
        entry.focus_set()
        self.grab_set()

    def on_query_changed(self) -> None:
        """Search as you type once the query has 3+ characters; Enter/Search still work for shorter ones."""
        if len(self.query.get().strip()) >= 3:
            self.search()
        else:
            self.matches = []
            self.tree.delete(*self.tree.get_children())
            self.update_select_button()
            self.message_var.set("Type at least 3 letters to see matches.")

    def search(self) -> None:
        query = self.query.get().strip().casefold()
        self.tree.delete(*self.tree.get_children())
        self.update_select_button()
        if not query:
            self.matches = []
            self.message_var.set("Enter part of an artist folder name to search.")
            return
        self.matches = sorted((r for r in self.queue if query in r[0].casefold()),
                              key=lambda r: (r[0].casefold(), r[1].casefold()))
        if not self.matches:
            self.message_var.set(f"No search results for “{self.query.get().strip()}”.")
            return
        for i, (artist, album, created, *_) in enumerate(self.matches):
            self.tree.insert("", "end", iid=str(i), values=(artist, album, created[:10]),
                             tags=("odd",) if i % 2 else ())
        self.message_var.set(f"{len(self.matches)} album(s) found. Select one and click Select, "
                             "or double-click it.")


# --- Main window ---

class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.config = load_config()
        root.title("Album Picker")
        root.minsize(px(480), px(480))
        self.restore_window_size()
        root.protocol("WM_DELETE_WINDOW", self.on_close)

        # Status bar is packed first so the expanding main frame can't squeeze it out.
        self.status_var = tk.StringVar(value="Ready.")
        ttk.Label(root, textvariable=self.status_var, relief="sunken", anchor="w", padding=(px(8), px(2))).pack(
            side="bottom", fill="x")

        frm = ttk.Frame(root, padding=px(16))
        frm.pack(fill="both", expand=True)

        # Now playing card
        card = ttk.LabelFrame(frm, text="Up next", padding=px(12))
        card.pack(fill="x")
        self.artist_var = tk.StringVar(value="—")
        self.album_var = tk.StringVar(value="Press “Random Pick!” to choose")
        self.added_var = tk.StringVar(value="")
        self.length_var = tk.StringVar(value="")
        self.length_request = 0
        # Cover art, flush right; empty (zero-size) when the album has none.
        self.cover_label = ttk.Label(card)
        self.cover_label.pack(side="right", anchor="ne", padx=(px(12), 0))
        self.cover_image = None  # keep a reference so Tk doesn't drop the image
        self.cover_request = 0
        text = ttk.Frame(card)
        text.pack(side="left", fill="x", expand=True, anchor="n")
        ttk.Label(text, textvariable=self.artist_var, font=("Segoe UI", 18, "bold")).pack(anchor="w")
        ttk.Label(text, textvariable=self.album_var, font=("Segoe UI", 14, "italic")).pack(anchor="w")
        ttk.Label(text, textvariable=self.added_var, foreground="gray").pack(anchor="w", pady=(px(4), 0))
        ttk.Label(text, textvariable=self.length_var, foreground="gray").pack(anchor="w")

        # Buttons
        btns = ttk.Frame(frm)
        btns.pack(fill="x", pady=px(12))
        self.pick_btn = ttk.Button(btns, text="🎲  Random Pick!", command=self.pick)
        self.pick_btn.pack(side="left", ipadx=px(8), ipady=px(6))
        ttk.Button(btns, text="🔍  Manually pick", command=self.manual_pick).pack(
            side="left", padx=(px(8), 0), ipadx=px(8), ipady=px(6))
        self.update_btn = ttk.Button(btns, text="🔄  Refresh Library", command=self.update_from_library)
        self.update_btn.pack(side="left", padx=px(8), ipadx=px(8), ipady=px(6))
        self.shortlist_btn = ttk.Menubutton(btns, text="📋  Shortlists")
        self.shortlist_btn.pack(side="left", ipadx=px(8), ipady=px(6))
        menu = tk.Menu(self.shortlist_btn, tearoff=False)
        for label, kind in (("10 oldest Date Added", "oldest"), ("10 newest Date Added", "newest"),
                            ("10 smallest folders", "smallest"), ("10 biggest folders", "biggest")):
            menu.add_command(label=label, command=lambda k=kind: self.shortlist_pick(k))
        self.shortlist_btn["menu"] = menu
        ttk.Button(btns, text="⚙  Settings", command=self.open_settings).pack(side="right", ipady=px(6))

        # Queue count, then the queue CSV's file name as a link: click opens it with the
        # default program, right-click offers "Open with…" and "Show in folder".
        queue_line = ttk.Frame(frm)
        queue_line.pack(anchor="w")
        self.queue_var = tk.StringVar()
        ttk.Label(queue_line, textvariable=self.queue_var).pack(side="left")
        link_font = font.nametofont("TkDefaultFont").copy()
        link_font.configure(underline=True)
        self.queue_link = ttk.Label(queue_line, foreground="#0066cc", cursor="hand2", font=link_font)
        self.queue_link.bind("<Button-1>", lambda e: self.open_queue_file())
        self.queue_link.bind("<Button-3>", self.show_queue_link_menu)
        self.queue_link_menu = tk.Menu(root, tearoff=False)
        self.queue_link_menu.add_command(label="Open", command=self.open_queue_file)
        self.queue_link_menu.add_command(label="Open with…", command=lambda: self.open_queue_file(choose=True))
        self.queue_link_menu.add_command(label="Show in folder", command=self.show_queue_file)

        # Recent picks
        recent = ttk.LabelFrame(frm, text="Recent picks", padding=px(6))
        recent.pack(fill="both", expand=True, pady=(px(8), 0))
        self.tree = ttk.Treeview(recent, columns=("date", "artist", "album", "added"), show="headings", height=8)
        saved_widths = self.config.get("recent_columns") or {}
        for col, label, width in (("date", "Selected On", 130), ("artist", "Artist", 170),
                                  ("album", "Album", 230), ("added", "Date Added", 110)):
            self.tree.heading(col, text=label)
            saved = saved_widths.get(col)
            width = saved if isinstance(saved, int) and saved >= px(30) else px(width)
            self.tree.column(col, width=width, anchor="w")
        self.tree.tag_configure("odd", background="#f0f0f0")
        scroll = ttk.Scrollbar(recent, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

        self.refresh()
        if not self.config["library_dir"]:
            root.after(100, self.open_settings)

    # Window size
    def restore_window_size(self) -> None:
        default_w, default_h = px(960), px(600)
        try:
            w, h = (int(n) for n in self.config.get("window_size", "").split("x"))
        except ValueError:
            w, h = default_w, default_h
        # Never open bigger than the screen (e.g. size saved on a larger monitor).
        w = min(max(w, px(480)), self.root.winfo_screenwidth())
        h = min(max(h, px(480)), self.root.winfo_screenheight())
        self.root.geometry(f"{w}x{h}")
        if self.config.get("window_zoomed"):
            self.root.state("zoomed")

    def on_close(self) -> None:
        zoomed = self.root.state() == "zoomed"
        self.config["window_zoomed"] = zoomed
        if not zoomed:  # a maximized size isn't useful to restore; keep the last normal size
            self.config["window_size"] = f"{self.root.winfo_width()}x{self.root.winfo_height()}"
        self.config["recent_columns"] = {col: self.tree.column(col, "width") for col in self.tree["columns"]}
        try:
            save_config(self.config)
        except OSError:
            pass
        self.root.destroy()

    # Paths
    @property
    def csv_path(self) -> Path:
        return Path(self.config["csv_path"])

    @property
    def history_path(self) -> Path:
        return history_path(self.csv_path)

    def show_queue_link_menu(self, event: tk.Event) -> None:
        try:
            self.queue_link_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.queue_link_menu.grab_release()

    def open_queue_file(self, choose: bool = False) -> None:
        path = self.csv_path
        try:
            if choose:  # Windows' "How do you want to open this file?" dialog
                subprocess.Popen(["rundll32.exe", "shell32.dll,OpenAs_RunDLL", str(path)])
            else:
                os.startfile(path)
        except OSError as e:
            messagebox.showerror("Open queue", f"Could not open {path}:\n{e}")

    def show_queue_file(self) -> None:
        subprocess.Popen(["explorer.exe", f"/select,{self.csv_path}"])

    def set_status(self, text: str) -> None:
        self.status_var.set(text)

    def refresh(self) -> None:
        try:
            queue = read_rows(self.csv_path)
            history = read_rows(self.history_path)
        except (OSError, csv.Error) as e:
            self.queue_var.set(f"Could not read queue: {e}")
            self.queue_link.pack_forget()
            return
        self.queue_var.set(f"{len(queue)} albums left in the queue  •  ")
        self.queue_link.configure(text=self.csv_path.name)
        self.queue_link.pack(side="left")
        self.tree.delete(*self.tree.get_children())
        played = [h for h in history if len(h) >= 5 and h[4] == "played"]
        recent = reversed(played[-RECENT_COUNT:])
        for i, (artist, album, created, picked, _status) in enumerate(recent):
            self.tree.insert("", "end", values=(short_time(picked), artist, album, created[:10]),
                             tags=("odd",) if i % 2 else ())

    # Settings
    def open_settings(self) -> None:
        dlg = SettingsDialog(self.root, self.config)
        self.root.wait_window(dlg)
        if dlg.result:
            self.config.update(dlg.result)  # keep keys the dialog doesn't edit (e.g. window size)
            save_config(self.config)
            self.refresh()
            self.set_status("Settings saved.")
            if dlg.rebuild:
                self.rebuild_queue()

    # Pick
    def pick(self) -> None:
        try:
            queue = read_rows(self.csv_path)
        except (OSError, csv.Error) as e:
            messagebox.showerror("Pick", f"Could not read {self.csv_path}:\n{e}")
            return
        if not queue:
            messagebox.showinfo("Pick", "No albums left in the queue!\nUse “Refresh Library” to add some.")
            return

        self.commit_pick(queue, queue.pop(random.randrange(len(queue))))

    def read_queue_for_pick(self, title: str) -> list[list[str]] | None:
        """The queue rows, or None (after telling the user) if it can't be read or is empty."""
        try:
            queue = read_rows(self.csv_path)
        except (OSError, csv.Error) as e:
            messagebox.showerror(title, f"Could not read {self.csv_path}:\n{e}")
            return None
        if not queue:
            messagebox.showinfo(title, "No albums left in the queue!\nUse “Refresh Library” to add some.")
            return None
        return queue

    def pick_chosen(self, title: str, dlg: tk.Toplevel) -> None:
        """Wait for a pick dialog, then pick the album it returned (if it is still queued)."""
        self.root.wait_window(dlg)
        if not dlg.result:
            return
        key = album_key(dlg.result)
        try:
            queue = read_rows(self.csv_path)  # re-read in case it changed while the dialog was open
        except (OSError, csv.Error) as e:
            messagebox.showerror(title, f"Could not read {self.csv_path}:\n{e}")
            return
        index = next((i for i, r in enumerate(queue) if album_key(r) == key), None)
        if index is None:
            messagebox.showerror(title, "That album is no longer in the queue.")
            self.refresh()
            return
        self.commit_pick(queue, queue.pop(index))

    def manual_pick(self) -> None:
        queue = self.read_queue_for_pick("Manually pick")
        if queue:
            self.pick_chosen("Manually pick", ManualPickDialog(self.root, queue))

    def shortlist_pick(self, kind: str) -> None:
        """Offer the 10 oldest/newest queued albums by Date Added, or the 10 smallest/biggest folders."""
        titles = {"oldest": "10 oldest Date Added", "newest": "10 newest Date Added",
                  "smallest": "10 smallest folders", "biggest": "10 biggest folders"}
        title = titles[kind]
        queue = self.read_queue_for_pick(title)
        if not queue:
            return
        if kind in ("oldest", "newest"):
            rows = sorted(queue, key=lambda r: r[2], reverse=kind == "newest")[:10]
            self.pick_chosen(title, ShortlistDialog(self.root, title, rows))
            return

        library = Path(self.config["library_dir"])
        if not library.is_dir():
            messagebox.showerror(title, "Music library folder not found. Check Settings.")
            return
        self.shortlist_btn.state(["disabled"])
        self.set_status(f"Measuring {len(queue)} album folders ...")

        def worker():
            sized = []
            for row in queue:
                try:
                    sized.append((folder_size(library / row[0] / row[1]), row))
                except OSError:
                    pass  # folder gone or unreadable; Refresh Library will deal with it
            self.root.after(0, finish, sized)

        def finish(sized):
            self.shortlist_btn.state(["!disabled"])
            self.set_status(f"Measured {len(sized)} album folders.")
            if not sized:
                messagebox.showerror(title, "None of the queued album folders could be read.")
                return
            sized.sort(key=lambda pair: pair[0], reverse=kind == "biggest")
            top = sized[:10]
            dlg = ShortlistDialog(self.root, title, [row for _, row in top],
                                  extra=("Size", [format_size(size) for size, _ in top]))
            self.pick_chosen(title, dlg)

        threading.Thread(target=worker, daemon=True).start()

    def commit_pick(self, remaining: list[list[str]], row: list[str]) -> None:
        """Save the queue without the picked row, log it, show it, and post it to Discord."""
        artist, album, created = row[:3]
        try:
            write_queue(self.csv_path, remaining)
            append_history(self.history_path, [[artist, album, created, datetime.now().strftime(DATE_FMT), "played"]])
        except OSError as e:
            messagebox.showerror("Pick", f"Could not update the queue:\n{e}")
            return

        self.artist_var.set(display_artist(artist))
        self.album_var.set(album)
        try:
            self.added_var.set(f"Added {short_time(created)} ({time_since(created)} ago)")
        except ValueError:
            self.added_var.set(f"Added {short_time(created)}")
        self.show_cover(artist, album)
        self.show_length(artist, album)
        self.refresh()

        url = self.config["webhook_url"]
        if self.config["post_to_discord"] and url:
            self.set_status("Posting to Discord...")
            message = f"💿 Up Next: **{display_artist(artist)}** — *{album}*"
            threading.Thread(
                target=lambda: self.root.after(0, self.set_status, post_to_discord(url, message)),
                daemon=True,
            ).start()
        else:
            self.set_status("Picked (Discord posting is off).")

    def show_cover(self, artist: str, album: str) -> None:
        """Clear the cover, then load the album's cover image (if any) off the UI thread."""
        self.cover_request += 1
        request = self.cover_request
        self.cover_image = None
        self.cover_label.configure(image="")
        if sys.platform != "win32" or not self.config["library_dir"]:
            return
        album_dir = Path(self.config["library_dir"]) / artist / album

        def worker():
            try:
                cover = find_cover(album_dir)
                data = load_cover_ppm(cover, px(COVER_SIZE)) if cover else None
            except OSError:
                data = None
            if data:
                self.root.after(0, self.set_cover, request, data)

        threading.Thread(target=worker, daemon=True).start()

    def set_cover(self, request: int, data: bytes) -> None:
        if request != self.cover_request:
            return  # a newer pick has replaced this one
        try:
            self.cover_image = tk.PhotoImage(data=data, format="ppm")
        except tk.TclError:
            return
        self.cover_label.configure(image=self.cover_image)

    def show_length(self, artist: str, album: str) -> None:
        """Total up the album's track lengths (read from file headers) off the UI thread."""
        self.length_request += 1
        request = self.length_request
        if not self.config["library_dir"]:
            self.length_var.set("")
            return
        album_dir = Path(self.config["library_dir"]) / artist / album
        self.length_var.set("Length: …")

        def worker():
            seconds, tracks, failed = album_length(album_dir)
            if tracks:
                text = f"Length: {format_length(seconds)}  •  {tracks} track{'s' if tracks != 1 else ''}"
                if failed:
                    text += f" ({failed} unreadable)"
            else:
                text = "Length: unknown" + (f" ({failed} unreadable tracks)" if failed else "")
            self.root.after(0, self.set_length, request, text)

        threading.Thread(target=worker, daemon=True).start()

    def set_length(self, request: int, text: str) -> None:
        if request == self.length_request:  # ignore results for an earlier pick
            self.length_var.set(text)

    # Update
    def scan_in_background(self, title: str, on_done) -> None:
        """Scan the library off the UI thread, then call on_done(found, unreadable) unless it failed."""
        library = Path(self.config["library_dir"])
        if not library.is_dir():
            messagebox.showerror(title, "Music library folder not found. Check Settings.")
            return
        self.update_btn.state(["disabled"])
        self.set_status(f"Scanning {library} ...")

        def worker():
            try:
                found, unreadable = scan_library(library)
                no_audio = find_albums_without_audio(library, found)
                error = None
            except OSError as e:
                found, unreadable, no_audio, error = [], set(), [], e
            self.root.after(0, finish, found, unreadable, no_audio, error)

        def finish(found, unreadable, no_audio, error):
            self.update_btn.state(["!disabled"])
            if error:
                self.set_status("Scan failed.")
                messagebox.showerror(title, f"Scan failed:\n{error}")
            elif not found:
                # Guard against an unplugged drive / unmounted share making every album look deleted.
                self.set_status("Scan found no albums.")
                messagebox.showerror(title, "No albums found in the library folder.\n"
                                            "Is the drive connected? Nothing was changed.")
            else:
                on_done(found, unreadable)
                if no_audio:
                    self.show_no_audio(library, no_audio)

        threading.Thread(target=worker, daemon=True).start()

    def show_no_audio(self, library: Path, albums: list[tuple[list[str], str]]) -> None:
        """Show (or refill) the list of album folders that have no audio files."""
        dlg = getattr(self, "no_audio_dialog", None)
        if dlg is not None and dlg.winfo_exists():
            dlg.library, dlg.albums = library, albums
            dlg.fill()
            dlg.lift()
        else:
            self.no_audio_dialog = NoAudioDialog(self.root, library, albums)

    def update_from_library(self) -> None:
        self.scan_in_background("Update", self.finish_update)

    def finish_update(self, found: list[list[str]], unreadable: set[str]) -> None:
        try:
            queue = read_rows(self.csv_path)
            history = read_rows(self.history_path)
        except (OSError, csv.Error) as e:
            messagebox.showerror("Update", f"Could not read queue/history:\n{e}")
            return

        new, missing = compute_changes(found, unreadable, queue, history)
        status = f"Scanned {len(found)} album(s) in library."
        if unreadable:
            status += f" Couldn't read {len(unreadable)} artist folder(s)."
        self.set_status(status)
        if not new and not missing:
            messagebox.showinfo("Update", "The queue is already in sync with the library.")
            return

        dlg = SyncDialog(self.root, new, missing)
        self.root.wait_window(dlg)
        if not dlg.result:
            return
        to_add, to_skip, to_remove = dlg.result

        now = datetime.now().strftime(DATE_FMT)
        remove_keys = {album_key(r) for r in to_remove}
        try:
            if to_add or to_remove:
                queue = read_rows(self.csv_path)  # re-read in case it changed while the dialog was open
                queue = [r for r in queue if album_key(r) not in remove_keys] + to_add
                write_queue(self.csv_path, queue)
            if to_skip:
                append_history(self.history_path, [[a, b, c, now, "skipped"] for a, b, c in to_skip])
        except (OSError, csv.Error) as e:
            messagebox.showerror("Update", f"Could not update the queue:\n{e}")
            return
        self.refresh()
        parts = [f"Added {len(to_add)}", f"removed {len(to_remove)} deleted"]
        if to_skip:
            parts.append(f"marked {len(to_skip)} as already heard")
        self.set_status(", ".join(parts) + " album(s).")

    def rebuild_queue(self) -> None:
        """Rescan the whole library and write a brand-new queue CSV (like re-running albumfinder.py)."""
        if not messagebox.askokcancel(
            "Rebuild queue",
            f"Rescan all of {self.config['library_dir']} and write a new {self.csv_path.name} "
            "listing every album found?\n\nThe current queue is replaced. played.csv is not changed.",
            icon="warning",
        ):
            return
        try:
            self.rebuild_old_count = len(read_rows(self.csv_path))
        except (OSError, csv.Error):
            self.rebuild_old_count = None
        self.scan_in_background("Rebuild queue", self.finish_rebuild)

    def finish_rebuild(self, found: list[list[str]], unreadable: set[str]) -> None:
        try:
            write_queue(self.csv_path, found)
        except OSError as e:
            messagebox.showerror("Rebuild queue", f"Could not write {self.csv_path}:\n{e}")
            return
        self.refresh()
        self.set_status(f"Rebuilt queue: {len(found)} album(s) from a full library scan.")
        message = f"Wrote a new {self.csv_path.name} with {len(found)} album(s)"
        if self.rebuild_old_count is not None:
            message += f" (was {self.rebuild_old_count})"
        message += "."
        if unreadable:
            message += (f"\n\nCouldn't read {len(unreadable)} artist folder(s), so their albums are not "
                        "in the queue:\n" + "\n".join(sorted(unreadable)[:10]))
        messagebox.showinfo("Rebuild queue", message)

def enable_high_dpi() -> None:
    """Tell Windows we render at native resolution, so it doesn't bitmap-stretch (blur) the window."""
    if sys.platform != "win32":
        return
    import ctypes
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)  # system DPI aware (Windows 8.1+)
    except (AttributeError, OSError):
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except (AttributeError, OSError):
            pass


def apply_style(root: tk.Tk) -> None:
    global SCALE
    SCALE = root.winfo_fpixels("1i") / 96

    # Fonts are in points, so Tk scales them with the DPI automatically.
    for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont", "TkCaptionFont"):
        font.nametofont(name).configure(family="Segoe UI", size=10)
    font.nametofont("TkHeadingFont").configure(weight="bold")

    style = ttk.Style(root)
    try:
        style.theme_use("vista")
    except tk.TclError:
        pass
    # Row height is in pixels and doesn't scale on its own.
    style.configure("Treeview", rowheight=px(26))
    style.configure("Treeview.Heading", padding=(px(4), px(4)))


def main() -> None:
    enable_high_dpi()
    root = tk.Tk()
    # In a PyInstaller build the icon is unpacked to sys._MEIPASS, not next to the exe.
    icon = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent)) / "album_picker.ico"
    try:
        root.iconbitmap(default=str(icon))  # default= also applies to dialogs
    except tk.TclError:
        pass
    apply_style(root)
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
