"""Build hermes-mobile.exe. Bumps the last version part on every build.

Usage: python build.py            (bumps version.py, builds the exe in this folder)
"""
import os
import re
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
VERSION_FILE = os.path.join(HERE, "version.py")
WORK = os.path.join(os.environ.get("LOCALAPPDATA", HERE), "hermes", "cache", "scratch", "hermes-build")


def bump():
    text = open(VERSION_FILE, encoding="utf-8").read()
    m = re.search(r'VERSION = "(\d+)\.(\d+)\.(\d+)"', text)
    if not m:
        sys.exit("version.py: VERSION not found")
    major, minor, patch = m.group(1), m.group(2), int(m.group(3))
    new = f"{major}.{minor}.{patch + 1:03d}"
    open(VERSION_FILE, "w", encoding="utf-8").write(
        text.replace(m.group(0), f'VERSION = "{new}"'))
    return new


def main():
    version = bump()
    print(f"building version {version}")
    # The running exe locks the file on Windows; stop it first.
    subprocess.run(["taskkill", "/IM", "hermes-mobile.exe", "/F"],
                   capture_output=True, text=True)
    cmd = [
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--onefile", "--noconsole",
        "--name", "hermes-mobile",
        "--icon", os.path.join(HERE, "assets", "tray.ico"),
        "--add-data", f"{os.path.join(HERE, 'assets')};assets",
        "--distpath", os.path.join(WORK, "dist"),
        "--workpath", os.path.join(WORK, "build"),
        "--specpath", WORK,
        os.path.join(HERE, "tray.py"),
    ]
    r = subprocess.run(cmd, cwd=HERE, capture_output=True, text=True)
    if r.returncode != 0:
        print(r.stdout[-2000:], r.stderr[-2000:])
        sys.exit(f"build failed (version.py already bumped to {version})")
    shutil.move(os.path.join(WORK, "dist", "hermes-mobile.exe"),
                os.path.join(HERE, "hermes-mobile.exe"))
    print(f"ok: hermes-mobile.exe {version}")


if __name__ == "__main__":
    main()
