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

    fun isRight(rawX: Float, screenWidthPx: Int): Boolean = rawX >= screenWidthPx / 2f
}
