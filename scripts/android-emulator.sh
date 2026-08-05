#!/usr/bin/env bash
#
# One headless Android emulator, per app, for the whole repo.
#
# Why this file exists: two sessions built this independently on 2026-08-05 —
# `scripts/gate-android.sh` for hermes-deck and `android/hermes-dictate/scripts/emu.sh`
# for hermes-dictate — and both booted the SAME single AVD (`hermes-dictate`, the
# only one that existed) on different ports. Two boots of one AVD is not two
# emulators; the second one loses. Running them at the same time therefore looked
# like a flaky gate.
#
# So the AVD is derived from the app, never picked up off the floor:
#
#   app            AVD              console port
#   hermes-deck    hermes-deck      5554
#   hermes-dictate hermes-dictate   5560   (kept — the dictate scripts assume it)
#   hermes-voice   hermes-voice     5562
#
# Everything else here is the environment the server does not provide: the JDK
# and SDK are not on PATH (the harness shell reads neither ~/.bashrc nor
# ~/.profile), and /dev/kvm needs the `kvm` group.
#
# Usage:
#   scripts/android-emulator.sh start <app>    boot headless, wait for boot, print serial
#   scripts/android-emulator.sh serial <app>   print the serial if it is usable, else exit 1
#   scripts/android-emulator.sh stop <app>     shut it down
#   scripts/android-emulator.sh ports          print the app->AVD/port table

set -euo pipefail

export JAVA_HOME="${JAVA_HOME:-$HOME/Android/jdk}"
export ANDROID_HOME="${ANDROID_HOME:-$HOME/Android/Sdk}"
export ANDROID_SDK_ROOT="$ANDROID_HOME"
export PATH="$JAVA_HOME/bin:$ANDROID_HOME/platform-tools:$ANDROID_HOME/emulator:$ANDROID_HOME/cmdline-tools/latest/bin:$PATH"

SYSTEM_IMAGE="${HERMES_AVD_PACKAGE:-system-images;android-36;google_apis;x86_64}"
AVD_DEVICE="${HERMES_AVD_DEVICE:-pixel_6}"

app_avd() {
    case "$1" in
        hermes-deck|deck) echo "hermes-deck 5554" ;;
        hermes-dictate|dictate) echo "hermes-dictate 5560" ;;
        hermes-voice|voice) echo "hermes-voice 5562" ;;
        *) echo "unbekannte App: $1 (hermes-deck | hermes-dictate | hermes-voice)" >&2; return 2 ;;
    esac
}

# "Is a device there" is not the same question as "can it run a test": adb keeps
# listing an emulator that is still tearing down, and a run started right after a
# previous one then died in Gradle as "No connected devices!". A device counts
# only once it reports sys.boot_completed.
usable_serial() {
    local serial="emulator-$1"
    [ "$(adb -s "$serial" shell getprop sys.boot_completed 2>/dev/null | tr -d '\r')" = "1" ]
}

ensure_kvm() {
    if [ -r /dev/kvm ] && [ -w /dev/kvm ]; then
        SG_PREFIX=()
        return 0
    fi
    # Piet IS in group kvm, but a login session older than the group change does
    # not carry the supplementary group — measured: direct access fails, `sg kvm`
    # succeeds. So re-exec through sg rather than declare KVM missing.
    if sg kvm -c 'test -r /dev/kvm && test -w /dev/kvm' 2>/dev/null; then
        SG_PREFIX=(sg kvm -c)
        return 0
    fi
    echo "FEHLER: kein Zugriff auf /dev/kvm, auch nicht über 'sg kvm'." >&2
    echo "        Ohne KVM startet der Emulator nicht (oder braucht Stunden)." >&2
    return 1
}

ensure_avd() {
    local avd="$1"
    if emulator -list-avds | rg -qx "$avd"; then
        return 0
    fi
    # Everything in this file writes to stderr except the serial itself: the
    # caller does `SERIAL="$(… start …)"`, and the first version let the AVD
    # creation chatter into that capture. Gradle then got a device name made of
    # three log lines and died as "'→ AVD 'hermes-deck' fehlt' not found!".
    echo "→ AVD '$avd' fehlt, lege sie an ($SYSTEM_IMAGE, $AVD_DEVICE) …" >&2
    # `system-images;android-36;…` is an sdkmanager coordinate, not a path: the
    # separators become slashes and the leading segment is already the directory.
    if [ ! -d "$ANDROID_HOME/${SYSTEM_IMAGE//;//}" ]; then
        echo "FEHLER: System-Image '$SYSTEM_IMAGE' ist nicht installiert." >&2
        echo "        \$ANDROID_HOME/cmdline-tools/latest/bin/sdkmanager '$SYSTEM_IMAGE'" >&2
        return 1
    fi
    echo no | avdmanager create avd -n "$avd" -k "$SYSTEM_IMAGE" -d "$AVD_DEVICE" --force >&2
}

# `read <<<"$(...)"` swallows a failing substitution: an unknown app produced an
# empty AVD name and the script marched on to create it. So resolve first, check,
# then read.
resolve() {
    local spec
    spec="$(app_avd "$1")" || exit 2
    read -r AVD PORT <<<"$spec"
    [ -n "$AVD" ] && [ -n "$PORT" ] || { echo "FEHLER: App '$1' nicht aufgelöst." >&2; exit 2; }
}

cmd_start() {
    local avd port
    resolve "$1"; avd="$AVD"; port="$PORT"

    if usable_serial "$port"; then
        echo "→ läuft bereits und ist durchgebootet: emulator-$port" >&2
        echo "emulator-$port"
        return 0
    fi

    ensure_kvm
    ensure_avd "$avd"

    local log
    log="$(mktemp -t "hermes-emulator-$avd-XXXXXX.log")"
    local cmd="emulator -avd $avd -port $port -no-window -no-audio -no-boot-anim -gpu swiftshader_indirect"
    echo "→ starte $avd headless auf Port $port (Log: $log) …" >&2
    if [ ${#SG_PREFIX[@]} -gt 0 ]; then
        "${SG_PREFIX[@]}" "$cmd" > "$log" 2>&1 &
    else
        $cmd > "$log" 2>&1 &
    fi

    adb wait-for-device >/dev/null 2>&1 || true
    local i
    for i in $(seq 1 180); do
        usable_serial "$port" && break
        sleep 2
    done
    if ! usable_serial "$port"; then
        echo "FEHLER: $avd ist nicht durchgebootet. Log: $log" >&2
        return 1
    fi
    echo "→ gebootet: emulator-$port" >&2
    echo "emulator-$port"
}

cmd_serial() {
    local avd port
    resolve "$1"; avd="$AVD"; port="$PORT"
    usable_serial "$port" || return 1
    echo "emulator-$port"
}

cmd_stop() {
    local avd port
    resolve "$1"; avd="$AVD"; port="$PORT"
    adb -s "emulator-$port" emu kill >/dev/null 2>&1 || true
    echo "→ emulator-$port heruntergefahren." >&2
}

cmd_ports() {
    printf '%-16s %-16s %s\n' App AVD Port
    local a
    for a in hermes-deck hermes-dictate hermes-voice; do
        # shellcheck disable=SC2046
        printf '%-16s %-16s %s\n' "$a" $(app_avd "$a")
    done
}

case "${1:-}" in
    start) shift; cmd_start "${1:?App fehlt}" ;;
    serial) shift; cmd_serial "${1:?App fehlt}" ;;
    stop) shift; cmd_stop "${1:?App fehlt}" ;;
    ports) cmd_ports ;;
    *) rg '^#( |$)' "$0" | sed 's/^# \?//' >&2; exit 2 ;;
esac
