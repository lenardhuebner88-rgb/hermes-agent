#!/usr/bin/env bash
# Headless Android emulator for Hermes Dictate.
set -euo pipefail

AVD_NAME="${HERMES_AVD_NAME:-hermes-dictate}"
AVD_PACKAGE="${HERMES_AVD_PACKAGE:-system-images;android-36;google_apis;x86_64}"
AVD_DEVICE="${HERMES_AVD_DEVICE:-pixel_6}"
EMU_PORT="${HERMES_EMU_PORT:-5560}"
SERIAL="emulator-${EMU_PORT}"
BOOT_TIMEOUT="${HERMES_EMU_BOOT_TIMEOUT:-300}"
LOG_DIR="${HERMES_EMU_LOG_DIR:-/mnt/data/android/logs}"

export ANDROID_SDK_ROOT="${ANDROID_SDK_ROOT:-/home/piet/Android/Sdk}"
export ANDROID_HOME="$ANDROID_SDK_ROOT"
export JAVA_HOME="${JAVA_HOME:-/home/piet/Android/jdk}"
export PATH="$JAVA_HOME/bin:$ANDROID_SDK_ROOT/platform-tools:$ANDROID_SDK_ROOT/emulator:$ANDROID_SDK_ROOT/cmdline-tools/latest/bin:$PATH"

if [ ! -w /dev/kvm ] && [ -z "${HERMES_EMU_SG_REEXEC:-}" ]; then
  export HERMES_EMU_SG_REEXEC=1
  exec sg kvm -c "$(printf '%q ' "$0" "$@")"
fi

die() { echo "emu.sh: $*" >&2; exit 1; }

ensure_avd() {
  if avdmanager list avd 2>/dev/null | grep -q "Name: ${AVD_NAME}\$"; then return; fi
  echo "emu.sh: creating AVD ${AVD_NAME} (${AVD_PACKAGE})"
  echo no | avdmanager create avd -n "$AVD_NAME" -k "$AVD_PACKAGE" -d "$AVD_DEVICE" >/dev/null
  local cfg="$HOME/.android/avd/${AVD_NAME}.avd/config.ini"
  {
    echo "hw.ramSize=4096"
    echo "vm.heapSize=512"
    echo "hw.keyboard=yes"
    echo "hw.audioInput=yes"
    echo "disk.dataPartition.size=6G"
  } >>"$cfg"
}

is_running() { adb devices | grep -q "^${SERIAL}[[:space:]]*device$"; }

cmd_start() {
  ensure_avd
  mkdir -p "$LOG_DIR"
  if is_running; then echo "emu.sh: ${SERIAL} already running"; return; fi
  adb start-server >/dev/null 2>&1 || true
  echo "emu.sh: booting ${AVD_NAME} headless on port ${EMU_PORT}"
  nohup emulator -avd "$AVD_NAME" -port "$EMU_PORT" \
    -no-window -no-audio -no-boot-anim -no-snapshot \
    -gpu swiftshader_indirect -accel on \
    >"$LOG_DIR/emulator-${EMU_PORT}.log" 2>&1 &

  local waited=0
  until [ "$(adb -s "$SERIAL" shell getprop sys.boot_completed 2>/dev/null | tr -d '\r')" = "1" ]; do
    sleep 3; waited=$((waited + 3))
    [ "$waited" -ge "$BOOT_TIMEOUT" ] && die "boot timed out after ${BOOT_TIMEOUT}s — see $LOG_DIR/emulator-${EMU_PORT}.log"
  done
  for setting in window_animation_scale transition_animation_scale animator_duration_scale; do
    adb -s "$SERIAL" shell settings put global "$setting" 0 >/dev/null
  done
  adb -s "$SERIAL" shell input keyevent 82 >/dev/null 2>&1 || true
  echo "emu.sh: ${SERIAL} booted (${waited}s)"
}

cmd_stop() {
  is_running || { echo "emu.sh: ${SERIAL} not running"; return; }
  adb -s "$SERIAL" emu kill >/dev/null 2>&1 || true
  sleep 2
  echo "emu.sh: ${SERIAL} stopped"
}

case "${1:-status}" in
  start) cmd_start ;;
  stop) cmd_stop ;;
  restart) cmd_stop; cmd_start ;;
  status) adb devices -l; echo "serial=${SERIAL} avd=${AVD_NAME}" ;;
  logcat) adb -s "$SERIAL" logcat -v time ;;
  serial) echo "$SERIAL" ;;
  *) die "usage: $0 {start|stop|restart|status|logcat|serial}" ;;
esac
