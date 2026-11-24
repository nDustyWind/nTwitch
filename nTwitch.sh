#!/usr/bin/env sh
# nTwitch launcher for macOS / Linux. Sets everything up on first run.
cd "$(dirname "$0")" || exit 1
VENV=".venv"

if [ ! -x "$VENV/bin/python" ]; then
    echo
    echo " nTwitch - setting up for the first time, this only happens once..."
    PY=""
    for c in python3 python; do
        if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null; then
            PY="$c"
            break
        fi
    done

    if [ -n "$PY" ]; then
        "$PY" -m venv "$VENV" || {
            echo " Could not create a Python environment. On Debian/Ubuntu try: sudo apt install python3-venv"
            exit 1
        }
    elif command -v uv >/dev/null 2>&1; then
        uv venv "$VENV" --python 3.12 || exit 1
    else
        echo " Python 3.9 or newer is required. Install it (e.g. 'brew install python' or 'sudo apt install python3 python3-venv') and run again."
        exit 1
    fi
fi

PYTHONUTF8=1 exec "$VENV/bin/python" nTwitch.py "$@"
