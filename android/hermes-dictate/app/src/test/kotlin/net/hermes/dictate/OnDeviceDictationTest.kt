package net.hermes.dictate

import android.content.Intent
import android.os.Bundle
import android.speech.RecognitionListener
import android.speech.SpeechRecognizer
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import org.mockito.Mock
import org.mockito.MockitoAnnotations
import org.mockito.kotlin.any
import org.mockito.kotlin.argumentCaptor
import org.mockito.kotlin.eq
import org.mockito.kotlin.mock
import org.mockito.kotlin.never
import org.mockito.kotlin.times
import org.mockito.kotlin.verify
import org.mockito.kotlin.whenever
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [34])
class OnDeviceDictationTest {

    @Mock
    private lateinit var recognizer: SpeechRecognizer

    @Mock
    private lateinit var callbacks: OnDeviceDictation.Callbacks

    private lateinit var intentFactory: RecognizeIntentFactory
    private lateinit var intent: Intent
    private lateinit var dictation: OnDeviceDictation
    private lateinit var listener: RecognitionListener

    @Before
    fun setUp() {
        MockitoAnnotations.openMocks(this)
        intent = Intent()
        intentFactory = mock { on { create(any()) }.thenReturn(intent) }
        dictation = OnDeviceDictation(recognizer, callbacks, intentFactory)

        val captor = argumentCaptor<RecognitionListener>()
        verify(recognizer).setRecognitionListener(captor.capture())
        listener = captor.firstValue
    }

    private fun resultsBundle(text: String): Bundle {
        val bundle = Bundle()
        bundle.putStringArrayList(SpeechRecognizer.RESULTS_RECOGNITION, arrayListOf(text))
        return bundle
    }

    private fun partialBundle(text: String): Bundle {
        val bundle = Bundle()
        bundle.putStringArrayList(SpeechRecognizer.RESULTS_RECOGNITION, arrayListOf(text))
        return bundle
    }

    @Test
    fun `startSegment returns false when already pending`() {
        whenever(intentFactory.create(any())).thenReturn(intent)
        dictation.startSegment("de-DE")
        verify(recognizer).startListening(eq(intent))

        val second = dictation.startSegment("de-DE")
        assert(!second)
        verify(recognizer, times(1)).startListening(any())
    }

    @Test
    fun `startSegment uses default language when blank`() {
        dictation.startSegment("")
        verify(intentFactory).create(eq("de-DE"))
    }

    @Test
    fun `startSegment passes provided language to factory`() {
        dictation.startSegment("en-US")
        verify(intentFactory).create(eq("en-US"))
    }

    @Test
    fun `onResults emits final text`() {
        dictation.startSegment("de-DE")
        listener.onResults(resultsBundle("Hallo Welt"))
        verify(callbacks).onFinal(eq("Hallo Welt"))
    }

    @Test
    fun `onResults with blank text emits no speech error`() {
        dictation.startSegment("de-DE")
        listener.onResults(resultsBundle("   "))
        verify(callbacks).onError(eq(RecognizerFailure.NO_SPEECH))
    }

    @Test
    fun `onResults with missing results emits no speech error`() {
        dictation.startSegment("de-DE")
        listener.onResults(Bundle())
        verify(callbacks).onError(eq(RecognizerFailure.NO_SPEECH))
    }

    @Test
    fun `onPartialResults emits partial text`() {
        dictation.startSegment("de-DE")
        listener.onPartialResults(partialBundle("teil"))
        verify(callbacks).onPartial(eq("teil"))
    }

    @Test
    fun `onPartialResults ignores blank`() {
        dictation.startSegment("de-DE")
        listener.onPartialResults(partialBundle("   "))
        verify(callbacks, never()).onPartial(any())
    }

    @Test
    fun `onError maps BUSY to busy and marks unavailable`() {
        dictation.startSegment("de-DE")
        listener.onError(SpeechRecognizer.ERROR_RECOGNIZER_BUSY)
        verify(callbacks).onError(eq(RecognizerFailure.BUSY))
        assert(!dictation.isAvailable)
    }

    @Test
    fun `onError maps NO_MATCH to no speech`() {
        dictation.startSegment("de-DE")
        listener.onError(SpeechRecognizer.ERROR_NO_MATCH)
        verify(callbacks).onError(eq(RecognizerFailure.NO_SPEECH))
    }

    @Test
    fun `stopSegment only stops when pending`() {
        dictation.stopSegment()
        verify(recognizer, never()).stopListening()

        dictation.startSegment("de-DE")
        dictation.stopSegment()
        verify(recognizer).stopListening()
    }

    @Test
    fun `recreate resets pending segment and listener`() {
        dictation.startSegment("de-DE")
        dictation.recreate()

        val captor = argumentCaptor<RecognitionListener>()
        verify(recognizer, times(2)).setRecognitionListener(captor.capture())
        val newListener = captor.lastValue

        // After recreate a new segment should be accepted again.
        whenever(intentFactory.create(any())).thenReturn(intent)
        val started = dictation.startSegment("de-DE")
        assert(started)
        verify(recognizer, times(2)).startListening(any())

        newListener.onResults(resultsBundle("neu"))
        verify(callbacks).onFinal(eq("neu"))
    }

    @Test
    fun `destroy prevents further starts`() {
        dictation.destroy()
        val started = dictation.startSegment("de-DE")
        assert(!started)
        verify(recognizer, never()).startListening(any())
    }
}
