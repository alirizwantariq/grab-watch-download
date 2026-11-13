#!/bin/zsh
# Install the MediaGrab Chrome native-messaging host.
#
#   zsh cli/script/install_chrome_host.sh
#
# What it does:
#   1. writes a tiny launcher to ~/.config/mgd/mgd-native-host
#   2. registers the native-messaging manifest for every Chromium browser
#      it finds (Chrome, Brave, Edge, Chromium, Arc, Vivaldi)
#   3. prints the fixed extension id to allow in the manifest (matches the
#      "key" baked into chrome-extension/manifest.json)
#
# After running this, load the extension:
#   chrome://extensions -> Developer mode -> Load unpacked ->
#   -> <repo>/chrome-extension
set -euo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
MGD="$REPO/cli/bin/mgd"
EXT_ID="hgebmpanpoiebnbnmabafnpmkkmdgdmn"   # from manifest.json "key"
HOST_NAME="com.alirizwantariq.mediagrab"

[[ -f "$MGD" ]] || { echo "error: $MGD not found"; exit 1; }

# 1. launcher script (Chrome refuses relative paths in the manifest)
LAUNCH_DIR="$HOME/.config/mgd"
mkdir -p "$LAUNCH_DIR"
LAUNCHER="$LAUNCH_DIR/mgd-native-host"
cat > "$LAUNCHER" <<EOF
#!/bin/sh
# MediaGrab native-messaging launcher (created by install_chrome_host.sh)
exec "$MGD" native-host
EOF
chmod +x "$LAUNCHER"
echo "launcher: $LAUNCHER -> $MGD native-host"

# 2. register for every browser present
MANIFEST_JSON=$(cat <<EOF
{
  "name": "$HOST_NAME",
  "description": "MediaGrab download integration (mgd)",
  "path": "$LAUNCHER",
  "type": "stdio",
  "allowed_origins": ["chrome-extension://$EXT_ID/"]
}
EOF
)

register() {
  local dir="$1"
  [[ -d "${dir:h}" ]] || return 0
  mkdir -p "$dir"
  print -r -- "$MANIFEST_JSON" > "$dir/$HOST_NAME.json"
  echo "registered: $dir/$HOST_NAME.json"
}

BROWSER_SUPPORT="$HOME/Library/Application Support"
register "$BROWSER_SUPPORT/Google/Chrome/NativeMessagingHosts"
register "$BROWSER_SUPPORT/BraveSoftware/Brave-Browser/NativeMessagingHosts"
register "$BROWSER_SUPPORT/Microsoft Edge/NativeMessagingHosts"
register "$BROWSER_SUPPORT/Chromium/NativeMessagingHosts"
register "$BROWSER_SUPPORT/Arc/User Data/NativeMessagingHosts"
register "$BROWSER_SUPPORT/Vivaldi/NativeMessagingHosts"

# Chromium (non-mac layout) just in case
[[ -d "$HOME/.config/chromium" ]] && \
  register "$HOME/.config/chromium/NativeMessagingHosts"

echo ""
echo "done. now load the extension (one time):"
echo "  1. open chrome://extensions"
echo "  2. enable 'Developer mode' (top right)"
echo "  3. 'Load unpacked' -> $REPO/chrome-extension"
echo "  4. pin the MediaGrab icon, open it on any page with videos"
