"""Launcher shim: `python nTwitch.py` is the same as the installed `nTwitch` command."""
from ntwitch.cli import run

if __name__ == "__main__":
    run()
