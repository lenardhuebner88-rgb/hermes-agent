package net.hermes.dictate

import android.content.Context
import android.content.Intent
import android.os.Build
import android.os.Bundle
import android.speech.RecognitionListener
import android.speech.RecognizerIntent
import android.speech.SpeechRecognizer

/**
 * Thin wrapper around Android's SpeechRecognizer for the default dictation path.
 *
 * Privacy contract (PlanSpec): this path must never send audio off the device, so ONLY the
 * dedicated on-device recognizer (API 31+) is ever bound — the generic recognizer is documented
 * to stream audio to a server and EXTRA_PREFER_OFFLINE is merely best-effort, which would break
 * the guarantee silently (Codex review finding, 2026-07-10). Missing recognizer or missing
 * offline language pack surfaces visibly; there is no networked fallback.
 */
class OnDeviceDictation(
    private val context: Context,
    private val events: Events,
) {
    interface Events {
        fun onPartial(text: String)
        fun onFinal(text: String)
        fun onError(failure: RecognizerFailure)
    }

    private var recognizer: SpeechRecognizer? = null

    /** True when a recognizer could be created at all. */
    fun startSegment(languageTag: String?): Boolean {
        val r = recognizer ?: createRecognizer() ?: return false
        r.cancel()
        r.startListening(buildIntent(languageTag))
        return true
    }

    fun stopSegment() {
        recognizer?.stopListening()
    }

    fun cancel() {
        recognizer?.cancel()
    }

    /** Drops the current instance so the next segment binds a fresh recognizer (BUSY recovery). */
    fun recreate() {
        recognizer?.destroy()
        recognizer = null
    }

    fun destroy() {
        recognizer?.destroy()
        recognizer = null
    }

    private fun createRecognizer(): SpeechRecognizer? {
        if (Build.VERSION.SDK_INT < 31 || !SpeechRecognizer.isOnDeviceRecognitionAvailable(context)) {
            return null
        }
        val created = SpeechRecognizer.createOnDeviceSpeechRecognizer(context)
        created.setRecognitionListener(listener)
        recognizer = created
        return created
    }

    private fun buildIntent(languageTag: String?): Intent =
        Intent(RecognizerIntent.ACTION_RECOGNIZE_SPEECH).apply {
            putExtra(RecognizerIntent.EXTRA_LANGUAGE_MODEL, RecognizerIntent.LANGUAGE_MODEL_FREE_FORM)
            putExtra(RecognizerIntent.EXTRA_CALLING_PACKAGE, context.packageName)
            putExtra(RecognizerIntent.EXTRA_PARTIAL_RESULTS, true)
            // Belt and braces besides the on-device recognizer: never use networked models.
            putExtra(RecognizerIntent.EXTRA_PREFER_OFFLINE, true)
            // Dictation pauses (thinking) shouldn't end the segment instantly; chaining in the
            // controller covers longer gaps.
            putExtra(RecognizerIntent.EXTRA_SPEECH_INPUT_COMPLETE_SILENCE_LENGTH_MILLIS, 1600)
            putExtra(RecognizerIntent.EXTRA_SPEECH_INPUT_POSSIBLY_COMPLETE_SILENCE_LENGTH_MILLIS, 1600)
            languageTag?.let { putExtra(RecognizerIntent.EXTRA_LANGUAGE, it) }
        }

    private val listener = object : RecognitionListener {
        override fun onPartialResults(partialResults: Bundle?) {
            firstResult(partialResults)?.let { events.onPartial(it) }
        }

        override fun onResults(results: Bundle?) {
            events.onFinal(firstResult(results) ?: "")
        }

        override fun onError(error: Int) {
            events.onError(mapError(error))
        }

        override fun onReadyForSpeech(params: Bundle?) {}
        override fun onBeginningOfSpeech() {}
        override fun onRmsChanged(rmsdB: Float) {}
        override fun onBufferReceived(buffer: ByteArray?) {}
        override fun onEndOfSpeech() {}
        override fun onEvent(eventType: Int, params: Bundle?) {}
    }

    private fun firstResult(bundle: Bundle?): String? =
        bundle?.getStringArrayList(SpeechRecognizer.RESULTS_RECOGNITION)?.firstOrNull()?.takeIf { it.isNotBlank() }

    private fun mapError(code: Int): RecognizerFailure = when (code) {
        SpeechRecognizer.ERROR_NO_MATCH,
        SpeechRecognizer.ERROR_SPEECH_TIMEOUT,
        -> RecognizerFailure.NO_MATCH

        SpeechRecognizer.ERROR_RECOGNIZER_BUSY -> RecognizerFailure.BUSY

        // 12/13 exist from API 31; referenced numerically so minSdk 29 compiles.
        ERROR_LANGUAGE_NOT_SUPPORTED, ERROR_LANGUAGE_UNAVAILABLE -> RecognizerFailure.LANGUAGE_UNAVAILABLE

        SpeechRecognizer.ERROR_INSUFFICIENT_PERMISSIONS -> RecognizerFailure.PERMISSION

        else -> RecognizerFailure.OTHER
    }

    companion object {
        private const val ERROR_LANGUAGE_NOT_SUPPORTED = 12
        private const val ERROR_LANGUAGE_UNAVAILABLE = 13
    }
}
