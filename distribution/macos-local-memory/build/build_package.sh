#!/bin/zsh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
SOURCE="$ROOT/distribution/macos-local-memory"
STAGE="$ROOT/.tmp/local-memory-macos-package"
APP="$STAGE/Local Memory Search.app"
RESOURCES="$APP/Contents/Resources"
DELIVERABLES="$ROOT/deliverables"
PACKAGE_VERSION="0.5"
PACKAGE_BUILD="5"
DMG_NAME="Local-Memory-Search-macOS-unsigned.dmg"
ZIP_NAME="Local-Memory-Search-macOS-unsigned.zip"
CHECKSUM_NAME="Local-Memory-Search-macOS-unsigned.sha256.txt"
DMG="$DELIVERABLES/$DMG_NAME"
ZIP="$DELIVERABLES/$ZIP_NAME"
CHECKSUM="$DELIVERABLES/$CHECKSUM_NAME"
OUTPUT_STAGE=""

cleanup_output_stage() {
  if [ -n "${OUTPUT_STAGE:-}" ] && [ -d "$OUTPUT_STAGE" ]; then
    rm -rf -- "$OUTPUT_STAGE"
  fi
}
trap cleanup_output_stage EXIT

rm -rf "$STAGE"
mkdir -p "$STAGE/導入ガイド" "$DELIVERABLES"
OUTPUT_STAGE="$(mktemp -d "$DELIVERABLES/.local-memory-package.XXXXXX")"
DMG_CANDIDATE="$OUTPUT_STAGE/$DMG_NAME"
ZIP_CANDIDATE="$OUTPUT_STAGE/$ZIP_NAME"
CHECKSUM_CANDIDATE="$OUTPUT_STAGE/$CHECKSUM_NAME"
ln -s /Applications "$STAGE/Applications"

/usr/bin/osacompile -l JavaScript -o "$APP" "$SOURCE/app/launcher.js"
mkdir -p "$RESOURCES/engine"
cp "$SOURCE/app/bootstrap.py" "$SOURCE/app/claim_graph_validator.py" "$SOURCE/app/final_answer_audit.py" "$SOURCE/app/cross_document_semantic_graph_edge_audit.py" "$SOURCE/app/semantic_graph_answer_promotion.py" "$SOURCE/app/semantic_graph_trust.py" "$SOURCE/app/launcher_lease.py" "$SOURCE/app/local_memory_server.py" "$SOURCE/app/launch.sh" "$RESOURCES/"
cp "$SOURCE/engine/"*.py "$RESOURCES/engine/"
mkdir -p "$RESOURCES/engine/layer1/scripts" "$RESOURCES/engine/layer1/schemas"
cp \
  "$ROOT/scripts/build_intermediate_records.py" \
  "$ROOT/scripts/probe_intermediate_records.py" \
  "$ROOT/scripts/evidence_text_chunking.py" \
  "$ROOT/scripts/build_search_units.py" \
  "$ROOT/scripts/validate_search_units.py" \
  "$ROOT/scripts/validate_intermediate_records.py" \
  "$ROOT/scripts/validate_intermediate_records_streaming.py" \
  "$ROOT/scripts/lexical_search_common.py" \
  "$ROOT/scripts/adapt_layer1_to_local_memory.py" \
  "$ROOT/scripts/build_cross_document_semantic_graph.py" \
  "$ROOT/scripts/query_cross_document_semantic_graph.py" \
  "$ROOT/scripts/validate_cross_document_semantic_graph.py" \
  "$ROOT/scripts/project_cross_document_graph_to_answer_index.py" \
  "$ROOT/scripts/local_image_ocr.py" \
  "$ROOT/scripts/local_paddle_ocr.py" \
  "$ROOT/scripts/extract_ocr_observations.py" \
  "$ROOT/scripts/classify_visual_assets.py" \
  "$ROOT/scripts/validate_ocr_observations.py" \
  "$ROOT/scripts/validate_visual_classifications.py" \
  "$ROOT/scripts/ollama_embedding_common.py" \
  "$ROOT/scripts/image_canonicalizer.swift" \
  "$ROOT/scripts/apple_vision_ocr.swift" \
  "$RESOURCES/engine/layer1/scripts/"
cp \
  "$ROOT/schemas/document.schema.json" \
  "$ROOT/schemas/evidence.schema.json" \
  "$ROOT/schemas/relation.schema.json" \
  "$ROOT/schemas/search-unit.schema.json" \
  "$ROOT/schemas/ocr-observation.schema.json" \
  "$ROOT/schemas/visual-classification.schema.json" \
  "$RESOURCES/engine/layer1/schemas/"
cp \
  "$SOURCE/paddleocr-requirements.lock.txt" \
  "$SOURCE/paddleocr-model-manifest.json" \
  "$RESOURCES/"
cp "$SOURCE/docs/はじめにお読みください.md" "$STAGE/導入ガイド/"
cp "$SOURCE/docs/START-HERE.html" "$STAGE/START-HERE.html"
chmod +x "$RESOURCES/launch.sh" "$RESOURCES/"*.py "$RESOURCES/engine/"*.py "$RESOURCES/engine/layer1/scripts/"*.py

PLIST="$APP/Contents/Info.plist"
/usr/libexec/PlistBuddy -c "Add :CFBundleIdentifier string jp.rivage.local-memory-search" "$PLIST" 2>/dev/null || /usr/libexec/PlistBuddy -c "Set :CFBundleIdentifier jp.rivage.local-memory-search" "$PLIST"
/usr/libexec/PlistBuddy -c "Add :LSMinimumSystemVersion string 14.0" "$PLIST" 2>/dev/null || /usr/libexec/PlistBuddy -c "Set :LSMinimumSystemVersion 14.0" "$PLIST"
/usr/libexec/PlistBuddy -c "Add :CFBundleShortVersionString string $PACKAGE_VERSION" "$PLIST" 2>/dev/null || /usr/libexec/PlistBuddy -c "Set :CFBundleShortVersionString $PACKAGE_VERSION" "$PLIST"
/usr/libexec/PlistBuddy -c "Add :CFBundleVersion string $PACKAGE_BUILD" "$PLIST" 2>/dev/null || /usr/libexec/PlistBuddy -c "Set :CFBundleVersion $PACKAGE_BUILD" "$PLIST"
for key in NSAppleMusicUsageDescription NSCalendarsUsageDescription NSCameraUsageDescription NSContactsUsageDescription NSHomeKitUsageDescription NSMicrophoneUsageDescription NSPhotoLibraryUsageDescription NSRemindersUsageDescription NSSiriUsageDescription NSSystemAdministrationUsageDescription; do
  /usr/libexec/PlistBuddy -c "Delete :$key" "$PLIST" 2>/dev/null || true
done
/usr/bin/codesign --force --deep --sign - "$APP"

# Remove build-machine metadata and prove that no runtime/user data is included.
find "$STAGE" -name '.DS_Store' -delete
FORBIDDEN_FILE="$(find "$STAGE" -type f \( -name '*.sqlite3' -o -name '*.jsonl' -o -name '*.log' \) -print -quit)"
if [ -n "$FORBIDDEN_FILE" ]; then
  print -u2 "refusing to package generated data: $FORBIDDEN_FILE"
  exit 1
fi

/usr/bin/codesign --verify --deep --strict "$APP"
/usr/bin/hdiutil create -volname "Local Memory Search" -srcfolder "$STAGE" -ov -format UDZO "$DMG_CANDIDATE" >/dev/null
(cd "$STAGE" && /usr/bin/ditto -c -k --sequesterRsrc --keepParent "Local Memory Search.app" "$ZIP_CANDIDATE")
/usr/bin/hdiutil verify "$DMG_CANDIDATE" >/dev/null
/usr/bin/unzip -tq "$ZIP_CANDIDATE"
(
  cd "$OUTPUT_STAGE"
  /usr/bin/shasum -a 256 "$DMG_NAME" "$ZIP_NAME" > "$CHECKSUM_NAME"
)

# Keep the last complete release until every candidate artifact has passed.
/bin/mv -f "$DMG_CANDIDATE" "$DMG"
/bin/mv -f "$ZIP_CANDIDATE" "$ZIP"
/bin/mv -f "$CHECKSUM_CANDIDATE" "$CHECKSUM"
print "created: $DMG"
print "created: $ZIP"
print "created: $CHECKSUM"
