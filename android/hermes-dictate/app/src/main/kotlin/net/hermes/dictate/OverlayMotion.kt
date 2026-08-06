package net.hermes.dictate

import kotlin.math.roundToLong

/** Motion policy: short, one-shot transitions that honor Android's animator scale. */
object OverlayMotion {
    const val PILL_ENTER_MS = 140L

    fun durationMs(baseMs: Long, animatorScale: Float): Long {
        if (!animatorScale.isFinite() || animatorScale <= 0f) return 0L
        return (baseMs * animatorScale.coerceAtMost(MAX_SCALE)).roundToLong()
    }

    /**
     * The pill enters from the right: it starts translated by its own current width and animates
     * back to translationX=0, so it arrives from just off its own trailing edge regardless of
     * which side of the screen it is anchored to. Negative widths (shouldn't happen, but geometry
     * math is defensive elsewhere too) never produce a distance that pulls it the wrong way.
     */
    fun slideDistancePx(pillWidthPx: Int): Int = pillWidthPx.coerceAtLeast(0)

    private const val MAX_SCALE = 2f
}
