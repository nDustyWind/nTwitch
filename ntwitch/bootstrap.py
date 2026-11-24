"""Installs the Python packages nTwitch needs before anything imports them.

Stdlib only, and must be called before importing textual/rich/streamlink.
"""
import importlib.metadata
import importlib.util
import os
import sys

# module to import -> (pip distribution name, minimum major.minor)
REQUIRED = {
    "textual": ("textual", (0, 86)),   # custom themes need 0.86+
    "rich": ("rich", (13, 0)),
    "streamlink": ("streamlink", (6, 0)),
    "keyring": ("keyring", (24, 0)),   # chat token in the OS credential store
}

_ACCENT = "\x1b[38;2;186;186;255m"  # #BABAFF
_DIM = "\x1b[2m"
_RESET = "\x1b[0m"


def _enable_ansi() -> None:
    if os.name == "nt":
        os.system("")  # flips the console into VT mode


def _version_tuple(text: str):
    parts = []
    for piece in text.split(".")[:2]:
        digits = "".join(ch for ch in piece if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    while len(parts) < 2:
        parts.append(0)
    return tuple(parts)


def missing_packages() -> list:
    """pip requirement strings for every package that is absent or too old."""
    missing = []
    for module, (dist, minimum) in REQUIRED.items():
        spec = f"{dist}>={minimum[0]}.{minimum[1]}"
        if importlib.util.find_spec(module) is None:
            missing.append(spec)
            continue
        try:
            if _version_tuple(importlib.metadata.version(dist)) < minimum:
                missing.append(spec)
        except importlib.metadata.PackageNotFoundError:
            pass  # importable but not pip-managed (e.g. vendored): trust it
    return missing


def ensure_python_packages() -> None:
    """Install missing packages, or exit with a clear message if that is impossible."""
    if getattr(sys, "frozen", False):
        return  # a bundled exe already contains everything
    missing = missing_packages()
    if not missing:
        return

    _enable_ansi()
    print(f"\n{_ACCENT}nTwitch{_RESET} {_DIM}- first run: installing required packages "
          f"({', '.join(m.split('>=')[0] for m in missing)})...{_RESET}", flush=True)

    from .tools import pip_install  # noqa: E402 (stdlib-only module)

    if not pip_install(missing):
        print(f"\nCould not install the required packages automatically.\n"
              f"Please run:  {sys.executable} -m pip install {' '.join(missing)}\n")
        sys.exit(1)

    importlib.invalidate_caches()
    still = missing_packages()
    if still:
        print(f"\nInstalled, but these are still unavailable: {', '.join(still)}.\n"
              f"Please restart nTwitch.\n")
        sys.exit(1)
