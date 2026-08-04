# Dictation quality evidence

Measured 2026-08-04 on the repository's deterministic German text corpus and the
API 36 `google_apis` emulator. This document deliberately separates text-pipeline,
window/UI, and audio evidence: the server has no microphone/audio stack, so none of
the numbers below claim real acoustic recognition quality.

## Recognition gate

`RecognitionQualityGateTest` contains 64 realistic German utterances; 31 are held
as generalization cases (48.4 %, above the required one third). Case and punctuation
are significant in the whitespace-token word error rate (WER). The fixed release
threshold is `WER <= 0.10`.

| Measurement | Edits / reference words | WER | Exact |
|---|---:|---:|---:|
| Previous production pipeline, after adding the corpus | 139 / 238 | 0.5840 | 0 / 64 |
| Current production pipeline | 0 / 238 | 0.0000 | 64 / 64 |

The baseline was captured before changing production code. The current value is a
text-only regression gate, not evidence that Whisper or Android's recognizer heard
the words correctly.

**Read the 0.0000 correctly.** Corpus and pipeline were developed together, so a
perfect score on this corpus measures nothing but the absence of regressions. The
honest number comes from `RecognitionHoldoutTest`, a corpus written separately from
the pipeline and never tuned against it:

| Held-out measurement (2026-08-04) | WER | Exact |
|---|---:|---:|
| Pipeline before the recognition/design round | 0.1774 | 43/72 |
| After prose safety | 0.0948 | 55/72 |
| After German ordinals, magnitudes, colloquial clock forms | 0.0062 | 71/72 |

That corpus carries a ratchet which may only ever move down. Its worst category
before the fix was `prose_guard` at 0.3692 — ordinary German sentences from which
the cleanup deleted words ("Es funktioniert besser jetzt" became "Es jetzt."). The
gate corpus never contained such a case and therefore never saw the defect.
Details and the deliberate remaining miss: [recognition-and-design-iterations.md](recognition-and-design-iterations.md).

### Cumulative stage attribution

The test also measures each cumulative processing stage. The reduction column is the
change from the immediately preceding stage; it is useful for locating regressions,
but it is not an isolated causal estimate.

| Stage | Cumulative WER | Reduction |
|---|---:|---:|
| Raw fixture hypothesis | 0.8866 | — |
| Local filler/correction refinement | 0.6849 | 0.2017 |
| Spoken punctuation | 0.5420 | 0.1429 |
| German numbers, dates, times, codes | 0.5084 | 0.0336 |
| Exact dictionary substitutions | 0.4454 | 0.0630 |
| Canonical-term generalization | 0.4160 | 0.0294 |
| Sentence formatting | 0.0000 | 0.4160 |

The domain-bias path mechanically covers `Hermes`, `PlanSpec`, `Kanban Board`,
`Health Track`, and `Tailscale`. Partial text uses the same safe cleanup and
canonicalization, but does not add terminal punctuation or expand snippets; final
text does.

Run the gate from `android/hermes-dictate`:

```bash
ANDROID_HOME=/home/piet/Android/Sdk \
ANDROID_SDK_ROOT=/home/piet/Android/Sdk \
JAVA_HOME=/home/piet/Android/jdk \
./gradlew :app:testDebugUnitTest --no-daemon
```

The machine-readable figures are emitted as `RecognitionQualityGate` and
`RecognitionQualityStages` in
`app/build/test-results/testDebugUnitTest/TEST-net.hermes.dictate.RecognitionQualityGateTest.xml`.

## Overlay state machine

| State | Visible copy | Behavior |
|---|---|---|
| Idle | `Bereit` | Collapsed bubble; no recording |
| Listening | `Hört zu` / `Cloud hört zu` | Expanded pill; partial text grows; live level bar moves |
| Processing | `Verarbeitet …` | Expanded, busy, input no longer accepted |
| Done | `Fertig` | Success state for 1.2 s, then collapses |
| Failed | concise error | Failure state, then safe reset |

The expanded production layout has 48 dp cancel, Hermes, and confirm targets, a
two-line growing transcript, and a four-dp level bar. The emulator test drives the
exact production layout from `Hermes` to `Hermes erstellt eine PlanSpec`, level 28
to 76, and through Processing/Done. It also asserts the minimum touch geometry.

Instrumentation cannot bind the accessibility service while the Android test runner
owns the target process. Therefore two complementary proofs are used:

1. instrumentation checks the exact production layout/resources and state changes;
2. outside instrumentation, `dumpsys window windows` proves the real service owns an
   on-screen `ACCESSIBILITY_OVERLAY` that grows from 126 × 126 px to a requested
   715 × 158 px.

## Debug-only WAV seam

The seam exists only to make future provider recognition tests deterministic on the
headless server. It is one-shot and fail-closed:

```bash
./gradlew :app:assembleDebug
./scripts/device.sh prepare
./scripts/device.sh inject-wav /absolute/path/to/fixture.wav
```

The next cloud recording reads `files/debug-dictation.wav`, validates the RIFF/WAVE
header and size, deletes the file even when invalid, and uploads it as `audio/wav`.
The normal recorder remains `audio/mp4`. The release source set implements the same
interface as a hard no-op and contains no fixture path or file-reading behavior.
`DebugAudioInputTest` proves valid consumption/deletion and malformed-input failure.

No WAV was transcribed in this run: there is no approved speech fixture/provider
exercise, and synthetic audio would not prove microphone or German acoustic quality.

## Checkpoints

| Checkpoint | Product change | Mechanical evidence |
|---|---|---|
| 1 — corpus | 64 German cases, 31 generalization | baseline 0.5840 captured before production edits |
| 2 — pipeline | fillers, corrections, punctuation, German values, terminology, partial/final split | WER 0.0000; unit gate green |
| 3 — audio seam | debug one-shot WAV; release no-op | debug seam unit tests green |
| 4 — live overlay | level, partial growth, Processing/Done, 48 dp targets | connected tests 2/2; real window-manager geometry |
| 5 — server safety | bias hints retain silence/hallucination filtering; no recognized text in filter log | focused Python regression test |

Open acceptance item: real German microphone/on-device-model quality still needs an
explicit real-device session. `scripts/phone.sh` was not executed during this work.
