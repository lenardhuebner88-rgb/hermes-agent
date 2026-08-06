#!/usr/bin/env bash
#
# Builds a deliverable Hermes-Deck APK — the step that until now lived only in
# past sessions' heads.
#
# Why this file exists: six APKs (0.1–0.6) were published without the procedure
# being written down anywhere, and on 2026-08-05 that cost an hour. A session
# rebuilt the app with `assembleDebug`, got 43.4 MB against the published
# 33.3 MB, and — after ruling out its own code with a control build of the
# unchanged commit — concluded the *build environment* had drifted. It had not.
# `assembleDebug` and `assembleRelease` are simply different apps:
#
#   debug    11 dex, 43.4 MB, `application-debuggable`, carries ui-tooling
#            (a debugImplementation dependency, several MB on its own)
#   release   3 dex, 34 872 406 bytes — byte-for-byte the published 0.6 minus
#            its ~1.2 kB signature block
#
# So: deliveries are RELEASE builds, signed with the Android debug keystore.
# That keystore is deliberate, not an oversight — every published Hermes APK
# (deck, dictate, voice) carries `CN=Android Debug`, SHA-256 285a89ae…, and a
# different key would make every update refuse to install over the last one.
#
# What this does NOT do: publish. Putting the file where the phone can fetch it
# needs root (`tailscale serve`), and it is the one irreversible step here, so
# it stays a deliberate act. The command is printed at the end.

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# The app is an argument, defaulting to hermes-deck — the shape this script had
# when it was deck-only. It was generalised on 2026-08-06, when hermes-dictate
# needed shipping and turned out to have no delivery path at all: a second copy
# of this file would have drifted from the assertions below within one release.
APP="hermes-deck"
ARTIFACTS="${DECK_ARTIFACTS:-$HOME/Android/artifacts}"
KEYSTORE="${DECK_KEYSTORE:-$HOME/.android/debug.keystore}"
# Every published Hermes APK carries the debug keystore's cert (see header), so
# this one expectation holds for deck, dictate and voice alike.
EXPECTED_CERT="285a89aeba9d0ae422449d00a9fb33fcdb9d1a63e7c7be30c8c28b1de6681f3e"
RUN_GATE=1

usage() {
    cat <<'EOF'
Usage: scripts/release-deck-apk.sh [<app>] [--skip-gate]

  <app>         hermes-deck (Vorgabe) | hermes-dictate | hermes-voice
  --skip-gate   do not run scripts/gate-android.sh <app> --ui first
                (only when it has just run green in this very session)

Env: DECK_ARTIFACTS (default ~/Android/artifacts)
     DECK_KEYSTORE  (default ~/.android/debug.keystore)
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --skip-gate) RUN_GATE=0 ;;
        -h|--help) usage; exit 0 ;;
        hermes-deck|hermes-dictate|hermes-voice) APP="$1" ;;
        *) echo "unbekannte Option: $1" >&2; usage >&2; exit 2 ;;
    esac
    shift
done

MODULE="$REPO/android/$APP"
[ -d "$MODULE" ] || { echo "FEHLER: $MODULE existiert nicht" >&2; exit 1; }

export JAVA_HOME="${JAVA_HOME:-$HOME/Android/jdk}"
export ANDROID_HOME="${ANDROID_HOME:-$HOME/Android/Sdk}"
export PATH="$JAVA_HOME/bin:$ANDROID_HOME/platform-tools:$PATH"

BUILD_TOOLS="$(ls -d "$ANDROID_HOME"/build-tools/*/ 2>/dev/null | sort -V | tail -1)"
[ -n "$BUILD_TOOLS" ] || { echo "FEHLER: keine build-tools unter $ANDROID_HOME" >&2; exit 1; }
APKSIGNER="$BUILD_TOOLS/apksigner"

if [ "$RUN_GATE" = 1 ]; then
    echo "=== Gate (Lint, Unit, instrumentiert) — $APP ==="
    "$REPO/scripts/gate-android.sh" "$APP" --ui
fi

# The filename carries the commit, so an APK on the phone can always be traced
# back to a tree. A dirty tree cannot, hence the warning — not a refusal, since
# a hand-tested build is sometimes exactly what is wanted.
SHA="$(git -C "$REPO" rev-parse --short=10 HEAD)"
if [ -n "$(git -C "$REPO" status --porcelain -- "android/$APP")" ]; then
    echo "WARNUNG: android/$APP ist nicht eingecheckt — $SHA im Dateinamen" \
         "beschreibt diesen Bau NICHT vollständig." >&2
fi

VERSION="$(rg -o 'versionName = "([^"]*)"' --replace '$1' "$MODULE/app/build.gradle.kts" | head -1)"
[ -n "$VERSION" ] || { echo "FEHLER: versionName nicht lesbar" >&2; exit 1; }
echo "=== Release-Bau: Version $VERSION, Commit $SHA ==="

cd "$MODULE"
[ -f local.properties ] || echo "sdk.dir=$ANDROID_HOME" > local.properties
./gradlew --console=plain assembleRelease

UNSIGNED="$MODULE/app/build/outputs/apk/release/app-release-unsigned.apk"
[ -f "$UNSIGNED" ] || { echo "FEHLER: $UNSIGNED fehlt" >&2; exit 1; }

mkdir -p "$ARTIFACTS"
OUT="$ARTIFACTS/$APP-$VERSION-$SHA.apk"
cp "$UNSIGNED" "$OUT"

echo "=== Signieren ($KEYSTORE) ==="
"$APKSIGNER" sign \
    --ks "$KEYSTORE" --ks-pass pass:android \
    --ks-key-alias androiddebugkey --key-pass pass:android \
    "$OUT"

# Three assertions, because the whole point of this script is that "it built"
# is not the same claim as "this is the artefact we mean to ship".
echo "=== Prüfungen ==="
CERT="$("$APKSIGNER" verify --print-certs "$OUT" | rg -o 'SHA-256 digest: ([0-9a-f]+)' --replace '$1' | head -1)"
if [ "$CERT" != "$EXPECTED_CERT" ]; then
    echo "FEHLER: fremdes Zertifikat ($CERT)." >&2
    echo "        Updates über die bisherigen Stände hinweg würden abgelehnt." >&2
    exit 1
fi
echo "→ Zertifikat $CERT ✓ (wie 0.1–0.6)"

if "$BUILD_TOOLS/aapt2" dump badging "$OUT" | rg -q "application-debuggable"; then
    echo "FEHLER: die APK ist debuggable — das ist ein Debug-Bau, kein Release." >&2
    exit 1
fi
echo "→ nicht debuggable ✓"

APK_VERSION="$("$BUILD_TOOLS/aapt2" dump badging "$OUT" | rg -o "versionName='([^']*)'" --replace '$1' | head -1)"
if [ "$APK_VERSION" != "$VERSION" ]; then
    echo "FEHLER: die APK meldet $APK_VERSION, gebaut werden sollte $VERSION." >&2
    echo "        Genau das war der Fehler der Stände 0.1–0.6: der Dateiname log." >&2
    exit 1
fi
echo "→ meldet intern $APK_VERSION ✓"

sha256sum "$OUT" > "$OUT.sha256"
# Readable for the web server: freshly built APKs come out as 0600.
chmod 644 "$OUT"

printf '\n=== Fertig ===\n'
ls -la "$OUT"
cat "$OUT.sha256"
cat <<EOF

Veröffentlichen (braucht root, bewusster Schritt — nicht Teil dieses Skripts):

  cp "$OUT" "$ARTIFACTS/$APP-latest.apk"
  chmod 644 "$ARTIFACTS/$APP-latest.apk"
  sudo tailscale serve --bg --set-path /$APP.apk "$ARTIFACTS/$APP-latest.apk"

Danach gegenprüfen — die Prüfsumme über HTTP, nicht das Dateisystem:

  curl -sL https://huebners.tail50819a.ts.net/$APP.apk | sha256sum
EOF
