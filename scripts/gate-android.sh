#!/usr/bin/env bash
#
# Gate for the Android apps — the layer that had none.
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
# The emulator itself belongs to `scripts/android-emulator.sh`, which pins one
# AVD and one port per app. Until 2026-08-05 this script picked its AVD with
# `emulator -list-avds | head -1` — and since exactly one AVD existed, the deck
# gate silently booted the dictate AVD, the same one the dictate scripts boot.
# Two boots of one AVD is not two emulators.
#
# Like gate-frontend.sh this pipes nothing: the exit code is the truth. Never
# call it through `| tail` — without pipefail that swallows the failure.

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
EMULATOR="$REPO/scripts/android-emulator.sh"
APP="hermes-deck"
RUN_UI=0
RUN_LINT=1
RUN_UNIT=1

usage() {
    cat <<'EOF'
Usage: scripts/gate-android.sh [<app>] [--ui] [--only-ui] [--skip-lint]

  <app>        hermes-deck (Vorgabe) | hermes-dictate | hermes-voice
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
        hermes-deck|hermes-dictate|hermes-voice) APP="$1" ;;
        deck|dictate|voice) APP="hermes-$1" ;;
        *) echo "unbekannte Option: $1" >&2; usage >&2; exit 2 ;;
    esac
    shift
done

MODULE_DIR="$REPO/android/$APP"
[ -d "$MODULE_DIR" ] || { echo "FEHLER: $MODULE_DIR gibt es nicht." >&2; exit 2; }

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

step() { printf '\n=== %s (%s) ===\n' "$1" "$APP"; }

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

    if find "$MODULE_DIR/app/src/androidTest" -name '*.kt' -print -quit 2>/dev/null | rg -q .; then
        :
    else
        echo "FEHLER: $APP hat keine instrumentierten Tests — --ui würde einen" >&2
        echo "        Emulator booten und nichts messen, und das grüne Gate" >&2
        echo "        läse sich wie Abdeckung." >&2
        exit 1
    fi

    # Shut down only what we started: a device that was already attached may be
    # Piet's phone or another session's emulator.
    BOOTED_BY_US=0
    if SERIAL="$("$EMULATOR" serial "$APP" 2>/dev/null)"; then
        echo "→ vorhandenes, durchgebootetes Gerät wird benutzt: $SERIAL"
    else
        SERIAL="$("$EMULATOR" start "$APP")"
        BOOTED_BY_US=1
    fi

    cleanup() {
        if [ "$BOOTED_BY_US" = 1 ]; then
            "$EMULATOR" stop "$APP"
        fi
    }
    trap cleanup EXIT

    # Pin the target: with two apps' emulators possibly up at once, Gradle's
    # "all connected devices" default would run this app's tests on the other
    # app's emulator too.
    ANDROID_SERIAL="$SERIAL" ./gradlew --console=plain connectedDebugAndroidTest
fi

printf '\n=== Gate grün (%s) ===\n' "$APP"
