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
}
