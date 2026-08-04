package net.hermes.dictate

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/** The waveform is the only thing on screen while dictating, so its math is tested directly. */
class WaveformTest {

    @Test
    fun `a pushed sample lands on the right and shifts the rest left`() {
        var history = Waveform.emptyHistory()
        history = Waveform.push(history, 90)
        assertEquals(90, Waveform.latestLevel(history))

        history = Waveform.push(history, 10)
        assertEquals(10, Waveform.latestLevel(history))
        // The loud sample is now one slot older and slightly decayed, but still clearly loud.
        val previous = history[history.size - 2]
        assertTrue("older sample kept its shape: $previous", previous in 0.70f..0.90f)
    }

    @Test
    fun `silence decays toward the resting line but never below it`() {
        var history = Waveform.push(Waveform.emptyHistory(), 100)
        repeat(80) { history = Waveform.push(history, 0) }
        // The stored amplitudes really reach silence; the resting line is a drawing floor.
        assertTrue("silence settles at zero", history.all { it <= 0.001f })
        assertTrue(
            "every drawn bar keeps the resting height",
            history.indices.all { Waveform.barFraction(history, it) >= Waveform.REST },
        )
    }

    @Test
    fun `levels outside the range are clamped instead of distorting the strip`() {
        assertEquals(100, Waveform.latestLevel(Waveform.push(Waveform.emptyHistory(), 250)))
        assertEquals(0, Waveform.latestLevel(Waveform.push(Waveform.emptyHistory(), -40)))
    }

    @Test
    fun `the fade applies to history only and never to the newest sample`() {
        val count = Waveform.BAR_COUNT
        assertEquals("the oldest bar fades out", 0f, Waveform.window(0, count), 0.001f)
        assertEquals("the newest bar is what the user is speaking", 1f, Waveform.window(count - 1, count), 0.001f)
        assertEquals(1f, Waveform.window(count / 2, count), 0.001f)
        for (index in 1 until count) {
            assertTrue(
                "the fade must not dip at $index",
                Waveform.window(index, count) >= Waveform.window(index - 1, count) - 0.001f,
            )
        }
    }

    @Test
    fun `no bar ever collapses completely`() {
        val history = Waveform.push(Waveform.emptyHistory(), 100)
        for (index in history.indices) {
            assertTrue(Waveform.barFraction(history, index) >= Waveform.REST)
        }
    }

    @Test
    fun `a single bar strip degrades gracefully`() {
        assertEquals(1f, Waveform.window(0, 1), 0.001f)
    }
}
