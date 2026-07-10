package net.hermes.voice

import kotlin.math.max
import kotlin.math.roundToInt

/**
 * Pure sizing/quality math for the screen-capture frame pipeline.
 * No android.* imports — JVM-testable.
 */
object FrameScaler {

    /** One JPEG-encode attempt: quality in [0,1], scale factor applied to the base dimensions. */
    data class Step(val quality: Double, val scale: Double)

    /**
     * Computes the destination (width, height) so the longest edge is at most [maxEdge],
     * preserving aspect ratio. Never upscales: if the source already fits, it is returned
     * unchanged (no-op).
     */
    fun computeScaledDimensions(srcWidth: Int, srcHeight: Int, maxEdge: Int): Pair<Int, Int> {
        require(srcWidth > 0 && srcHeight > 0) { "dimensions must be positive" }
        require(maxEdge > 0) { "maxEdge must be positive" }

        val longestEdge = max(srcWidth, srcHeight)
        if (longestEdge <= maxEdge) {
            return srcWidth to srcHeight
        }

        val ratio = maxEdge.toDouble() / longestEdge.toDouble()
        val dstWidth = (srcWidth * ratio).roundToInt().coerceAtLeast(1)
        val dstHeight = (srcHeight * ratio).roundToInt().coerceAtLeast(1)
        return dstWidth to dstHeight
    }

    /**
     * The quality/dimension step-down ladder used when an encoded JPEG exceeds the size
     * budget: quality drops to 0.6 then 0.5, after which dimensions shrink by ×0.75 per
     * step (quality held at the floor). Infinite — callers cap how many steps they try
     * before dropping the frame.
     */
    fun stepDownLadder(): Sequence<Step> = sequence {
        yield(Step(quality = 0.6, scale = 1.0))
        yield(Step(quality = 0.5, scale = 1.0))
        var scale = 1.0
        while (true) {
            scale *= 0.75
            yield(Step(quality = 0.5, scale = scale))
        }
    }
}
