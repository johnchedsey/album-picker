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

from version import __version__

HERE = Path(__file__).resolve().parent
NAME = "AlbumPicker"
VERSION_FILE = HERE / "build" / "version_info.txt"


def write_version_file() -> None:
    """PyInstaller version resource, shown in the exe's Properties → Details."""
    nums = tuple(int(n) for n in __version__.split(".")) + (0,)
    VERSION_FILE.parent.mkdir(parents=True, exist_ok=True)
    VERSION_FILE.write_text(f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers={nums}, prodvers={nums}),
  kids=[
    StringFileInfo([StringTable('040904B0', [
      StringStruct('ProductName', 'Album Picker'),
      StringStruct('FileDescription', 'Album Picker'),
      StringStruct('FileVersion', '{__version__}'),
      StringStruct('ProductVersion', '{__version__}'),
      StringStruct('OriginalFilename', '{NAME}.exe'),
    ])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])]),
  ],
)
""", encoding="utf-8")


def main() -> None:
    write_version_file()
    subprocess.run(
        [
            sys.executable, "-m", "PyInstaller",
            "--noconfirm", "--clean",
            "--onefile",
            "--windowed",                      # no console window
            "--name", NAME,
            "--icon", "album_picker.ico",
            "--version-file", str(VERSION_FILE),
            "--add-data", "album_picker.ico;.",  # window icon, read at runtime
            "album_picker_gui.pyw",
        ],
        cwd=HERE,
        check=True,
    )
    shutil.copy2(HERE / "dist" / f"{NAME}.exe", HERE / f"{NAME}.exe")
    print(f"\nBuilt {HERE / f'{NAME}.exe'} (v{__version__})")


if __name__ == "__main__":
    main()
