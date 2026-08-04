#!/usr/bin/env bash
# Prepare and inspect Hermes Dictate on one adb device. Never selects the real phone implicitly.
set -euo pipefail

APP_ID="net.hermes.dictate"
IME_ID="${APP_ID}/.DictateInputMethodService"
A11Y_ID="${APP_ID}/${APP_ID}.DictateOverlayService"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APK="${HERMES_DICTATE_APK:-$HERE/app/build/outputs/apk/debug/app-debug.apk}"

export ANDROID_SDK_ROOT="${ANDROID_SDK_ROOT:-/home/piet/Android/Sdk}"
export ANDROID_HOME="$ANDROID_SDK_ROOT"
export JAVA_HOME="${JAVA_HOME:-/home/piet/Android/jdk}"
export PATH="$JAVA_HOME/bin:$ANDROID_SDK_ROOT/platform-tools:$PATH"

die() { echo "device.sh: $*" >&2; exit 1; }

resolve_serial() {
  if [ -n "${ANDROID_SERIAL:-}" ]; then echo "$ANDROID_SERIAL"; return; fi
  local list count
  list="$(adb devices | awk '$2=="device"{print $1}')"
  count="$(printf '%s\n' "$list" | grep -c . || true)"
  [ "$count" -eq 0 ] && die "no device attached (start the emulator with scripts/emu.sh start)"
  [ "$count" -gt 1 ] && die "several devices attached — set ANDROID_SERIAL explicitly"
  printf '%s' "$list"
}
SERIAL="$(resolve_serial)"
a() { adb -s "$SERIAL" "$@"; }

cmd_prepare() {
  [ -f "$APK" ] || die "no debug APK at $APK — run ./gradlew :app:assembleDebug first"
  echo "device.sh: installing $(basename "$APK") on $SERIAL"
  a install -r -g "$APK"
  a shell ime enable "$IME_ID"
  a shell ime set "$IME_ID"
  enable_accessibility
  cmd_state
}

enable_accessibility() {
  local existing confirmed attempt
  # API 36 may asynchronously scrub the first write immediately after an instrumentation
  # uninstall. Treat the read-back as truth and retry only this exact emulator service.
  for attempt in 1 2 3; do
    a shell settings put secure accessibility_enabled 1
    existing="$(a shell settings get secure enabled_accessibility_services | tr -d '\r')"
    case "$existing" in
      *"$A11Y_ID"*) ;;
      null|"") a shell settings put secure enabled_accessibility_services "$A11Y_ID" ;;
      *) a shell settings put secure enabled_accessibility_services "${existing}:${A11Y_ID}" ;;
    esac
    sleep 0.5
    confirmed="$(a shell settings get secure enabled_accessibility_services | tr -d '\r')"
    case "$confirmed" in
      *"$A11Y_ID"*) return 0 ;;
    esac
  done
  die "accessibility service did not remain enabled after $attempt attempts"
}

cmd_state() {
  echo "serial:        $SERIAL"
  echo "installed:     $(a shell pm list packages "$APP_ID" | tr -d '\r' | head -1)"
  echo "ime default:   $(a shell settings get secure default_input_method | tr -d '\r')"
  echo "ime enabled:   $(a shell ime list -s | tr -d '\r' | tr '\n' ' ')"
  echo "a11y enabled:  $(a shell settings get secure enabled_accessibility_services | tr -d '\r')"
  echo "a11y master:   $(a shell settings get secure accessibility_enabled | tr -d '\r')"
  echo "mic granted:   $(a shell dumpsys package "$APP_ID" | grep -m1 RECORD_AUDIO | tr -d '\r' | xargs || echo '(none)')"
  echo "a11y running:  $(a shell dumpsys accessibility | grep -c "$APP_ID" | tr -d '\r') references in dumpsys"
}

cmd_tree() {
  local out="${1:-/tmp/dictate-tree.xml}"
  a shell uiautomator dump /sdcard/window_dump.xml >/dev/null
  a pull /sdcard/window_dump.xml "$out" >/dev/null
  echo "$out"
}

cmd_shot() {
  local out="${1:-/tmp/dictate-shot.png}"
  a exec-out screencap -p >"$out"
  echo "$out"
}

cmd_scratch() {
  a shell am start -a android.settings.SETTINGS >/dev/null
  sleep 1
  # API 36 ignores KEYCODE_SEARCH on the Settings homepage. Its stable search affordance spans
  # the top app bar; tapping its centre opens and focuses the real EditText deterministically.
  a shell input tap 540 190 >/dev/null
  sleep 1
  echo "device.sh: settings search field focused on $SERIAL"
}

cmd_inject_wav() {
  local wav="${1:?usage: device.sh inject-wav <fixture.wav>}"
  [ -f "$wav" ] || die "WAV fixture not found: $wav"
  a push "$wav" /data/local/tmp/hermes-dictation.wav >/dev/null
  a shell run-as "$APP_ID" cp /data/local/tmp/hermes-dictation.wav "files/debug-dictation.wav"
  a shell rm /data/local/tmp/hermes-dictation.wav
  echo "device.sh: one-shot debug WAV armed on $SERIAL"
}

case "${1:-state}" in
  prepare) cmd_prepare ;;
  state) cmd_state ;;
  tree) cmd_tree "${2:-}" ;;
  shot) cmd_shot "${2:-}" ;;
  scratch) cmd_scratch ;;
  inject-wav) cmd_inject_wav "${2:-}" ;;
  windows) a shell dumpsys window windows ;;
  logcat-dictate) a logcat -v time --pid="$(a shell pidof "$APP_ID" | tr -d '\r')" ;;
  serial) echo "$SERIAL" ;;
  *) die "usage: $0 {prepare|state|tree|shot|scratch|inject-wav|windows|logcat-dictate|serial}" ;;
esac
