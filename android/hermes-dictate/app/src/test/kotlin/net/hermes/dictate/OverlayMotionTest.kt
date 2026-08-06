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

    @Test
    fun slideDistanceMatchesTheCurrentPillWidth() {
        // The pill slides in across its own current width, so a narrower quiet pill also travels
        // a shorter distance than a wider active one.
        assertEquals(346, OverlayMotion.slideDistancePx(346))
        assertEquals(735, OverlayMotion.slideDistancePx(735))
    }

    @Test
    fun slideDistanceNeverGoesNegative() {
        assertEquals(0, OverlayMotion.slideDistancePx(-12))
    }

    @Test
    fun zeroAnimatorScaleLeavesNothingHanging() {
        // Reduced-animations users (or a system animator scale of exactly 0) must land at the
        // resting state in one step: no duration to animate, so the caller applies translationX=0
        // directly instead of scheduling an animator that would otherwise never fire its listeners.
        val duration = OverlayMotion.durationMs(OverlayMotion.PILL_ENTER_MS, 0f)
        assertEquals(0L, duration)
    }
}
