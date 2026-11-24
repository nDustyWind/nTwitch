"""Build a standalone nTwitch executable:  python build_scripts/build.py

The exe bundles Python and every package. VLC and Streamlink are still installed on first
run by the exe itself (winget on Windows), exactly like the script version.
Output: dist/nTwitch.exe  (dist/nTwitch on macOS/Linux)
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("Installing PyInstaller...")
        subprocess.run([sys.executable, "-m", "pip", "install", "pyinstaller"], check=True)

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean", "--onefile", "--console",
        "--name", "nTwitch",
        "--distpath", str(ROOT / "dist"),
        "--workpath", str(ROOT / "build" / "pyinstaller"),
        "--specpath", str(ROOT / "build"),
        # modules these libraries load dynamically
        "--collect-submodules", "ntwitch",
        "--collect-submodules", "keyring.backends",
        "--collect-all", "textual",
        "--collect-submodules", "rich",
        "--hidden-import", "win32ctypes.core",
        str(ROOT / "nTwitch.py"),
    ]
    print("Building...")
    result = subprocess.run(cmd, cwd=ROOT)
    if result.returncode != 0:
        print("\nBuild failed.")
        return result.returncode
    print(f"\nDone: {ROOT / 'dist'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
