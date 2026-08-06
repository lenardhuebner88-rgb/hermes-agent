package net.hermes.dictate

/** Responsive dimensions for the expanded overlay. Values are bounded in dp, then pixel-safe. */
object OverlayGeometry {
    /**
     * The pill's width depends on what state it is in, not just the screen:
     * - [active] (listening/recording/processing/uploading — the wave is breathing): targets
     *   [ACTIVE_WIDTH_FRACTION] of the screen, but never below the floor a visible wave plus the
     *   cancel/confirm actions actually need, and never above [MAX_PILL_WIDTH_DP].
     * - otherwise ("quiet" — done/cloud-done/failed): only as wide as what's actually on screen —
     *   the always-visible cancel/confirm actions, [secondaryVisible]'s undo button if present,
     *   and the content column if [contentVisible] (a result/error message is showing).
     * Either way the result never exceeds the screen minus [SCREEN_MARGIN_DP] margins.
     */
    fun pillWidthPx(
        screenWidthPx: Int,
        density: Float,
        active: Boolean,
        secondaryVisible: Boolean = false,
        contentVisible: Boolean = active,
    ): Int {
        val margin = (SCREEN_MARGIN_DP * density).toInt()
        val available = (screenWidthPx - margin * 2).coerceAtLeast(1)
        if (!active) {
            val quietDp = QUIET_BASE_WIDTH_DP +
                (if (contentVisible) CONTENT_COLUMN_WIDTH_DP else 0) +
                (if (secondaryVisible) TOUCH_TARGET_DP + ACTION_GAP_DP else 0)
            return (quietDp * density).toInt().coerceAtMost(available)
        }
        val maximum = (MAX_PILL_WIDTH_DP * density).toInt()
        val minimum = (MIN_PILL_WIDTH_DP * density).toInt().coerceAtMost(available)
        val target = (screenWidthPx * ACTIVE_WIDTH_FRACTION).toInt()
        return target.coerceAtMost(maximum).coerceAtLeast(minimum).coerceAtMost(available)
    }

    fun pillHeightPx(density: Float): Int = (PILL_HEIGHT_DP * density).toInt()

    private const val SCREEN_MARGIN_DP = 12
    // Floor for the active pill: below this, the wave and the cancel/confirm actions around it
    // don't have room to breathe. Derived from the quiet floor below (132dp) plus the content
    // column a visible wave needs (132dp, CONTENT_COLUMN_WIDTH_DP) plus a small buffer — not an
    // arbitrary number.
    private const val MIN_PILL_WIDTH_DP = 280
    private const val MAX_PILL_WIDTH_DP = 384
    private const val ACTIVE_WIDTH_FRACTION = 0.6
    // Mirrors overlay_touch_target (48dp) in dimens.xml: OverlayGeometryTokenTest checks the XML
    // literal separately; this is the same number used for the *window* size, not a view inside it.
    private const val TOUCH_TARGET_DP = 48
    // pill_padding_horizontal (8dp) on both sides + pill_column_margin (10dp) on both sides of the
    // content column, which is the space the cancel/confirm actions always need even with the
    // content column collapsed to zero width. 8*2 + 10*2 + 48(cancel) + 48(confirm) = 132dp.
    private const val QUIET_BASE_WIDTH_DP = 132
    // Mirrors pill_action_gap (8dp): the breathing room between undo and confirm. Only the
    // content column carried margins, so without this the two buttons sit flush once the quiet
    // capsule collapses the column to zero — and the capsule would be 8dp too narrow for them.
    const val ACTION_GAP_DP = 8
    // Mirrors pill_column_min_width (132dp): the room a visible wave or message needs.
    private const val CONTENT_COLUMN_WIDTH_DP = 132
    /** Mirrors @dimen/pill_height; OverlayGeometryTokenTest asserts they stay equal. */
    const val PILL_HEIGHT_DP = 64
}
