package net.hermes.dictate

import org.junit.Assert.assertEquals
import org.junit.Test

class OverlayGeometryTest {
    @Test fun `phone pill keeps twelve dp margins below the 384dp cap`() {
        assertEquals(1008, OverlayGeometry.pillWidthPx(1080, 2.625f))
    }

    @Test fun `wide tablet does not turn pill into a banner`() {
        assertEquals(768, OverlayGeometry.pillWidthPx(1600, 2f))
    }

    @Test fun `narrow viewport preserves margins instead of overflowing`() {
        assertEquals(352, OverlayGeometry.pillWidthPx(400, 2f))
    }

    @Test fun `height is one stable token shared with the layout`() {
        assertEquals(168, OverlayGeometry.pillHeightPx(2.625f))
    }
}
