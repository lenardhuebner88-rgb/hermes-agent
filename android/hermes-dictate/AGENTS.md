# Hermes Dictate Android

## Scope

This directory is a standalone Android/Gradle project for the system-wide Hermes
dictation IME and accessibility overlay. Keep changes privacy-preserving and
fail closed around field focus, audio, and cloud opt-in.

## Module map

- `app/src/main/kotlin/net/hermes/dictate/DictateInputMethodService.kt` adapts
  `DictationController` commands to the Android IME and `InputConnection`.
- `DictateOverlayService.kt` owns the draggable accessibility bubble/pill,
  focused-field tracking, and overlay commit path.
- `DictationController.kt` is the pure lifecycle/state core; recorder,
  recognizer, formatter, and overlay-state helpers stay independently testable.
- `AccessibilityNodeCommitter.kt` and `TextSplicer.kt` guard commits into
  accessibility-backed fields and their clipboard fallback.
- `SettingsActivity.kt`, `LoginActivity.kt`, and `AndroidNet.kt` own setup,
  origin-locked WebView login, and the shared cookie-backed cloud session.
- Pure JVM tests are under `app/src/test/kotlin/net/hermes/dictate/`.

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

- Overlay window replacement temporarily perturbs Android focus. Preserve the
  delayed focus-loss confirmation and keep the active pill visible through
  transient `TYPE_WINDOWS_CHANGED` events.
- A dictation started in field A must never commit into field B. Re-query live
  focus at commit time, reject a different node, and treat cached nodes as stale.
- IME teardown callbacks and late recognizer/upload callbacks race; lifecycle
  cancellation must stay idempotent and stale callbacks must become no-ops.
- On-device mode must not silently fall back to a network recognizer. Cloud audio
  is allowed only after explicit per-use opt-in and must never be logged.
- WebView login remains origin-locked on every navigation; cookies are shared
  only through the existing `WebViewCookieStore` boundary.
