#!/bin/zsh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
SOURCE="$ROOT/distribution/macos-local-memory"
STAGE="$ROOT/.tmp/local-memory-macos-package"
APP="$STAGE/Local Memory Search.app"
RESOURCES="$APP/Contents/Resources"
DELIVERABLES="$ROOT/deliverables"
DMG="$DELIVERABLES/Local-Memory-Search-macOS-unsigned.dmg"
ZIP="$DELIVERABLES/Local-Memory-Search-macOS-unsigned.zip"
CHECKSUM="$DELIVERABLES/Local-Memory-Search-macOS-unsigned.sha256.txt"

rm -rf "$STAGE"
mkdir -p "$STAGE/導入ガイド" "$DELIVERABLES"
ln -s /Applications "$STAGE/Applications"

/usr/bin/osacompile -l JavaScript -o "$APP" "$SOURCE/app/launcher.js"
mkdir -p "$RESOURCES/engine"
cp "$SOURCE/app/bootstrap.py" "$SOURCE/app/final_answer_audit.py" "$SOURCE/app/local_memory_server.py" "$SOURCE/app/launch.sh" "$RESOURCES/"
cp "$SOURCE/engine/"*.py "$RESOURCES/engine/"
cp "$SOURCE/docs/はじめにお読みください.md" "$STAGE/導入ガイド/"
cp "$SOURCE/docs/START-HERE.html" "$STAGE/START-HERE.html"
chmod +x "$RESOURCES/launch.sh" "$RESOURCES/"*.py "$RESOURCES/engine/"*.py

PLIST="$APP/Contents/Info.plist"
/usr/libexec/PlistBuddy -c "Add :CFBundleIdentifier string jp.rivage.local-memory-search" "$PLIST" 2>/dev/null || /usr/libexec/PlistBuddy -c "Set :CFBundleIdentifier jp.rivage.local-memory-search" "$PLIST"
/usr/libexec/PlistBuddy -c "Add :LSMinimumSystemVersion string 14.0" "$PLIST" 2>/dev/null || /usr/libexec/PlistBuddy -c "Set :LSMinimumSystemVersion 14.0" "$PLIST"
for key in NSAppleMusicUsageDescription NSCalendarsUsageDescription NSCameraUsageDescription NSContactsUsageDescription NSHomeKitUsageDescription NSMicrophoneUsageDescription NSPhotoLibraryUsageDescription NSRemindersUsageDescription NSSiriUsageDescription NSSystemAdministrationUsageDescription; do
  /usr/libexec/PlistBuddy -c "Delete :$key" "$PLIST" 2>/dev/null || true
done
/usr/bin/codesign --force --deep --sign - "$APP"

# Remove build-machine metadata and prove that no runtime/user data is included.
find "$STAGE" -name '.DS_Store' -delete
if find "$STAGE" -type f \( -name '*.sqlite3' -o -name '*.jsonl' -o -name '*.log' \) | grep -q .; then
  print -u2 "refusing to package generated data"
  exit 1
fi

rm -f "$DMG" "$ZIP" "$CHECKSUM"
/usr/bin/hdiutil create -volname "Local Memory Search" -srcfolder "$STAGE" -ov -format UDZO "$DMG" >/dev/null
(cd "$STAGE" && /usr/bin/ditto -c -k --sequesterRsrc --keepParent "Local Memory Search.app" "$ZIP")
/usr/bin/shasum -a 256 "$DMG" "$ZIP" > "$CHECKSUM"
print "created: $DMG"
print "created: $ZIP"
print "created: $CHECKSUM"
