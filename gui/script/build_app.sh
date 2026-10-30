#!/bin/zsh
# Build MediaGrab.app into dist/ (SwiftPM release build, same toolchain Xcode uses).
set -euo pipefail
cd "$(dirname "$0")/.."

SWIFT_BIN="$(swift build -c release --show-bin-path)"
swift build -c release
APP="dist/MediaGrab.app"

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleIdentifier</key>
    <string>com.alirizwantariq.mediagrab</string>
    <key>CFBundleName</key>
    <string>MediaGrab</string>
    <key>CFBundleDisplayName</key>
    <string>MediaGrab</string>
    <key>CFBundleExecutable</key>
    <string>MediaGrab</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>CFBundleShortVersionString</key>
    <string>1.0.0</string>
    <key>CFBundleVersion</key>
    <string>1</string>
    <key>LSMinimumSystemVersion</key>
    <string>13.0</string>
    <key>NSHighResolutionCapable</key>
    <true/>
    <key>NSPrincipalClass</key>
    <string>NSApplication</string>
</dict>
</plist>
PLIST

cp "$SWIFT_BIN/MediaGrab" "$APP/Contents/MacOS/MediaGrab"
codesign --force --deep --sign - "$APP" >/dev/null 2>&1 || true
echo "built: $(pwd)/$APP"