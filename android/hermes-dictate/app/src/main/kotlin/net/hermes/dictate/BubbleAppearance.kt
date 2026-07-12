package net.hermes.dictate

object BubbleAppearance {
    val sizeStops = listOf(70, 85, 100, 115)
    val opacityStops = listOf(20, 40, 60, 80, 100)

    fun nearestSize(value: Int): Int = sizeStops.minBy { kotlin.math.abs(it - value) }
    fun nearestOpacity(value: Int): Int = opacityStops.minBy { kotlin.math.abs(it - value) }

    /** Visual sizing never shrinks the actual accessibility touch target below 48dp. */
    fun sizeDp(percent: Int, idleShrink: Boolean): Int {
        val effective = nearestSize(percent) * if (idleShrink) 0.7f else 1f
        return (56f * effective / 100f).toInt().coerceAtLeast(48)
    }
}
