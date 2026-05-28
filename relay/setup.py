"""py2app build configuration for rrelay menu bar app.

Run ./build.sh — it handles dependencies, build, signing, and DMG creation.
"""
from setuptools import setup

APP       = ["menubar.py"]
DATA_FILES = [("", ["rrelay.py"])]   # menubar launches rrelay.py as a subprocess

OPTIONS = {
    "argv_emulation": False,          # must be False for menu bar apps on modern macOS
    "packages":       ["rumps", "websockets", "pythonosc"],
    "plist": {
        "LSUIElement":                True,   # menu bar only — no Dock icon
        "CFBundleName":               "rrelay",
        "CFBundleDisplayName":        "rrelay",
        "CFBundleIdentifier":         "com.marionettes.rrelay",
        "CFBundleVersion":            "1.0.0",
        "CFBundleShortVersionString": "1.0.0",
        "NSHighResolutionCapable":    True,
    },
}

setup(
    name="rrelay",
    app=APP,
    data_files=DATA_FILES,
    options={"py2app": OPTIONS},
    setup_requires=["py2app"],
)
