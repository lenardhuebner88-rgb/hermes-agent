package net.hermes.dictate

import android.content.Context
import android.content.Intent
import android.os.Build
import android.os.Bundle
import android.speech.RecognitionListener
import android.speech.RecognizerIntent
import android.speech.SpeechRecognizer

/**
 * Factory for the [Intent] passed to [SpeechRecognizer.startListening].
 * Extracted so unit tests can capture and assert the extras without an Android runtime.
 */
fun interface RecognizeIntentFactory {
    fun create(language: String): Intent
}

/**
 * Default recognize intent with the evidence-backed German/S24 quality defaults.
 *
 * Extras that only exist on newer SDK levels are feature-detected via [sdkInt] and gracefully
 * omitted on older devices, so an unsupported extra can never wedge the recognizer. The German
 * language is selected explicitly by the caller (blank falls back to `de-DE` in [OnDeviceDictation]).
 */
class DefaultRecognizeIntentFactory(
    private val sdkInt: Int = Build.VERSION.SDK_INT,
    private val callingPackage: String? = null,
) : RecognizeIntentFactory {
    override fun create(language: String): Intent {
        return Intent(RecognizerIntent.ACTION_RECOGNIZE_SPEECH).apply {
            putExtra(RecognizerIntent.EXTRA_LANGUAGE_MODEL, RecognizerIntent.LANGUAGE_MODEL_FREE_FORM)
            putExtra(RecognizerIntent.EXTRA_LANGUAGE, language)
            // Some engines consult EXTRA_LANGUAGE_PREFERENCE as the fallback locale hint.
            putExtra(RecognizerIntent.EXTRA_LANGUAGE_PREFERENCE, language)
            putExtra(RecognizerIntent.EXTRA_PARTIAL_RESULTS, true)
            // Belt-and-braces alongside the dedicated on-device recognizer: never networked models.
            putExtra(RecognizerIntent.EXTRA_PREFER_OFFLINE, true)
            callingPackage?.let { putExtra(RecognizerIntent.EXTRA_CALLING_PACKAGE, it) }
            // Dictation pauses (thinking) shouldn't end a segment instantly; the controller chains
            // segments for longer gaps.
            putExtra(RecognizerIntent.EXTRA_SPEECH_INPUT_COMPLETE_SILENCE_LENGTH_MILLIS, 1600)
            putExtra(RecognizerIntent.EXTRA_SPEECH_INPUT_POSSIBLY_COMPLETE_SILENCE_LENGTH_MILLIS, 1600)
            // Automatic punctuation/capitalization ("formatting") is a String extra added in API 33
            // (value FORMATTING_OPTIMIZE_QUALITY|LATENCY). Older devices simply don't get it — a raw
            // int here (the previous value) is not a valid value for this extra and is ignored.
            if (sdkInt >= 33) {
                putExtra(
                    RecognizerIntent.EXTRA_ENABLE_FORMATTING,
                    RecognizerIntent.FORMATTING_OPTIMIZE_QUALITY,
                )
            }
        }
    }
}

/**
 * Creates a fresh recognizer instance, or null when on-device recognition is unavailable.
 *
 * A *factory* (not a single pre-built instance) is required so [OnDeviceDictation.recreate] can
 * actually destroy a wedged recognizer and bind a brand-new one — the crux of the repeated-dictation
 * fix. Tests supply a lambda returning mocks; production supplies [OnDeviceRecognizerFactory].
 */
fun interface RecognizerFactory {
    fun create(): SpeechRecognizer?
}

/**
 * Real on-device recognizer factory.
 *
 * Privacy contract (PlanSpec): the on-device path must never stream audio off the device, so ONLY
 * the dedicated on-device recognizer (API 31+) is ever bound. The generic recognizer is documented
 * to send audio to a server and `EXTRA_PREFER_OFFLINE` is merely best-effort, which would break the
 * guarantee silently (Codex review finding, 2026-07-10). When on-device recognition is unavailable
 * the factory returns null and the failure surfaces visibly; there is no networked fallback.
 */
class OnDeviceRecognizerFactory(private val context: Context) : RecognizerFactory {
    override fun create(): SpeechRecognizer? {
        if (!isAvailable(context)) return null
        return SpeechRecognizer.createOnDeviceSpeechRecognizer(context)
    }

    companion object {
        fun isAvailable(context: Context): Boolean =
            Build.VERSION.SDK_INT >= 31 && SpeechRecognizer.isOnDeviceRecognitionAvailable(context)
    }
}

/**
 * On-device recognizer backed by Android's [SpeechRecognizer].
 *
 * Lifecycle semantics:
 * - The [SpeechRecognizer] instance is created lazily on the first segment via [recognizerFactory]
 *   and reused for chained segments. [recreate] destroys it and drops the reference so the next
 *   segment binds a genuinely fresh instance (wedged-BUSY recovery); [destroy] tears it down for good.
 * - A *segment* is one continuous listen -> terminal callback cycle. Only one segment may be pending
 *   at a time ([pendingSegment]); this does NOT gate starting the next segment in the same field,
 *   otherwise repeated dictations die after the first segment.
 * - Every listener is stamped with the [generation] it was bound in. [recreate]/[destroy] bump the
 *   generation, so a stale or late callback from an older recognizer instance is dropped: it can
 *   neither commit into, corrupt, nor terminate a newer session.
 */
class OnDeviceDictation(
    private val recognizerFactory: RecognizerFactory,
    private val callbacks: Callbacks,
    private val intentFactory: RecognizeIntentFactory = DefaultRecognizeIntentFactory(),
) {
    interface Callbacks {
        fun onPartial(text: String)
        fun onFinal(text: String)
        fun onError(failure: RecognizerFailure)
    }

    private var recognizer: SpeechRecognizer? = null
    private var pendingSegment = false
    private var destroyed = false

    /** Bumped on every teardown so late callbacks from an older recognizer are recognized as stale. */
    private var generation = 0

    /** False only after [destroy]; the service builds a fresh wrapper per input session. */
    val isAvailable: Boolean get() = !destroyed

    /**
     * Start a new recognition segment. Returns false when a segment is already pending, the wrapper
     * was destroyed, or no on-device recognizer could be bound (the caller surfaces that as a
     * visible failure — there is no networked fallback).
     */
    fun startSegment(languageTag: String): Boolean {
        if (destroyed || pendingSegment) return false
        val r = boundRecognizer() ?: return false
        pendingSegment = true
        r.startListening(intentFactory.create(languageTag.ifBlank { "de-DE" }))
        return true
    }

    fun stopSegment() {
        if (!pendingSegment) return
        recognizer?.stopListening()
    }

    /**
     * Destroy the current recognizer and drop it so the next [startSegment] binds a fresh one.
     * Used for wedged-BUSY recovery. Any late callback from the destroyed instance is ignored
     * because the generation has moved on.
     */
    fun recreate() {
        generation += 1
        pendingSegment = false
        recognizer?.destroy()
        recognizer = null
    }

    fun destroy() {
        generation += 1
        destroyed = true
        pendingSegment = false
        recognizer?.destroy()
        recognizer = null
    }

    private fun boundRecognizer(): SpeechRecognizer? {
        recognizer?.let { return it }
        val created = recognizerFactory.create() ?: return null
        created.setRecognitionListener(Listener(generation))
        recognizer = created
        return created
    }

    private inner class Listener(private val boundGeneration: Int) : RecognitionListener {
        /** A callback is stale once the wrapper is destroyed or the recognizer has been recreated. */
        private val isStale: Boolean get() = destroyed || boundGeneration != generation

        override fun onReadyForSpeech(params: Bundle?) {}
        override fun onBeginningOfSpeech() {}
        override fun onRmsChanged(rmsdB: Float) {}
        override fun onBufferReceived(buffer: ByteArray?) {}
        override fun onEndOfSpeech() {}

        override fun onResults(results: Bundle?) {
            if (isStale) return
            pendingSegment = false
            val text = firstResult(results)
            if (text != null) callbacks.onFinal(text) else callbacks.onError(RecognizerFailure.NO_SPEECH)
        }

        override fun onPartialResults(partialResults: Bundle?) {
            if (isStale) return
            firstResult(partialResults)?.let { callbacks.onPartial(it) }
        }

        override fun onError(error: Int) {
            if (isStale) return
            pendingSegment = false
            callbacks.onError(mapError(error))
        }

        override fun onEvent(eventType: Int, params: Bundle?) {}
    }

    private fun firstResult(bundle: Bundle?): String? =
        bundle?.getStringArrayList(SpeechRecognizer.RESULTS_RECOGNITION)
            ?.firstOrNull()
            ?.takeIf { it.isNotBlank() }

    private fun mapError(error: Int): RecognizerFailure = when (error) {
        SpeechRecognizer.ERROR_NO_MATCH,
        SpeechRecognizer.ERROR_SPEECH_TIMEOUT -> RecognizerFailure.NO_SPEECH
        SpeechRecognizer.ERROR_INSUFFICIENT_PERMISSIONS -> RecognizerFailure.PERMISSION
        SpeechRecognizer.ERROR_LANGUAGE_NOT_SUPPORTED,
        SpeechRecognizer.ERROR_LANGUAGE_UNAVAILABLE -> RecognizerFailure.LANGUAGE_UNAVAILABLE
        SpeechRecognizer.ERROR_RECOGNIZER_BUSY -> RecognizerFailure.BUSY
        else -> RecognizerFailure.OTHER
    }
}
