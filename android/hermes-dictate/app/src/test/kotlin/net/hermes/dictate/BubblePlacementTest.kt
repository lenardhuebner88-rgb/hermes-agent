package net.hermes.dictate

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class BubblePlacementTest {
    private val screen = BubbleScreen(widthPx = 1080, heightPx = 2400, topInsetPx = 128, bottomInsetPx = 63)

    @Test
    fun `restored y cannot overlap status or navigation bars`() {
        assertEquals(152, BubblePlacement.clampY(-500, 126, screen, marginPx = 24))
        assertEquals(2187, BubblePlacement.clampY(9_999, 126, screen, marginPx = 24))
    }

    @Test
    fun `default placement is centered in usable space`() {
        assertEquals(1169, BubblePlacement.centeredY(126, screen, marginPx = 24))
    }

    @Test
    fun `edge choice changes exactly at screen midpoint`() {
        assertFalse(BubblePlacement.isRight(539f, 1080))
        assertTrue(BubblePlacement.isRight(540f, 1080))
    }

    @Test
    fun `tiny screen still produces one safe deterministic y`() {
        val tiny = BubbleScreen(widthPx = 100, heightPx = 100, topInsetPx = 40, bottomInsetPx = 40)
        assertEquals(48, BubblePlacement.clampY(90, 80, tiny, marginPx = 8))
    }

    @Test
    fun `x outside either edge is pulled back onto screen`() {
        assertEquals(24, BubblePlacement.clampX(-500, 126, screen, marginPx = 24))
        assertEquals(930, BubblePlacement.clampX(9_999, 126, screen, marginPx = 24))
    }

    @Test
    fun `an x valid on a wide screen is reclamped after rotating to a narrower one`() {
        val landscape = BubbleScreen(widthPx = 2400, heightPx = 1080, topInsetPx = 63, bottomInsetPx = 63)
        val landscapeX = BubblePlacement.clampX(1_200, 126, landscape, marginPx = 24)
        assertEquals(1_200, landscapeX)
        // Rotating to the narrower (portrait) screen leaves the same x hanging off the edge.
        assertEquals(930, BubblePlacement.clampX(landscapeX, 126, screen, marginPx = 24))
    }

    @Test
    fun `migrating a right-docked install starts near the right edge, not at zero`() {
        val migrated = BubblePlacement.migratedX(onRight = true, bubbleWidthPx = 126, screen = screen, marginPx = 24)
        assertEquals(930, migrated)
        assertTrue(migrated > screen.widthPx / 2)
    }

    @Test
    fun `migrating a left-docked install starts at the left margin`() {
        assertEquals(24, BubblePlacement.migratedX(onRight = false, bubbleWidthPx = 126, screen = screen, marginPx = 24))
    }
}
