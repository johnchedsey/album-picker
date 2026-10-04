"""
Build AlbumPicker.exe (a single-file Windows app) with PyInstaller.

Usage:
    python -m pip install pyinstaller   # one-time, build machine only
    python build_exe.py

The exe is copied to this folder. Keep it next to .env and
album_picker_config.json: the app looks for both beside the exe.
"""

import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
NAME = "AlbumPicker"


def main() -> None:
    subprocess.run(
        [
            sys.executable, "-m", "PyInstaller",
            "--noconfirm", "--clean",
            "--onefile",
            "--windowed",                      # no console window
            "--name", NAME,
            "--icon", "album_picker.ico",
            "--add-data", "album_picker.ico;.",  # window icon, read at runtime
            "album_picker_gui.pyw",
        ],
        cwd=HERE,
        check=True,
    )
    shutil.copy2(HERE / "dist" / f"{NAME}.exe", HERE / f"{NAME}.exe")
    print(f"\nBuilt {HERE / f'{NAME}.exe'}")


if __name__ == "__main__":
    main()
