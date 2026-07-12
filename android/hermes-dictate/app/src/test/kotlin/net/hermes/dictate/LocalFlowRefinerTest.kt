package net.hermes.dictate

import org.junit.Assert.assertEquals
import org.junit.Test

class LocalFlowRefinerTest {
    @Test
    fun `removes German fillers and immediate repetitions`() {
        assertEquals(
            "das ist ein Test",
            LocalFlowRefiner.refine("äh das das ist ähm ein Test", "de-DE"),
        )
    }

    @Test
    fun `keeps German um because it is usually a real preposition`() {
        assertEquals("Treffen um drei", LocalFlowRefiner.refine("Treffen um drei", "de-DE"))
    }

    @Test
    fun `resolves a German spoken backtrack to the corrected word`() {
        assertEquals(
            "Treffen am Mittwoch",
            LocalFlowRefiner.refine("Treffen am Dienstag, nein, ich meine Mittwoch", "de-DE"),
        )
    }

    @Test
    fun `resolves an English spoken backtrack and English fillers`() {
        assertEquals(
            "meet on Wednesday",
            LocalFlowRefiner.refine("um meet on Tuesday, no wait, I mean Wednesday", "en-US"),
        )
    }

    @Test
    fun `recognizes only exact explicit edit commands`() {
        assertEquals(
            DictationTransform.UndoLastSegment,
            LocalFlowRefiner.transform("Nein, zurück.", "de-DE"),
        )
        assertEquals(
            DictationTransform.DeleteLastSentence,
            LocalFlowRefiner.transform("Lösche den letzten Satz", "de-DE"),
        )
        assertEquals(
            DictationTransform.Text("ich gehe nicht zurück"),
            LocalFlowRefiner.transform("ich gehe nicht zurück", "de-DE"),
        )
    }

    @Test
    fun `does not damage URLs email addresses numbers or code fragments`() {
        val input = "mail a.b+test@example.com URL https://x.dev/a_a Wert 82,4 code foo_bar()"
        assertEquals(input, LocalFlowRefiner.refine(input, "de-DE"))
    }
}
