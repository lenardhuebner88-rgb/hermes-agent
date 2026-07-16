package net.hermes.dictate

import android.content.Intent
import android.os.Bundle
import android.speech.RecognitionListener
import android.speech.RecognizerIntent
import android.speech.SpeechRecognizer
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
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
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [34])
class OnDeviceDictationTest {

    @Mock
    private lateinit var callbacks: OnDeviceDictation.Callbacks

    private lateinit var intentFactory: RecognizeIntentFactory
    private val intent = Intent()

    /** Hands out the supplied recognizers in order; a segment that outlives them gets null. */
    private class QueueFactory(private vararg val instances: SpeechRecognizer?) : RecognizerFactory {
        var createCount = 0
            private set

        override fun create(): SpeechRecognizer? = instances.getOrNull(createCount).also { createCount += 1 }
    }

    @Before
    fun setUp() {
        MockitoAnnotations.openMocks(this)
        intentFactory = mock { on { create(any()) }.thenReturn(intent) }
    }

    private fun resultsBundle(text: String): Bundle =
        Bundle().apply { putStringArrayList(SpeechRecognizer.RESULTS_RECOGNITION, arrayListOf(text)) }

    /** The listener the wrapper bound onto [r] (its only setRecognitionListener call). */
    private fun listenerOf(r: SpeechRecognizer): RecognitionListener {
        val captor = argumentCaptor<RecognitionListener>()
        verify(r).setRecognitionListener(captor.capture())
        return captor.lastValue
    }

    // --- Lifecycle / repeated dictation (AC-LIFECYCLE-1) ---

    @Test
    fun `two consecutive segments each commit through the current recognizer`() {
        val r = mock<SpeechRecognizer>()
        val dictation = OnDeviceDictation(QueueFactory(r), callbacks, intentFactory)
        val listener = run { dictation.startSegment("de-DE"); listenerOf(r) }
        verify(r).startListening(eq(intent))

        listener.onResults(resultsBundle("erster teil"))
        verify(callbacks).onFinal(eq("erster teil"))

        // The terminal result cleared the pending flag, so the next segment starts on the SAME
        // reused instance without recreating the app or keyboard.
        assertTrue(dictation.startSegment("de-DE"))
        verify(r, times(2)).startListening(eq(intent))
        listener.onResults(resultsBundle("zweiter teil"))
        verify(callbacks).onFinal(eq("zweiter teil"))
    }

    @Test
    fun `startSegment preserves blank language for auto detection`() {
        val r = mock<SpeechRecognizer>()
        OnDeviceDictation(QueueFactory(r), callbacks, intentFactory).startSegment("")
        verify(intentFactory).create(eq(""))
    }

    @Test
    fun `startSegment passes the provided language through`() {
        val r = mock<SpeechRecognizer>()
        OnDeviceDictation(QueueFactory(r), callbacks, intentFactory).startSegment("en-US")
        verify(intentFactory).create(eq("en-US"))
    }

    @Test
    fun `a second overlapping segment is rejected while one is pending`() {
        val r = mock<SpeechRecognizer>()
        val dictation = OnDeviceDictation(QueueFactory(r), callbacks, intentFactory)
        assertTrue(dictation.startSegment("de-DE"))
        assertFalse(dictation.startSegment("de-DE"))
        verify(r, times(1)).startListening(any())
    }

    // --- BUSY recovery: recreate() must DESTROY and REBIND (verifier's core finding) ---

    @Test
    fun `recreate destroys the wedged recognizer and binds a genuinely fresh instance`() {
        val first = mock<SpeechRecognizer>()
        val second = mock<SpeechRecognizer>()
        val factory = QueueFactory(first, second)
        val dictation = OnDeviceDictation(factory, callbacks, intentFactory)

        assertTrue(dictation.startSegment("de-DE"))
        verify(first).startListening(any())

        dictation.recreate()
        // The real recognizer is torn down — not merely a flag reset / listener swap.
        verify(first).destroy()

        assertTrue(dictation.startSegment("de-DE"))
        // A brand-new recognizer was created and used, not the wedged first one.
        assertEquals(2, factory.createCount)
        verify(second).startListening(any())
        verify(first, times(1)).startListening(any())
        listenerOf(second).onResults(resultsBundle("frisch"))
        verify(callbacks).onFinal(eq("frisch"))
    }

    // --- Stale / late callbacks cannot cross session boundaries (AC-LIFECYCLE-2) ---

    @Test
    fun `a late callback from a recreated recognizer cannot touch the new session`() {
        val first = mock<SpeechRecognizer>()
        val second = mock<SpeechRecognizer>()
        val dictation = OnDeviceDictation(QueueFactory(first, second), callbacks, intentFactory)

        dictation.startSegment("de-DE")
        val staleListener = listenerOf(first)

        dictation.recreate()
        assertTrue(dictation.startSegment("de-DE")) // binds `second`

        // Delayed results/errors from the destroyed first recognizer must be dropped entirely.
        staleListener.onResults(resultsBundle("alt"))
        staleListener.onPartialResults(resultsBundle("alt partial"))
        staleListener.onError(SpeechRecognizer.ERROR_NO_MATCH)
        verify(callbacks, never()).onFinal(any())
        verify(callbacks, never()).onPartial(any())
        verify(callbacks, never()).onError(any())

        // The current session is unaffected and still delivers its own result.
        listenerOf(second).onResults(resultsBundle("neu"))
        verify(callbacks).onFinal(eq("neu"))
    }

    @Test
    fun `a stale terminal callback cannot clear the pending gate of the newer session`() {
        // Real S24 race: segment starts -> recognizer reports BUSY -> controller recreate()s ->
        // a fresh segment is listening (pending) -> the OLD recognizer only now delivers its
        // delayed onResults. If that stale terminal leaked through it would flip pendingSegment
        // false and let an overlapping start corrupt the live session. It must be dropped, and the
        // new session's pending gate must stay intact.
        val first = mock<SpeechRecognizer>()
        val second = mock<SpeechRecognizer>()
        val dictation = OnDeviceDictation(QueueFactory(first, second), callbacks, intentFactory)

        dictation.startSegment("de-DE")
        val staleListener = listenerOf(first)
        staleListener.onError(SpeechRecognizer.ERROR_RECOGNIZER_BUSY)
        verify(callbacks).onError(eq(RecognizerFailure.BUSY))

        dictation.recreate()
        assertTrue(dictation.startSegment("de-DE")) // binds `second`, pending again

        // Late terminal from the destroyed first recognizer arrives now.
        staleListener.onResults(resultsBundle("verspaetet"))

        // It neither committed nor corrupted the new session: no extra onFinal, and the pending
        // gate is still closed so an overlapping start is still rejected.
        verify(callbacks, never()).onFinal(any())
        assertFalse(dictation.startSegment("de-DE"))
        verify(second, times(1)).startListening(any())

        // The live session still terminates on its own real result.
        listenerOf(second).onResults(resultsBundle("aktuell"))
        verify(callbacks).onFinal(eq("aktuell"))
    }

    @Test
    fun `a late callback after destroy is dropped`() {
        val r = mock<SpeechRecognizer>()
        val dictation = OnDeviceDictation(QueueFactory(r), callbacks, intentFactory)
        dictation.startSegment("de-DE")
        val listener = listenerOf(r)

        dictation.destroy()
        verify(r).destroy()
        listener.onResults(resultsBundle("zu spaet"))
        listener.onError(SpeechRecognizer.ERROR_NO_MATCH)
        verify(callbacks, never()).onFinal(any())
        verify(callbacks, never()).onError(any())
    }

    // --- No-text insertion path stays recoverable (AC-LIFECYCLE-2) ---

    @Test
    fun `a blank result surfaces no-speech and the field stays recoverable for another dictation`() {
        val r = mock<SpeechRecognizer>()
        val dictation = OnDeviceDictation(QueueFactory(r), callbacks, intentFactory)
        dictation.startSegment("de-DE")
        val listener = listenerOf(r)

        listener.onResults(resultsBundle("   "))
        verify(callbacks).onError(eq(RecognizerFailure.NO_SPEECH))

        // The pending flag was cleared by the terminal (empty) result, so a fresh segment can start
        // without recreating anything — the "no text inserted, then stops working" field bug.
        assertTrue(dictation.startSegment("de-DE"))
        verify(r, times(2)).startListening(any())
    }

    @Test
    fun `missing results emit no-speech`() {
        val r = mock<SpeechRecognizer>()
        val dictation = OnDeviceDictation(QueueFactory(r), callbacks, intentFactory)
        dictation.startSegment("de-DE")
        listenerOf(r).onResults(Bundle())
        verify(callbacks).onError(eq(RecognizerFailure.NO_SPEECH))
    }

    // --- Availability / privacy contract (AC-QUALITY-1) ---

    @Test
    fun `startSegment returns false when on-device recognition is unavailable`() {
        val dictation = OnDeviceDictation(RecognizerFactory { null }, callbacks, intentFactory)
        assertFalse(dictation.startSegment("de-DE"))
        verify(callbacks, never()).onError(any())
    }

    @Test
    fun `destroy prevents further starts and reports unavailable`() {
        val r = mock<SpeechRecognizer>()
        val dictation = OnDeviceDictation(QueueFactory(r), callbacks, intentFactory)
        dictation.destroy()
        assertFalse(dictation.isAvailable)
        assertFalse(dictation.startSegment("de-DE"))
    }

    // --- Error / stop mapping ---

    @Test
    fun `onError maps BUSY`() {
        val r = mock<SpeechRecognizer>()
        val dictation = OnDeviceDictation(QueueFactory(r), callbacks, intentFactory)
        dictation.startSegment("de-DE")
        listenerOf(r).onError(SpeechRecognizer.ERROR_RECOGNIZER_BUSY)
        verify(callbacks).onError(eq(RecognizerFailure.BUSY))
    }

    @Test
    fun `onError maps language unavailable`() {
        val r = mock<SpeechRecognizer>()
        val dictation = OnDeviceDictation(QueueFactory(r), callbacks, intentFactory)
        dictation.startSegment("de-DE")
        listenerOf(r).onError(SpeechRecognizer.ERROR_LANGUAGE_UNAVAILABLE)
        verify(callbacks).onError(eq(RecognizerFailure.LANGUAGE_UNAVAILABLE))
    }

    @Test
    fun `onPartialResults emits partial text and ignores blanks`() {
        val r = mock<SpeechRecognizer>()
        val dictation = OnDeviceDictation(QueueFactory(r), callbacks, intentFactory)
        dictation.startSegment("de-DE")
        val listener = listenerOf(r)
        listener.onPartialResults(resultsBundle("teil"))
        verify(callbacks).onPartial(eq("teil"))
        listener.onPartialResults(resultsBundle("   "))
        verify(callbacks, times(1)).onPartial(any())
    }

    @Test
    fun `stopSegment only stops a pending segment`() {
        val r = mock<SpeechRecognizer>()
        val dictation = OnDeviceDictation(QueueFactory(r), callbacks, intentFactory)
        dictation.stopSegment()
        verify(r, never()).stopListening()
        dictation.startSegment("de-DE")
        dictation.stopSegment()
        verify(r).stopListening()
    }

    // --- German quality defaults, feature-detected (AC-QUALITY-1) ---

    @Test
    fun `default intent factory selects German and enables formatting on API 33+`() {
        val built = DefaultRecognizeIntentFactory(sdkInt = 34, callingPackage = "net.hermes.dictate")
            .create("de-DE")
        assertEquals("de-DE", built.getStringExtra(RecognizerIntent.EXTRA_LANGUAGE))
        assertTrue(built.getBooleanExtra(RecognizerIntent.EXTRA_PREFER_OFFLINE, false))
        assertEquals(
            RecognizerIntent.FORMATTING_OPTIMIZE_QUALITY,
            built.getStringExtra(RecognizerIntent.EXTRA_ENABLE_FORMATTING),
        )
    }

    @Test
    fun `default intent factory omits the formatting extra below API 33`() {
        val built = DefaultRecognizeIntentFactory(sdkInt = 30).create("de-DE")
        assertFalse(built.hasExtra(RecognizerIntent.EXTRA_ENABLE_FORMATTING))
        assertEquals("de-DE", built.getStringExtra(RecognizerIntent.EXTRA_LANGUAGE))
        assertTrue(built.getBooleanExtra(RecognizerIntent.EXTRA_PREFER_OFFLINE, false))
    }

    // --- Personal vocabulary biasing (Stufe 3) ---

    @Test
    fun `default intent factory passes biasing phrases on API 33+`() {
        val built = DefaultRecognizeIntentFactory(
            sdkInt = 34,
            biasingPhrases = { listOf("Hermes", "PlanSpec") },
        ).create("de-DE")
        assertEquals(
            arrayListOf("Hermes", "PlanSpec"),
            built.getStringArrayListExtra(RecognizerIntent.EXTRA_BIASING_STRINGS),
        )
    }

    @Test
    fun `default intent factory omits the biasing extra below API 33 and when empty`() {
        val legacy = DefaultRecognizeIntentFactory(
            sdkInt = 30,
            biasingPhrases = { listOf("Hermes") },
        ).create("de-DE")
        assertFalse(legacy.hasExtra(RecognizerIntent.EXTRA_BIASING_STRINGS))

        val empty = DefaultRecognizeIntentFactory(sdkInt = 34).create("de-DE")
        assertFalse(empty.hasExtra(RecognizerIntent.EXTRA_BIASING_STRINGS))
    }

    @Test
    fun `biasing phrases are re-evaluated for every created intent`() {
        var phrases = listOf("Hermes")
        val factory = DefaultRecognizeIntentFactory(sdkInt = 34, biasingPhrases = { phrases })
        assertEquals(
            arrayListOf("Hermes"),
            factory.create("de-DE").getStringArrayListExtra(RecognizerIntent.EXTRA_BIASING_STRINGS),
        )
        phrases = listOf("Hermes", "Leitstand")
        assertEquals(
            arrayListOf("Hermes", "Leitstand"),
            factory.create("de-DE").getStringArrayListExtra(RecognizerIntent.EXTRA_BIASING_STRINGS),
        )
    }
}
