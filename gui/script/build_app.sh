#!/bin/zsh
# Build MediaGrab.app into dist/ (SwiftPM build, same toolchain Xcode uses).
#
# The bundle is self-contained: it carries its own copy of the mgd engine
# (cli/) and of the browser extension, so the app works wherever it is
# installed - no path back to this repository.
#
#   ./script/build_app.sh                 # dist/MediaGrab.app (release)
#
# Overrides (used by the automated checks, not for normal builds):
#   CONFIG=debug        build configuration
#   OUT_DIR=<dir>       where the .app goes (default: dist)
#   BUNDLE_ID=…         bundle identifier
#   URL_SCHEME=…        hand-off url scheme (default: mediagrab)
#   CLAIM_MAGNET=0      do not register for magnet links / .torrent files
set -euo pipefail
cd "$(dirname "$0")/.."

VERSION="2.2.0"
BUILD="13"
CONFIG="${CONFIG:-release}"
OUT_DIR="${OUT_DIR:-dist}"
BUNDLE_ID="${BUNDLE_ID:-com.alirizwantariq.mediagrab}"
URL_SCHEME="${URL_SCHEME:-mediagrab}"
CLAIM_MAGNET="${CLAIM_MAGNET:-1}"
REPO="$(cd .. && pwd)"

swift build -c "$CONFIG"
SWIFT_BIN="$(swift build -c "$CONFIG" --show-bin-path)"
APP="$OUT_DIR/MediaGrab.app"

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

# ── app icon: render + icns (best-effort; bundle still works without it) ──
if command -v swiftc >/dev/null 2>&1 && command -v iconutil >/dev/null 2>&1; then
  ICONSET="$OUT_DIR/.AppIcon.iconset"
  rm -rf "$ICONSET"
  mkdir -p "$ICONSET"
  swiftc -O -o "$ICONSET/render" script/render_icon.swift
  "$ICONSET/render" "$ICONSET/icon_512x512@2x.png"
  rm "$ICONSET/render"
  for sz in 16 32 128 256 512; do
    sips -z $sz $sz "$ICONSET/icon_512x512@2x.png" --out "$ICONSET/icon_${sz}x${sz}.png" >/dev/null
    sips -z $((sz*2)) $((sz*2)) "$ICONSET/icon_512x512@2x.png" --out "$ICONSET/icon_${sz}x${sz}@2x.png" >/dev/null
  done
  iconutil -c icns "$ICONSET" -o "$APP/Contents/Resources/AppIcon.icns"
  rm -rf "$ICONSET"
fi

# ── the engine: cli/bin/mgd + the mgd package (no tests, no bytecode) ──
ENGINE="$APP/Contents/Resources/engine"
mkdir -p "$ENGINE/bin" "$ENGINE/mgd"
cp "$REPO/cli/bin/mgd" "$ENGINE/bin/mgd"
chmod 755 "$ENGINE/bin/mgd"
cp "$REPO"/cli/mgd/*.py "$ENGINE/mgd/"

# ── the browser extension (the app's "Browser Extension" sheet exports it) ──
EXT="$APP/Contents/Resources/chrome-extension"
mkdir -p "$EXT"
for f in "$REPO"/chrome-extension/*; do
  [[ -f "$f" ]] && [[ "${f:t}" != "README.md" ]] && cp "$f" "$EXT/"
done

MAGNET_TYPES=""
DOCUMENT_TYPES=""
if [[ "$CLAIM_MAGNET" == "1" ]]; then
  MAGNET_TYPES='
        <dict>
            <key>CFBundleURLName</key>
            <string>BitTorrent magnet link</string>
            <key>CFBundleURLSchemes</key>
            <array>
                <string>magnet</string>
            </array>
            <key>CFBundleURLRole</key>
            <string>Viewer</string>
        </dict>'
  DOCUMENT_TYPES='
    <key>CFBundleDocumentTypes</key>
    <array>
        <dict>
            <key>CFBundleTypeName</key>
            <string>BitTorrent metainfo file</string>
            <key>CFBundleTypeRole</key>
            <string>Viewer</string>
            <key>CFBundleTypeExtensions</key>
            <array>
                <string>torrent</string>
            </array>
            <key>LSHandlerRank</key>
            <string>Alternate</string>
        </dict>
    </array>'
fi

cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleIdentifier</key>
    <string>$BUNDLE_ID</string>
    <key>CFBundleName</key>
    <string>MediaGrab</string>
    <key>CFBundleDisplayName</key>
    <string>MediaGrab</string>
    <key>CFBundleExecutable</key>
    <string>MediaGrab</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>CFBundleShortVersionString</key>
    <string>$VERSION</string>
    <key>CFBundleVersion</key>
    <string>$BUILD</string>
    <key>CFBundleURLTypes</key>
    <array>$MAGNET_TYPES
        <dict>
            <key>CFBundleURLName</key>
            <string>MediaGrab download handoff</string>
            <key>CFBundleURLSchemes</key>
            <array>
                <string>$URL_SCHEME</string>
            </array>
            <key>CFBundleURLRole</key>
            <string>Viewer</string>
        </dict>
    </array>$DOCUMENT_TYPES
    <key>CFBundleIconFile</key>
    <string>AppIcon</string>
    <key>LSMinimumSystemVersion</key>
    <string>13.0</string>
    <key>LSApplicationCategoryType</key>
    <string>public.app-category.utilities</string>
    <key>NSHighResolutionCapable</key>
    <true/>
    <key>NSPrincipalClass</key>
    <string>NSApplication</string>
    <key>NSHumanReadableCopyright</key>
    <string>MediaGrab $VERSION</string>
</dict>
</plist>
PLIST

cp "$SWIFT_BIN/MediaGrab" "$APP/Contents/MacOS/MediaGrab"
# ad-hoc signature (seals the bundled engine + extension as resources)
codesign --force --deep --sign - "$APP" >/dev/null 2>&1 || true

# A build sitting in dist/ must not take over mediagrab:// and magnet links
# from the copy installed in /Applications (LaunchServices prefers the
# newest version it has seen). Launching this build registers it again.
LSREGISTER="/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister"
[[ -x "$LSREGISTER" ]] && "$LSREGISTER" -u "${APP:A}" >/dev/null 2>&1 || true
echo "built: ${APP:A}  (v$VERSION build $BUILD, $CONFIG)"
