package net.hermes.dictate

import android.content.Context
import android.graphics.Canvas
import android.graphics.Paint
import android.util.AttributeSet
import android.view.View
import androidx.core.content.ContextCompat

/**
 * A level-reactive ring drawn around the idle mic bubble — the compact-bubble equivalent of
 * [OverlayWaveView]'s bar strip, since a full waveform does not fit the small round bubble.
 *
 * All amplitude math lives in [BubbleRing] so it stays testable on the JVM; this class only
 * draws. At rest (level at/below [BubbleRing.REST_THRESHOLD]) it draws nothing, so the bubble
 * looks exactly like it does today until someone actually speaks.
 */
class BubbleRingView @JvmOverloads constructor(
    context: Context,
    attrs: AttributeSet? = null,
) : View(context, attrs) {

    private val paint = Paint(Paint.ANTI_ALIAS_FLAG).apply { style = Paint.Style.STROKE }

    var level: Int = 0
        set(value) {
            field = value.coerceIn(0, 100)
            invalidate()
        }

    var ringColor: Int = ContextCompat.getColor(context, R.color.listening)
        set(value) {
            field = value
            invalidate()
        }

    override fun onDraw(canvas: Canvas) {
        super.onDraw(canvas)
        val alpha = BubbleRing.alpha(level)
        if (alpha <= 0 || width == 0 || height == 0) return

        val strokeWidth = resources.displayMetrics.density * STROKE_WIDTH_DP
        paint.strokeWidth = strokeWidth
        paint.color = ringColor
        paint.alpha = alpha

        val baseRadius = (width.coerceAtMost(height) / 2f) - strokeWidth
        val extra = BubbleRing.extraRadiusPx(level, maxExtraPx = strokeWidth * 2.5f)
        canvas.drawCircle(width / 2f, height / 2f, (baseRadius + extra).coerceAtLeast(0f), paint)
    }

    companion object {
        private const val STROKE_WIDTH_DP = 2.5f
    }
}
