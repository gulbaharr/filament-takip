#!/bin/bash
# ~/Applications/Filament Takip.app'i kurar (yoksa oluşturur), kodu kopyalar ve yeniden başlatır.
set -e
SRC="$(cd "$(dirname "$0")" && pwd)"
APP="$HOME/Applications/Filament Takip.app"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

if [ ! -f "$APP/Contents/Info.plist" ]; then
  cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>Filament Takip</string>
  <key>CFBundleIdentifier</key><string>local.filamenttakip</string>
  <key>CFBundleExecutable</key><string>FilamentTakip</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>LSUIElement</key><true/>
</dict></plist>
PLIST
fi

if [ ! -f "$APP/Contents/MacOS/FilamentTakip" ]; then
  cat > "$APP/Contents/MacOS/FilamentTakip" <<'SH'
#!/bin/bash
DIR="$(cd "$(dirname "$0")/../Resources" && pwd)"
LOG="$HOME/Library/Application Support/FilamentTakip/server.log"
mkdir -p "$(dirname "$LOG")"
if curl -s -o /dev/null --max-time 1 http://127.0.0.1:8765/api/state; then
  open http://127.0.0.1:8765
else
  nohup /usr/bin/python3 "$DIR/server.py" >> "$LOG" 2>&1 &
fi
SH
  chmod +x "$APP/Contents/MacOS/FilamentTakip"
fi

cp "$SRC"/{server.py,lan.py,bambu.py,slicer.py,index.html} "$APP/Contents/Resources/"
pkill -f "Filament Takip.app/Contents/Resources/server.py" || true
sleep 1
open "$APP"
echo "Kuruldu: $APP → http://127.0.0.1:8765"
