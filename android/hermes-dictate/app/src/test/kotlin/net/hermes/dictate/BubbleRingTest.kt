package net.hermes.dictate

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class BubbleRingTest {

    @Test
    fun `silence draws no ring at all`() {
        assertEquals(0, BubbleRing.alpha(0))
        assertEquals(0f, BubbleRing.extraRadiusPx(0, maxExtraPx = 10f), 0.001f)
        // Exactly at the resting threshold is still silent — no idle pulse.
        assertEquals(0, BubbleRing.alpha(BubbleRing.REST_THRESHOLD))
    }

    @Test
    fun `speaking makes the ring grow and brighten with loudness`() {
        val quiet = BubbleRing.alpha(20)
        val loud = BubbleRing.alpha(90)
        assertTrue("ring is visible once speaking starts", quiet > 0)
        assertTrue("louder speech brightens the ring further", loud > quiet)

        val quietRadius = BubbleRing.extraRadiusPx(20, maxExtraPx = 10f)
        val loudRadius = BubbleRing.extraRadiusPx(90, maxExtraPx = 10f)
        assertTrue(loudRadius > quietRadius)
    }

    @Test
    fun `max level never exceeds the drawable bounds`() {
        assertEquals(255, BubbleRing.alpha(100))
        assertEquals(10f, BubbleRing.extraRadiusPx(100, maxExtraPx = 10f), 0.001f)
    }
}
