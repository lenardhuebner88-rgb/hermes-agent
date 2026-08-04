package net.hermes.dictate

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class OverlayTextTest {
    @Test
    fun shortLiveTextIsUnchanged() {
        assertEquals("Kurzer Satz", OverlayText.visibleBody(state(UiStatus.Listening, "Kurzer Satz")))
    }

    @Test
    fun longLiveTextKeepsNewestWordsAndMarksOmission() {
        val source = "Bitte plane für morgen früh einen Termin mit dem gesamten Produktteam und notiere die wichtigsten Entscheidungen"
        val visible = checkNotNull(OverlayText.visibleBody(state(UiStatus.Listening, source), maxLiveChars = 48))

        assertTrue(visible.startsWith("… "))
        assertTrue(visible.endsWith("notiere die wichtigsten Entscheidungen"))
        assertTrue(visible.length <= 48)
    }

    @Test
    fun defaultWindowRetainsTheNewestLongWord() {
        val source = "Bitte plane für morgen früh einen Termin mit dem gesamten Produktteam und notiere die wichtigsten Entscheidungen"
        val visible = checkNotNull(OverlayText.visibleBody(state(UiStatus.Listening, source)))

        assertTrue(visible.endsWith("Entscheidungen"))
        assertTrue(visible.length <= 54)
    }

    @Test
    fun completedAndErrorCopyRemainComplete() {
        val done = state(UiStatus.Done, "Ein bewusst sehr langer finaler Text, der nicht durch eine Live-Regel gekürzt werden darf")
        assertEquals(done.body, OverlayText.visibleBody(done, maxLiveChars = 20))
    }

    private fun state(status: UiStatus, text: String): OverlayViewState = OverlayViewState.from(
        status = status,
        previewText = text,
        committedText = text,
        retryAvailable = false,
        errorText = { text },
    )
}
