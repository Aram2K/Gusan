#!/bin/bash
set -euo pipefail

IOS_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$IOS_DIR"
xcodegen generate
mkdir -p build
WORK_DIR="$(mktemp -d "$IOS_DIR/build/ipa.XXXXXX")"
trap 'rm -rf "$WORK_DIR"' EXIT

xcodebuild -project Gusan.xcodeproj -scheme Gusan \
    -configuration Release -destination 'generic/platform=iOS' \
    -derivedDataPath "$WORK_DIR/DerivedData" build \
    CODE_SIGNING_ALLOWED=NO CODE_SIGNING_REQUIRED=NO CODE_SIGN_IDENTITY="" \
    'SWIFT_ACTIVE_COMPILATION_CONDITIONS=$(inherited) GUSAN_SIDELOAD'

mkdir -p "$WORK_DIR/Payload"
ditto "$WORK_DIR/DerivedData/Build/Products/Release-iphoneos/Gusan.app" "$WORK_DIR/Payload/Gusan.app"
APP="$WORK_DIR/Payload/Gusan.app"
KEYBOARD="$APP/PlugIns/GusanKeyboard.appex"
SCREEN="$APP/PlugIns/GusanScreen.appex"
ACTIVITY="$APP/PlugIns/GusanActivity.appex"
for entitlements in Gusan/Gusan.entitlements GusanKeyboard/GusanKeyboard.entitlements GusanScreen/GusanScreen.entitlements; do
    plutil -lint "$entitlements"
    if /usr/bin/grep -q '\$(' "$entitlements"; then
        echo "Unresolved build variable in $entitlements" >&2
        exit 1
    fi
done

codesign --force --sign - --entitlements GusanKeyboard/GusanKeyboard.entitlements "$KEYBOARD"
codesign --force --sign - --entitlements GusanScreen/GusanScreen.entitlements "$SCREEN"
codesign --force --sign - "$ACTIVITY"
codesign --force --sign - --entitlements Gusan/Gusan.entitlements "$APP"
codesign --verify --deep --strict "$APP"
codesign -d --entitlements - "$APP"
codesign -d --entitlements - "$KEYBOARD"
codesign -d --entitlements - "$SCREEN"
/usr/libexec/PlistBuddy -c 'Print :NSExtension:NSExtensionPointIdentifier' "$SCREEN/Info.plist"
/usr/libexec/PlistBuddy -c 'Print :NSExtension:NSExtensionPointIdentifier' "$ACTIVITY/Info.plist"
/usr/libexec/PlistBuddy -c 'Print :NSSupportsLiveActivities' "$APP/Info.plist"
/usr/libexec/PlistBuddy -c 'Print :CFBundleURLTypes' "$APP/Info.plist"
/usr/libexec/PlistBuddy -c 'Print :CFBundleShortVersionString' "$APP/Info.plist"
/usr/libexec/PlistBuddy -c 'Print :CFBundleVersion' "$APP/Info.plist"

OUTPUT="$IOS_DIR/build/Gusan-iOS-unsigned.ipa"
rm -f "$OUTPUT"
(cd "$WORK_DIR" && /usr/bin/zip -qry "$OUTPUT" Payload)
echo "Created $OUTPUT"
ls -lh "$OUTPUT"
