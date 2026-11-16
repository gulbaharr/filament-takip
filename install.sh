#!/bin/bash
# server.py / index.html değişince uygulamaya kopyalar ve yeniden başlatır.
APP="$HOME/Applications/Filament Takip.app"
cp "$(dirname "$0")"/{server.py,lan.py,slicer.py,index.html} "$APP/Contents/Resources/"
pkill -f "Filament Takip.app/Contents/Resources/server.py"; sleep 1; open "$APP"
