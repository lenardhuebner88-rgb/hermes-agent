package net.hermes.dictate

import android.content.Intent
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

private object DefaultRecognizeIntentFactory : RecognizeIntentFactory {
    override fun create(language: String): Intent {
        return Intent(RecognizerIntent.ACTION_RECOGNIZE_SPEECH).apply {
            putExtra(RecognizerIntent.EXTRA_LANGUAGE_MODEL, RecognizerIntent.LANGUAGE_MODEL_FREE_FORM)
            putExtra(RecognizerIntent.EXTRA_LANGUAGE, language)
            putExtra(RecognizerIntent.EXTRA_PARTIAL_RESULTS, true)
            putExtra(RecognizerIntent.EXTRA_MAX_RESULTS, 3)
            // S24/German on-device quality: ask for spoken punctuation marks where the platform
            // supports them; unsupported extras are ignored, so this is safe. 0 is not a documented
            // constant for this extra on all SDK levels, so pass it as a raw int.
            putExtra(RecognizerIntent.EXTRA_ENABLE_FORMATTING, 0)
            // Short silence windows for responsive keyboard feel while still allowing short pauses.
            putExtra(RecognizerIntent.EXTRA_SPEECH_INPUT_COMPLETE_SILENCE_LENGTH_MILLIS, 500)
            putExtra(RecognizerIntent.EXTRA_SPEECH_INPUT_POSSIBLY_COMPLETE_SILENCE_LENGTH_MILLIS, 250)
            putExtra(RecognizerIntent.EXTRA_SPEECH_INPUT_MINIMUM_LENGTH_MILLIS, 300)
            // Prefer the on-device model if the device supports it.
            putExtra(RecognizerIntent.EXTRA_PREFER_OFFLINE, true)
        }
    }
}

/**
 * On-device recognizer backed by Android's [SpeechRecognizer].
 *
 * Lifecycle semantics:
 * - The [SpeechRecognizer] instance is created once and reused until [recreate] or [destroy].
 * - A *segment* is one continuous listen -> final result cycle. Only one segment may be pending at a
 *   time; this is tracked by [pendingSegment], not by the long-lived recognizer availability flag.
 * - [isRunning] tracks whether the recognizer instance itself is bound and usable. It is set during
 *   construction and cleared on [destroy]; it must NOT be used to reject the start of a new segment,
 *   otherwise repeated dictations in the same input field die after the first segment.
 */
class OnDeviceDictation(
    private val recognizer: SpeechRecognizer,
    private val callbacks: Callbacks,
    private val intentFactory: RecognizeIntentFactory = DefaultRecognizeIntentFactory,
) {
    interface Callbacks {
        fun onPartial(text: String)
        fun onFinal(text: String)
        fun onError(failure: RecognizerFailure)
    }

    private var isRunning = false
    private var pendingSegment = false

    init {
        recognizer.setRecognitionListener(Listener())
        isRunning = true
    }

    val isAvailable: Boolean get() = isRunning

    fun startSegment(languageTag: String): Boolean {
        // Guard against overlapping segments, not against the long-lived recognizer being "running".
        if (pendingSegment) return false
        if (!isRunning) return false
        pendingSegment = true

        val intent = intentFactory.create(languageTag.ifBlank { "de-DE" })
        recognizer.startListening(intent)
        return true
    }

    fun stopSegment() {
        if (!pendingSegment) return
        recognizer.stopListening()
    }

    /** Recreate the internal listener and clear any stuck pending-segment state. */
    fun recreate() {
        isRunning = true
        pendingSegment = false
        recognizer.setRecognitionListener(Listener())
    }

    fun destroy() {
        isRunning = false
        pendingSegment = false
        recognizer.destroy()
    }

    private inner class Listener : RecognitionListener {
        override fun onReadyForSpeech(params: Bundle?) {}
        override fun onBeginningOfSpeech() {}
        override fun onRmsChanged(rmsdB: Float) {}
        override fun onBufferReceived(buffer: ByteArray?) {}
        override fun onEndOfSpeech() {}

        override fun onResults(results: Bundle?) {
            pendingSegment = false
            val text = results?.getStringArrayList(SpeechRecognizer.RESULTS_RECOGNITION)
                ?.firstOrNull()
                ?.takeIf { it.isNotBlank() }
            if (text != null) {
                callbacks.onFinal(text)
            } else {
                callbacks.onError(RecognizerFailure.NO_SPEECH)
            }
        }

        override fun onPartialResults(partialResults: Bundle?) {
            val text = partialResults?.getStringArrayList(SpeechRecognizer.RESULTS_RECOGNITION)
                ?.firstOrNull()
                ?.takeIf { it.isNotBlank() }
            if (text != null) callbacks.onPartial(text)
        }

        override fun onError(error: Int) {
            pendingSegment = false
            val failure = when (error) {
                SpeechRecognizer.ERROR_NO_MATCH -> RecognizerFailure.NO_SPEECH
                SpeechRecognizer.ERROR_SPEECH_TIMEOUT -> RecognizerFailure.NO_SPEECH
                SpeechRecognizer.ERROR_INSUFFICIENT_PERMISSIONS -> RecognizerFailure.PERMISSION
                SpeechRecognizer.ERROR_LANGUAGE_NOT_SUPPORTED -> RecognizerFailure.LANGUAGE_UNAVAILABLE
                SpeechRecognizer.ERROR_LANGUAGE_UNAVAILABLE -> RecognizerFailure.LANGUAGE_UNAVAILABLE
                SpeechRecognizer.ERROR_RECOGNIZER_BUSY -> RecognizerFailure.BUSY
                SpeechRecognizer.ERROR_CLIENT,
                SpeechRecognizer.ERROR_SERVER,
                SpeechRecognizer.ERROR_NETWORK,
                SpeechRecognizer.ERROR_NETWORK_TIMEOUT,
                SpeechRecognizer.ERROR_TOO_MANY_REQUESTS -> RecognizerFailure.OTHER
                else -> RecognizerFailure.OTHER
            }
            if (failure == RecognizerFailure.BUSY) {
                // A busy recognizer is wedged; clear the running flag so the service will recreate.
                isRunning = false
            }
            callbacks.onError(failure)
        }

        override fun onEvent(eventType: Int, params: Bundle?) {}
    }
}
