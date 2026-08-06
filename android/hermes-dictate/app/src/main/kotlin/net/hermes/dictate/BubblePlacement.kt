package net.hermes.dictate

data class BubbleScreen(
    val widthPx: Int,
    val heightPx: Int,
    val topInsetPx: Int,
    val bottomInsetPx: Int,
)

/** Insets-aware placement rules shared by restore, drag and edge snap. */
object BubblePlacement {
    fun clampY(requestedY: Int, bubbleHeightPx: Int, screen: BubbleScreen, marginPx: Int): Int {
        val minimum = screen.topInsetPx + marginPx
        val maximum = (
            screen.heightPx - screen.bottomInsetPx - marginPx - bubbleHeightPx
            ).coerceAtLeast(minimum)
        return requestedY.coerceIn(minimum, maximum)
    }

    fun centeredY(bubbleHeightPx: Int, screen: BubbleScreen, marginPx: Int): Int {
        val availableTop = screen.topInsetPx + marginPx
        val availableBottom = screen.heightPx - screen.bottomInsetPx - marginPx
        val requested = availableTop + (availableBottom - availableTop - bubbleHeightPx) / 2
        return clampY(requested, bubbleHeightPx, screen, marginPx)
    }

    /**
     * Same contract as [clampY], horizontally: with FLAG_LAYOUT_NO_LIMITS and a persisted X the
     * bubble could otherwise be parked off-screen permanently — including after a rotation or a
     * resolution change that makes a previously-valid X invalid.
     */
    fun clampX(requestedX: Int, bubbleWidthPx: Int, screen: BubbleScreen, marginPx: Int): Int {
        val minimum = marginPx
        val maximum = (screen.widthPx - marginPx - bubbleWidthPx).coerceAtLeast(minimum)
        return requestedX.coerceIn(minimum, maximum)
    }

    fun isRight(rawX: Float, screenWidthPx: Int): Boolean = rawX >= screenWidthPx / 2f

    /**
     * Migration for installs that predate free-drag placement: they only ever recorded which
     * edge the bubble was docked to, not a pixel X. Derive a starting X from that edge so a user
     * who was docked right stays right after the update instead of jumping to x=0.
     */
    fun migratedX(onRight: Boolean, bubbleWidthPx: Int, screen: BubbleScreen, marginPx: Int): Int {
        val requested = if (onRight) screen.widthPx - marginPx - bubbleWidthPx else marginPx
        return clampX(requested, bubbleWidthPx, screen, marginPx)
    }
}
