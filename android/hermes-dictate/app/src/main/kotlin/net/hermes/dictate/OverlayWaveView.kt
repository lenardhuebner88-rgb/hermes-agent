package net.hermes.dictate

import android.content.Context
import android.graphics.Canvas
import android.graphics.Paint
import android.graphics.RectF
import android.util.AttributeSet
import android.view.View
import androidx.core.content.ContextCompat

/**
 * The live voice waveform: a strip of rounded bars, newest sample on the right.
 *
 * All amplitude math lives in [Waveform] so it stays testable on the JVM; this class only draws.
 * There is deliberately no animation loop — every frame is driven by a real microphone level, so
 * the strip is quiet when the room is quiet instead of pulsing at the user.
 *
 * The `level` property keeps the old level-meter contract: writing pushes a sample, reading
 * returns the newest one.
 */
class OverlayWaveView @JvmOverloads constructor(
    context: Context,
    attrs: AttributeSet? = null,
) : View(context, attrs) {

    private val paint = Paint(Paint.ANTI_ALIAS_FLAG)
    private val bar = RectF()
    private var history = Waveform.emptyHistory()

    var level: Int
        get() = Waveform.latestLevel(history)
        set(value) {
            history = Waveform.push(history, value)
            invalidate()
        }

    var fillColor: Int = ContextCompat.getColor(context, R.color.listening)
        set(value) {
            field = value
            invalidate()
        }

    /** Drops the strip back to its resting line, e.g. when a dictation ends. */
    fun reset() {
        history = Waveform.emptyHistory()
        invalidate()
    }

    override fun onDraw(canvas: Canvas) {
        super.onDraw(canvas)
        val count = history.size
        if (count == 0 || width == 0 || height == 0) return

        // Bars and gaps share the width; the gap is 55 % of a bar so the strip reads as a wave
        // rather than as a solid block.
        val slot = width.toFloat() / count
        val barWidth = slot / 1.55f
        val radius = barWidth / 2f
        val centerY = height / 2f
        val maxHalf = height / 2f - radius * 0.15f

        paint.color = fillColor
        for (index in 0 until count) {
            val fraction = Waveform.barFraction(history, index)
            val half = (maxHalf * fraction).coerceAtLeast(radius)
            val centerX = slot * index + slot / 2f
            bar.set(centerX - radius, centerY - half, centerX + radius, centerY + half)
            // The taper also dims the outer bars, so the strip fades into the pill.
            paint.alpha = (255 * (0.35f + 0.65f * Waveform.window(index, count))).toInt().coerceIn(0, 255)
            canvas.drawRoundRect(bar, radius, radius, paint)
        }
    }
}
