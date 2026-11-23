#!/bin/zsh
# Create a drag-to-Applications DMG from a fresh build of MediaGrab.app.
#
#   ./script/create_dmg.sh        # dist/MediaGrab-macos-arm64.dmg
#   NO_REVEAL=1 ./script/create_dmg.sh   # do not show it in Finder afterwards
set -euo pipefail
cd "$(dirname "$0")/.."

./script/build_app.sh
ARCH="$(uname -m)"
DMG="dist/MediaGrab-macos-${ARCH}.dmg"

# stage exactly what belongs on the disk image (dist/ may hold other things)
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
cp -R dist/MediaGrab.app "$STAGE/"
ln -s /Applications "$STAGE/Applications"

rm -f "$DMG"
hdiutil create -volname MediaGrab \
    -srcfolder "$STAGE" \
    -ov -format UDZO \
    "$DMG" >/dev/null
echo "dmg: $(pwd)/$DMG ($(du -h "$DMG" | cut -f1 | tr -d ' '))"
[[ -n "${NO_REVEAL:-}" ]] || open -R "$DMG" 2>/dev/null || true
