#!/bin/bash
# Builds Ixzib.app (a tiny launcher you can start from Spotlight: Cmd+Space -> "Ixzib").
# usage: make_app.sh <project-root> <folder-for-the-app>
set -euo pipefail
ROOT="$1"; APP_DIR="$2"
APP="$APP_DIR/Ixzib.app"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>Ixzib</string>
  <key>CFBundleDisplayName</key><string>Ixzib</string>
  <key>CFBundleIdentifier</key><string>local.ixzib.assistant</string>
  <key>CFBundleExecutable</key><string>Ixzib</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleVersion</key><string>1.0</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>LSUIElement</key><true/>
  <key>LSMinimumSystemVersion</key><string>13.0</string>
  <key>NSAppleEventsUsageDescription</key><string>Ixzib opens links and your terminal on request.</string>
  <key>NSMicrophoneUsageDescription</key><string>Ixzib listens for your voice commands.</string>
</dict></plist>
PLIST

cat > "$APP/Contents/MacOS/Ixzib" <<'LAUNCHER'
#!/bin/bash
# Launcher: starts the Python core + the widget; closing the widget (✕) stops the core too.
export PATH="/opt/homebrew/bin:$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
ROOT="__ROOT__"
LOG="$HOME/Library/Logs/Ixzib"; mkdir -p "$LOG"
ELECTRON="$ROOT/widget/electron/Electron.app/Contents/MacOS/Electron"
[ -x "$ELECTRON" ] || ELECTRON="$ROOT/widget/node_modules/electron/dist/Electron.app/Contents/MacOS/Electron"

# already running -> nothing to do
if pgrep -f "$ELECTRON" >/dev/null; then exit 0; fi

CORE=""
if ! pgrep -f "ixzib_core.py" >/dev/null; then
  cd "$ROOT/core" && "$ROOT/core/.venv/bin/python" -u ixzib_core.py >"$LOG/core.log" 2>&1 &
  CORE=$!
fi

cd "$ROOT/widget" && "$ELECTRON" . >"$LOG/widget.log" 2>&1

# widget closed -> stop the core
[ -n "$CORE" ] && kill "$CORE" 2>/dev/null
pkill -f "ixzib_core.py" 2>/dev/null
exit 0
LAUNCHER
# bake the project path in (sed with | so slashes are fine)
sed -i '' "s|__ROOT__|$ROOT|g" "$APP/Contents/MacOS/Ixzib"
chmod +x "$APP/Contents/MacOS/Ixzib"
codesign --force --deep --sign - "$APP" >/dev/null 2>&1 || true
mdimport "$APP" >/dev/null 2>&1 || true
echo "$APP"
