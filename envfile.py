"""
Minimal .env support (stdlib only) for secrets such as the Discord webhook URL.

The .env file lives next to the scripts and is git-ignored. Format: KEY=value,
one per line; blank lines and lines starting with # are ignored. A real
environment variable of the same name takes precedence over the file.
"""

import os
import sys
from pathlib import Path

ENV_PATH = (
    Path(sys.executable).parent if getattr(sys, "frozen", False)
    else Path(__file__).resolve().parent
) / ".env"

WEBHOOK_KEY = "DISCORD_WEBHOOK_URL"


def read_env(path: Path = ENV_PATH) -> dict[str, str]:
    values = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip("'\"")
    return values


def get_env(key: str, default: str = "", path: Path = ENV_PATH) -> str:
    return os.environ.get(key) or read_env(path).get(key, default)


def set_env(key: str, value: str, path: Path = ENV_PATH) -> None:
    """Set (or add) one key in the .env file, preserving other lines and comments."""
    lines = path.read_text(encoding="utf-8-sig").splitlines() if path.exists() else []
    new_line = f"{key}={value}"
    for i, line in enumerate(lines):
        if line.strip().split("=", 1)[0].strip() == key and not line.strip().startswith("#"):
            lines[i] = new_line
            break
    else:
        lines.append(new_line)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
