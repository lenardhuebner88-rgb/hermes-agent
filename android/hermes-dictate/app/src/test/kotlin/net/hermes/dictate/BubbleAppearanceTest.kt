package net.hermes.dictate

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class BubbleAppearanceTest {
    @Test
    fun `size and opacity snap to documented Flow stops`() {
        assertEquals(85, BubbleAppearance.nearestSize(82))
        assertEquals(100, BubbleAppearance.nearestOpacity(91))
    }

    @Test
    fun `idle shrink never violates 48dp touch target`() {
        for (size in BubbleAppearance.sizeStops) {
            assertTrue(BubbleAppearance.sizeDp(size, idleShrink = true) >= 48)
        }
    }
}
