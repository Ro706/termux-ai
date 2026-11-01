"""First-run dependency check: downloads any missing Python modules with pip."""
from __future__ import annotations

import importlib.util
import subprocess
import sys

REQUIRED = {"textual": "textual>=0.86", "rich": "rich>=13.7", "httpx": "httpx>=0.27"}


def missing() -> list[str]:
    return [spec for mod, spec in REQUIRED.items() if importlib.util.find_spec(mod) is None]


def ensure_deps() -> None:
    need = missing()
    if not need:
        return
    print(f"Termux AI: downloading missing modules: {', '.join(need)}")
    cmd = [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", *need]
    r = subprocess.run(cmd)
    if r.returncode != 0:
        r = subprocess.run(cmd + ["--break-system-packages"])
    if r.returncode != 0 or missing():
        sys.exit(
            "Could not install modules automatically. Check your internet connection, then run:\n"
            f"  pip install {' '.join(need)}"
        )
    print("Modules installed.\n")
