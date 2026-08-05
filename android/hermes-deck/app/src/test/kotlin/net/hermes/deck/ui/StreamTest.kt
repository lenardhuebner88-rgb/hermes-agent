package net.hermes.deck.ui

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * The stream's folding rule.
 *
 * A worker narrating itself emits the same heartbeat line every twenty seconds.
 * Three of them in a row filled the visible screen and pushed the things that
 * actually happened below the fold — so identical neighbours fold into one row
 * carrying a count.
 */
class StreamTest {

    private fun entry(
        who: String,
        kind: String,
        what: String,
        age: Int?,
        key: String = "$who-$kind-$what-$age",
    ) = StreamEntry(
        key = key,
        who = who,
        kind = kind,
        what = what,
        ageSeconds = age,
        tone = StreamEntry.Tone.GOOD,
        mono = false,
    )

    @Test
    fun `identical neighbours fold into one row with a count`() {
        val folded = collapseRepeats(
            listOf(
                entry("coder", "meldet sich", "receiving stream response", 7),
                entry("coder", "meldet sich", "receiving stream response", 67),
                entry("coder", "meldet sich", "receiving stream response", 127),
            ),
        )
        assertEquals(1, folded.size)
        assertEquals(3, folded.first().repeats)
        // The newest age survives — that is the one the operator is asking about.
        assertEquals(7, folded.first().ageSeconds)
    }

    @Test
    fun `a different actor breaks the run`() {
        val folded = collapseRepeats(
            listOf(
                entry("coder", "meldet sich", "same text", 1),
                entry("reviewer", "meldet sich", "same text", 2),
                entry("coder", "meldet sich", "same text", 3),
            ),
        )
        // Three actors' lines must never be merged just because they read alike.
        assertEquals(3, folded.size)
        assertTrue(folded.all { it.repeats == 1 })
    }

    @Test
    fun `only immediate neighbours fold so nothing is reordered`() {
        val folded = collapseRepeats(
            listOf(
                entry("coder", "meldet sich", "A", 1),
                entry("coder", "übernommen", "B", 2),
                entry("coder", "meldet sich", "A", 3),
            ),
        )
        assertEquals(3, folded.size)
        assertEquals(listOf("A", "B", "A"), folded.map { it.what })
    }

    @Test
    fun `a plain list is unchanged`() {
        val input = listOf(entry("a", "k", "1", 1), entry("b", "k", "2", 2))
        val folded = collapseRepeats(input)
        assertEquals(2, folded.size)
        assertTrue(folded.all { it.repeats == 1 })
    }

    @Test
    fun `the freshest age reads as jetzt rather than a clipped word`() {
        // The stream's time column is one line wide; "gerade eben" was clipped
        // to "gerade", which reads as a different word entirely.
        assertEquals("jetzt", ago(0))
        assertEquals("jetzt", ago(4))
        assertEquals("vor 5 s", ago(5))
    }
}
