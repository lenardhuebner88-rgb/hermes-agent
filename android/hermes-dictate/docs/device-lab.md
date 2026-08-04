# Device lab — Hermes Dictate on the server

Set up and exercised 2026-08-04. The default lane is a deterministic headless API 36
emulator. A real phone is an explicitly human-approved acceptance lane and was not
used in this run.

## Installed layout

| Purpose | Path |
|---|---|
| Android SDK | `/home/piet/Android/Sdk` (backed by `/mnt/data/android/sdk`) |
| JDK 21 | `/home/piet/Android/jdk` |
| AVD data | `/home/piet/.android` (backed by `/mnt/data/android/dot-android`) |
| Emulator logs | `/mnt/data/android/logs` |

The image is `system-images;android-36;google_apis;x86_64`, the AVD is
`hermes-dictate`, and the dedicated serial is `emulator-5560`. The scripts export the
SDK/JDK paths and re-enter the `kvm` group when an older login lacks it.

## Deterministic emulator lane

```bash
cd android/hermes-dictate
./scripts/emu.sh start
./gradlew :app:assembleDebug
./scripts/device.sh prepare
./scripts/device.sh state
./scripts/device.sh scratch
./scripts/device.sh tree /tmp/dictate-tree.xml
./scripts/device.sh shot docs/screenshots/settings.png
./scripts/emu.sh stop
```

`scratch` opens Settings and taps the API 36 search affordance at `(540,190)`;
`KEYCODE_SEARCH` is ignored on that screen. `device.sh` refuses ambiguous device
selection: if several devices exist, set `ANDROID_SERIAL` explicitly.

## Observation channels

| Channel | Can prove | Cannot prove |
|---|---|---|
| `device.sh windows` | overlay type, package, position, size, flags | displayed text |
| `device.sh tree` | focused-window nodes and settings copy | accessibility-overlay nodes |
| `device.sh shot` | pixel artifact for operator review | correctness without human review |

`uiautomator dump` does not include the bubble because the focused window is Settings.
The independent window-manager proof after installing the debug APK was:

```text
package=net.hermes.dictate appop=CREATE_ACCESSIBILITY_OVERLAY
mAttrs={(0,1200)(126x126) ... ty=ACCESSIBILITY_OVERLAY ...}
Requested w=126 h=126
frame=[954,1328][1080,1454] visible=true on-screen=true
```

After tapping the bubble center `(1017,1391)`, the same production window became:

```text
mAttrs={(0,1200)(wrapxwrap) ... ty=ACCESSIBILITY_OVERLAY ...}
Requested w=715 h=158
```

Instrumentation separately drives the exact production pill layout. This split is
intentional: an instrumentation runner owns the app process, so the accessibility
manager reports the component enabled but cannot bind a second service process during
that test.

## Audio boundary and debug injection

The emulator forwards only a host microphone. This server has neither PulseAudio nor
PipeWire, so it cannot feed a WAV into the virtual microphone. Do not infer audio
quality from an emulator tap.

The app-level debug seam avoids that host limitation for future cloud-provider tests:

```bash
./scripts/device.sh inject-wav /absolute/path/to/fixture.wav
```

It stages one validated WAV in the app-private debug directory. The next cloud
recording consumes and deletes it. Release builds use a hard no-op source-set
implementation. See `dictation-quality.md` for the exact contract.

The image exposes Android System Intelligence and Google TTS recognition services.
That proves recognizers exist, not that a German offline model is downloaded or good.

## Real-phone lane — operator only

`scripts/phone.sh` contains an explicit Tailscale pairing/connect helper for the S26.
Use it only with the operator at the phone, then set `ANDROID_SERIAL` before invoking
`device.sh`. Pairing codes, toggling wireless debugging, and unattended real-device
loops are outside the emulator workflow. This helper was never run for the evidence in
this document.
