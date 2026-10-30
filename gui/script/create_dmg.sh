#!/bin/zsh
# Create a drag-to-Applications DMG from the built MediaGrab.app.
set -euo pipefail
cd "$(dirname "$0")/.."

./script/build_app.sh
mkdir -p dist
rm -f "dist/MediaGrab-macos-arm64.dmg"
ln -sf /Applications dist/Applications
hdiutil create -volname MediaGrab \
    -srcfolder dist \
    -ov -format UDZO \
    "dist/MediaGrab-macos-arm64.dmg"
echo "dmg: $(pwd)/dist/MediaGrab-macos-arm64.dmg"
open -R "dist/MediaGrab-macos-arm64.dmg" 2>/dev/null || true