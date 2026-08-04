package net.hermes.dictate

import org.junit.Assert.assertEquals
import org.junit.Test

class OverlayMotionTest {
    @Test
    fun disabledOrInvalidAnimatorScaleRemovesMotion() {
        assertEquals(0L, OverlayMotion.durationMs(140L, 0f))
        assertEquals(0L, OverlayMotion.durationMs(140L, -1f))
        assertEquals(0L, OverlayMotion.durationMs(140L, Float.NaN))
    }

    @Test
    fun systemScaleControlsShortTransition() {
        assertEquals(70L, OverlayMotion.durationMs(140L, 0.5f))
        assertEquals(140L, OverlayMotion.durationMs(140L, 1f))
    }

    @Test
    fun extremeScaleIsCappedToKeepOverlayResponsive() {
        assertEquals(280L, OverlayMotion.durationMs(140L, 10f))
    }
}
