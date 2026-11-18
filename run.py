#!/usr/bin/env python3
"""One-command launcher:  python run.py

Creates a local virtualenv (.venv), installs requirements.txt into it the first time
(or whenever requirements.txt changes), then starts the server on http://127.0.0.1:8000.
Only the Python standard library is used here, so it works on macOS, Linux and Windows.
Override the address with HOST / PORT environment variables.
"""
import os
import subprocess
import sys
import venv
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV = ROOT / ".venv"
REQUIREMENTS = ROOT / "requirements.txt"
STAMP = VENV / ".requirements-installed"


def venv_python() -> Path:
    return VENV / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def main() -> int:
    if sys.version_info < (3, 9):
        print("Python 3.9 or newer is required.", file=sys.stderr)
        return 1
    if not venv_python().exists():
        print("Creating virtual environment in .venv ...")
        venv.create(VENV, with_pip=True)
    if not STAMP.exists() or STAMP.stat().st_mtime < REQUIREMENTS.stat().st_mtime:
        print("Installing dependencies ...")
        subprocess.check_call([str(venv_python()), "-m", "pip", "install", "--quiet", "-r", str(REQUIREMENTS)])
        STAMP.touch()

    host, port = os.environ.get("HOST", "127.0.0.1"), os.environ.get("PORT", "8000")
    print(f"\nEvent Sync Service -> http://{host}:{port}   (API docs: http://{host}:{port}/docs)\n")
    try:
        return subprocess.call(
            [str(venv_python()), "-m", "uvicorn", "app.main:app", "--host", host, "--port", port],
            cwd=ROOT,
        )
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
