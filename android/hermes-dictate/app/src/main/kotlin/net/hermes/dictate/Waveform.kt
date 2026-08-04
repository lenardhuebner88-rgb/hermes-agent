package net.hermes.dictate

import kotlin.math.PI
import kotlin.math.cos
import kotlin.math.max

/**
 * Pure waveform math, separated from drawing so it can be tested on the JVM.
 *
 * The meter is a scrolling history: the newest sample enters at the right and every older sample
 * shifts one slot left. Two things make it calmer than a raw level meter — samples decay toward a
 * resting height instead of collapsing to zero, and a smooth window tapers the outermost bars so
 * the strip fades into the pill instead of ending on a hard edge.
 */
object Waveform {
    const val BAR_COUNT = 28

    /** Drawn height of a silent bar, as a fraction of the maximum. Keeps the strip alive. */
    const val REST = 0.10f

    fun emptyHistory(): FloatArray = FloatArray(BAR_COUNT)

    /**
     * Shifts [history] left by one and appends [level] (0..100) as the newest amplitude.
     * The history holds true amplitudes — the resting floor belongs to drawing ([barFraction]),
     * so reading a level back never reports loudness that was never spoken.
     */
    fun push(history: FloatArray, level: Int): FloatArray {
        val next = FloatArray(history.size)
        for (index in 0 until history.size - 1) {
            next[index] = history[index + 1]
        }
        next[history.size - 1] = level.coerceIn(0, 100) / 100f
        return next
    }

    /** The newest sample as a 0..100 level; the inverse of what [push] accepted. */
    fun latestLevel(history: FloatArray): Int =
        (history.last() * 100f).toInt().coerceIn(0, 100)

    /**
     * Final drawn height of one bar as a fraction of the maximum, including the edge taper.
     * Always at least [REST] so no bar disappears entirely.
     */
    fun barFraction(history: FloatArray, index: Int): Float {
        val amplitude = history[index]
        return max(REST, amplitude * window(index, history.size))
    }

    /**
     * A raised-cosine fade applied to the OLD end of the strip only.
     *
     * The newest sample sits at the right edge and is the whole point of a live meter — tapering
     * both ends flattened exactly the bar the user is speaking into. History fades out to the
     * left, "now" stays at full height.
     */
    fun window(index: Int, count: Int): Float {
        if (count <= 1) return 1f
        val edge = count * EDGE_FRACTION
        if (index >= edge) return 1f
        return (0.5f - 0.5f * cos(PI * (index / edge)).toFloat()).coerceIn(0f, 1f)
    }

    private const val EDGE_FRACTION = 0.22f
}
