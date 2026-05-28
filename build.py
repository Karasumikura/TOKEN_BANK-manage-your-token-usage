#!/usr/bin/env python3
"""Build TOKENBANK.exe - run this once to create a distributable .exe"""

import subprocess
import sys
import os

os.chdir(os.path.dirname(os.path.abspath(__file__)))

print("=" * 50)
print(" Building TOKENBANK...")
print("=" * 50)

# Install deps
subprocess.check_call([sys.executable, "-m", "pip", "install", "pyinstaller", "-q"])
subprocess.check_call([sys.executable, "-m", "pip", "install", "-r", "requirements.txt", "-q"])

print("\nCompiling .exe ...\n")

cmd = [
    sys.executable, "-m", "PyInstaller",
    "--noconfirm", "--onefile", "--windowed",
    "--name", "TOKENBANK",
    "--hidden-import", "webview",
    "--hidden-import", "webview.platforms.winforms",
    "--hidden-import", "webview.platforms.edgechromium",
    "--hidden-import", "clr",
    "--hidden-import", "pystray._win32",
    "--hidden-import", "PIL",
    "--hidden-import", "PIL._tkinter_finder",
    "--collect-data", "webview",
    "app.py",
]

result = subprocess.run(cmd)

if result.returncode != 0:
    print("\nBuild FAILED!")
    sys.exit(1)

print("\n" + "=" * 50)
print(" Build complete!")
print(" Output: dist\\TOKENBANK.exe")
print(" Copy this single .exe to any Windows PC")
print(" - no Python needed!")
print("=" * 50)
