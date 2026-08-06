package net.hermes.dictate

/**
 * Pure math for the level-reactive ring drawn around the idle mic bubble, separated from drawing
 * so it stays testable on the JVM (same split as [Waveform]/[OverlayWaveView]).
 *
 * Unlike the wave strip there is no resting floor here: below [REST_THRESHOLD] the ring is fully
 * invisible, so the bubble looks exactly like it does today when nobody is speaking — no idle
 * pulse, no zappeln at level≈0.
 */
object BubbleRing {
    /** Levels at or below this (0..100 scale) draw no ring at all. */
    const val REST_THRESHOLD = 4

    private const val MIN_ALPHA = 60

    /** Stroke alpha 0..255 for [level] (0..100); 0 (invisible) at/below [REST_THRESHOLD]. */
    fun alpha(level: Int): Int {
        if (level <= REST_THRESHOLD) return 0
        val fraction = loudnessFraction(level)
        return (MIN_ALPHA + (255 - MIN_ALPHA) * fraction).toInt().coerceIn(0, 255)
    }

    /** Extra radius in px added on top of the base ring radius; 0 at/below [REST_THRESHOLD]. */
    fun extraRadiusPx(level: Int, maxExtraPx: Float): Float {
        if (level <= REST_THRESHOLD) return 0f
        return maxExtraPx * loudnessFraction(level)
    }

    private fun loudnessFraction(level: Int): Float =
        (level - REST_THRESHOLD).toFloat() / (100 - REST_THRESHOLD)
}
