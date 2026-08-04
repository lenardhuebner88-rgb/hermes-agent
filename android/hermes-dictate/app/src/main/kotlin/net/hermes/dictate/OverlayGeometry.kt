package net.hermes.dictate

/** Responsive dimensions for the expanded overlay. Values are bounded in dp, then pixel-safe. */
object OverlayGeometry {
    fun pillWidthPx(screenWidthPx: Int, density: Float): Int {
        val margin = (SCREEN_MARGIN_DP * density).toInt()
        val available = (screenWidthPx - margin * 2).coerceAtLeast(1)
        val maximum = (MAX_PILL_WIDTH_DP * density).toInt()
        val minimum = (MIN_PILL_WIDTH_DP * density).toInt().coerceAtMost(available)
        return available.coerceAtMost(maximum).coerceAtLeast(minimum)
    }

    fun pillHeightPx(density: Float): Int = (PILL_HEIGHT_DP * density).toInt()

    private const val SCREEN_MARGIN_DP = 12
    private const val MIN_PILL_WIDTH_DP = 280
    private const val MAX_PILL_WIDTH_DP = 384
    /** Mirrors @dimen/pill_height; OverlayGeometryTokenTest asserts they stay equal. */
    const val PILL_HEIGHT_DP = 72
}
