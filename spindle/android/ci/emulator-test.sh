#!/usr/bin/env bash
# Runs inside the emulator job: installs the APK, lets the app's autotest mode
# load a model and render a still, an MP4 and a GIF, then pulls the results.
set -u
APK="$1"
MODEL="$2"
OUT="$3"
PKG=com.spindle.app
DIR=/sdcard/Android/data/$PKG/files
mkdir -p "$OUT"

adb install -r "$APK" || exit 1
# First launch creates the app's external files folder.
adb shell am start -W -n $PKG/.MainActivity
sleep 8
adb shell mkdir -p $DIR
adb push "$MODEL" $DIR/model.3mf
adb shell am start -n $PKG/.MainActivity --es autotest $DIR/model.3mf

for i in $(seq 1 120); do
  if adb shell ls $DIR/autotest_result.txt >/dev/null 2>&1; then break; fi
  sleep 5
done
adb shell screencap -p /sdcard/spindle_screen.png
adb pull /sdcard/spindle_screen.png "$OUT/screen.png" || true
for f in autotest_result.txt autotest_still.png autotest.mp4 autotest.gif; do
  adb pull $DIR/$f "$OUT/$f" || true
done
adb logcat -d -s Spindle:V AndroidRuntime:E libEGL:V > "$OUT/logcat.txt" || true

echo "---- autotest_result.txt"
cat "$OUT/autotest_result.txt" || { echo "no result file (timed out?)"; tail -50 "$OUT/logcat.txt"; exit 1; }
status=0
for what in still mp4 gif; do
  grep -q "^$what ok" "$OUT/autotest_result.txt" || { echo "FAILED: $what"; status=1; }
done
head -c 8 "$OUT/autotest.mp4" | tail -c 4 | grep -q ftyp || { echo "MP4 has no ftyp box"; status=1; }
exit $status
