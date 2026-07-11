# Hermes Voice Android

## Scope

This directory is a standalone Android/Gradle project for the Hermes Voice shell.
Keep Android-specific changes here; the served voice client lives in
`hermes_cli/voice_client/` and its backend protocol lives in the Python tree.

## Module map

- `app/src/main/kotlin/net/hermes/voice/MainActivity.kt` owns the origin-locked
  WebView, runtime permissions, bridge dispatch, and confirmed phone actions.
- `HermesBridge.kt`, `BridgeProtocol.kt`, and `BridgeGenerationGate.kt` own the
  versioned WebView/native message boundary and stale-reply protection.
- `MediaProjectionService.kt` owns foreground screen capture and frame delivery.
- `CaptureStateMachine.kt` and `CaptureSurfaceSwap.kt` serialize capture lifecycle
  changes; `FrameScaler.kt` bounds image work.
- Pure JVM tests are under `app/src/test/kotlin/net/hermes/voice/`.

## Build and test

Run commands from this directory:

```bash
./gradlew :app:compileDebugKotlin
./gradlew :app:testDebugUnitTest
./gradlew :app:assembleDebug
```

Use `compileDebugKotlin` as the worker gate. `assembleDebug` is the local APK
build; do not substitute `assembleRelease` or upgrade Gradle as drive-by work.

## Load-bearing constraints

- The WebView bridge is origin-scoped, main-frame-only, versioned, and optional
  when `WEB_MESSAGE_LISTENER` is unavailable. Never add a broad JavaScript
  interface or accept messages from arbitrary origins.
- Native-to-web sends must remain generation-gated: an old reply proxy must not
  receive capture frames or phone-action results after detach/reconnect.
- Capture start/stop, permission callbacks, Activity teardown, renderer death,
  and service teardown race. Keep transitions in the existing state machine and
  make cleanup idempotent.
- Phone actions are side effects: preserve explicit confirmation, expiry checks,
  session invalidation, and fail-closed behavior.
- Never log bridge payloads, captured frames, dictated content, or credentials.
