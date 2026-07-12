package net.hermes.dictate

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

class DictationEditsTest {
    @Test
    fun `undo deletes only the exact immediately preceding inserted segment`() {
        assertEquals(
            DictationEdits.Result("Hallo", 5, 5),
            DictationEdits.undoLastSegment("Hallo Welt", 10, " Welt"),
        )
        assertNull(DictationEdits.undoLastSegment("Hallo Welt!", 11, " Welt"))
    }

    @Test
    fun `delete last sentence preserves everything through the prior boundary`() {
        val text = "Erster Satz. Zweiter Satz"
        assertEquals(
            DictationEdits.Result("Erster Satz.", 12, 13),
            DictationEdits.deleteLastSentence(text, text.length),
        )
    }

    @Test
    fun `delete first sentence clears only text before the cursor`() {
        assertEquals(
            DictationEdits.Result(" danach", 0, 5),
            DictationEdits.deleteLastSentence("Hallo danach", 5),
        )
    }
}
