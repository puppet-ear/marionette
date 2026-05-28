#!/bin/bash
# Build rrelay.app + rrelay.dmg for distribution.
#
# Requirements: Homebrew Python 3.12
# Usage: cd relay && ./build.sh

set -e
cd "$(dirname "$0")"

PYTHON=/opt/homebrew/opt/python@3.12/bin/python3.12

echo "→ Setting up build venv..."
$PYTHON -m venv .build-venv
source .build-venv/bin/activate

echo "→ Installing build dependencies..."
pip install --quiet pyinstaller rumps websockets python-osc

echo "→ Cleaning previous build..."
rm -rf build dist

echo "→ Building rrelay.app..."
python -m PyInstaller \
  --windowed \
  --onedir \
  --name rrelay \
  --icon rrelay.icns \
  --hidden-import rumps \
  --hidden-import websockets \
  --hidden-import pythonosc \
  --hidden-import pythonosc.udp_client \
  menubar.py

echo "→ Ad-hoc signing..."
codesign --force --deep --sign - "dist/rrelay.app"

echo "→ Building rrelay.dmg..."
rm -f dist/rrelay.dmg
hdiutil create \
  -volname rrelay \
  -srcfolder dist/rrelay.app \
  -ov -format UDZO \
  dist/rrelay.dmg

deactivate

echo ""
echo "✓  dist/rrelay.app  ready — drag to /Applications to install"
echo "✓  dist/rrelay.dmg  ready — distributable disk image"
