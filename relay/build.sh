#!/bin/bash
# Build rrelay.app and package it as a distributable DMG.
#
# Requirements: Python 3.9+ (framework build — /usr/bin/python3 on macOS)
# Usage: cd relay && ./build.sh

set -e
cd "$(dirname "$0")"

PYTHON=/usr/bin/python3   # must be the macOS framework build for rumps to work

echo "→ Installing build dependencies..."
$PYTHON -m pip install --quiet py2app rumps websockets python-osc

echo "→ Cleaning previous build..."
rm -rf build dist

echo "→ Building rrelay.app..."
$PYTHON setup.py py2app --quiet

echo "→ Ad-hoc signing..."
codesign --force --deep --sign - "dist/rrelay.app"

echo "→ Creating DMG..."
hdiutil create \
  -volname "rrelay" \
  -srcfolder "dist/rrelay.app" \
  -ov -format UDZO \
  "dist/rrelay.dmg"

echo ""
echo "✓  dist/rrelay.dmg  ready to distribute"
