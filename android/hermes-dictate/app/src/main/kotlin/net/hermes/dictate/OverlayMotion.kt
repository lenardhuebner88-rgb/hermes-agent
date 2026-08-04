package net.hermes.dictate

import kotlin.math.roundToLong

/** Motion policy: short, one-shot transitions that honor Android's animator scale. */
object OverlayMotion {
    const val PILL_ENTER_MS = 140L

    fun durationMs(baseMs: Long, animatorScale: Float): Long {
        if (!animatorScale.isFinite() || animatorScale <= 0f) return 0L
        return (baseMs * animatorScale.coerceAtMost(MAX_SCALE)).roundToLong()
    }

    private const val MAX_SCALE = 2f
}
