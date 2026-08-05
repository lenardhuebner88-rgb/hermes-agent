#!/usr/bin/env bash
#
# Gate for android/hermes-deck — the layer that had none.
#
# Four of the six defects found on 2026-08-05 lived in the Compose/Activity
# layer, and every one of them was caught by a human looking at an emulator.
# `scripts/gate-frontend.sh` covers web/, nothing covered android/.
#
# Legs, in order of cost:
#   lint    Android Lint over the app
#   unit    JVM unit tests (fast, no device)
#   ui      instrumented Compose tests on an emulator (slow, needs KVM)
#
# By default lint+unit run. `--ui` adds the emulator leg.
#
# Like gate-frontend.sh this pipes nothing: the exit code is the truth. Never
# call it through `| tail` — without pipefail that swallows the failure.

set -euo pipefail

MODULE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/android/hermes-deck"
RUN_UI=0
RUN_LINT=1
RUN_UNIT=1

usage() {
    cat <<'EOF'
Usage: scripts/gate-android.sh [--ui] [--only-ui] [--skip-lint]

  --ui         also run instrumented Compose tests on an emulator
  --only-ui    run only the emulator leg
  --skip-lint  skip Android Lint (it is the slowest of the two cheap legs)
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --ui) RUN_UI=1 ;;
        --only-ui) RUN_UI=1; RUN_LINT=0; RUN_UNIT=0 ;;
        --skip-lint) RUN_LINT=0 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "unbekannte Option: $1" >&2; usage >&2; exit 2 ;;
    esac
    shift
done

# The harness shell reads neither ~/.bashrc nor ~/.profile, so none of this can
# be assumed to be set. Without JAVA_HOME/bin on PATH Gradle dies as
# "exec: java: not found", which names nothing useful.
export JAVA_HOME="${JAVA_HOME:-$HOME/Android/jdk}"
export ANDROID_HOME="${ANDROID_HOME:-$HOME/Android/Sdk}"
export ANDROID_SDK_ROOT="$ANDROID_HOME"
export PATH="$JAVA_HOME/bin:$ANDROID_HOME/platform-tools:$ANDROID_HOME/emulator:$PATH"

cd "$MODULE_DIR"

# local.properties is git-ignored, so a fresh worktree has none and Gradle
# fails with "SDK location not found" before running a single task.
if [ ! -f local.properties ]; then
    echo "sdk.dir=$ANDROID_HOME" > local.properties
    echo "→ local.properties angelegt (sdk.dir=$ANDROID_HOME)"
fi

step() { printf '\n=== %s ===\n' "$1"; }

if [ "$RUN_LINT" = 1 ]; then
    step "Android Lint"
    ./gradlew --console=plain lintDebug
fi

if [ "$RUN_UNIT" = 1 ]; then
    step "Unit-Tests (JVM)"
    ./gradlew --console=plain testDebugUnitTest
fi

if [ "$RUN_UI" = 1 ]; then
    step "Instrumentierte Compose-Tests"

    # Piet is in group kvm, but a shell started before that change was made does
    # not carry the supplementary group — measured: direct access to /dev/kvm is
    # denied while `sg kvm` succeeds. So re-exec through sg rather than assume.
    if [ ! -r /dev/kvm ] || [ ! -w /dev/kvm ]; then
        if ! sg kvm -c 'test -r /dev/kvm && test -w /dev/kvm' 2>/dev/null; then
            echo "FEHLER: kein Zugriff auf /dev/kvm, auch nicht über 'sg kvm'." >&2
            echo "        Ohne KVM startet der Emulator nicht (oder braucht Stunden)." >&2
            exit 1
        fi
        echo "→ /dev/kvm nur über 'sg kvm' erreichbar; starte den Emulator so."
        SG_PREFIX=(sg kvm -c)
    else
        SG_PREFIX=()
    fi

    AVD="${DECK_AVD:-$(emulator -list-avds | head -1)}"
    if [ -z "$AVD" ]; then
        echo "FEHLER: keine AVD vorhanden. Anlegen z.B. mit:" >&2
        echo "  \$ANDROID_HOME/cmdline-tools/latest/bin/avdmanager create avd \\" >&2
        echo "      -n hermes-deck -k 'system-images;android-36;google_apis;x86_64'" >&2
        exit 1
    fi
    echo "→ AVD: $AVD"

    # "Is a device there" is not the same question as "can it run a test".
    #
    # `adb devices` keeps listing an emulator that is still tearing down, so a
    # run started right after a previous one saw a device, skipped booting, and
    # then died in Gradle as "No connected devices!" — measured, not imagined.
    # A device only counts once it reports sys.boot_completed.
    usable_device() {
        local serial
        for serial in $(adb devices | awk 'NR>1 && $2=="device" {print $1}'); do
            if [ "$(adb -s "$serial" shell getprop sys.boot_completed 2>/dev/null | tr -d '\r')" = "1" ]; then
                echo "$serial"
                return 0
            fi
        done
        return 1
    }

    BOOTED_BY_US=0
    if ! usable_device >/dev/null; then
        echo "→ starte Emulator headless …"
        LOG=$(mktemp -t deck-emulator-XXXXXX.log)
        EMU_CMD="emulator -avd $AVD -no-window -no-audio -no-boot-anim -gpu swiftshader_indirect"
        if [ ${#SG_PREFIX[@]} -gt 0 ]; then
            "${SG_PREFIX[@]}" "$EMU_CMD" > "$LOG" 2>&1 &
        else
            $EMU_CMD > "$LOG" 2>&1 &
        fi
        BOOTED_BY_US=1

        echo "→ warte auf Boot (Log: $LOG) …"
        adb wait-for-device
        for _ in $(seq 1 180); do
            usable_device >/dev/null && break
            sleep 2
        done
        if ! usable_device >/dev/null; then
            echo "FEHLER: Emulator ist nicht durchgebootet. Log: $LOG" >&2
            exit 1
        fi
        echo "→ gebootet: $(usable_device)"
    else
        echo "→ vorhandenes, durchgebootetes Gerät wird benutzt: $(usable_device)"
    fi

    # Shut down only what we started: a device that was already attached may be
    # Piet's phone or another session's emulator.
    cleanup() {
        if [ "$BOOTED_BY_US" = 1 ]; then
            echo "→ fahre den selbst gestarteten Emulator herunter."
            adb emu kill >/dev/null 2>&1 || true
        fi
    }
    trap cleanup EXIT

    ./gradlew --console=plain connectedDebugAndroidTest
fi

printf '\n=== Gate grün ===\n'
