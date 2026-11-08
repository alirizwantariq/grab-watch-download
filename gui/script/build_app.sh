#!/bin/zsh
# Build MediaGrab.app into dist/ (SwiftPM release build, same toolchain Xcode uses).
set -euo pipefail
cd "$(dirname "$0")/.."

SWIFT_BIN="$(swift build -c release --show-bin-path)"
swift build -c release
APP="dist/MediaGrab.app"

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

# ── app icon: render + icns (best-effort; bundle still works without it) ──
if command -v swiftc >/dev/null 2>&1 && command -v iconutil >/dev/null 2>&1; then
  ICONSET="dist/.AppIcon.iconset"
  rm -rf "$ICONSET" dist/AppIcon.icns
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
    <string>1.1.0</string>
    <key>CFBundleVersion</key>
    <string>2</string>
    <key>CFBundleIconFile</key>
    <string>AppIcon</string>
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