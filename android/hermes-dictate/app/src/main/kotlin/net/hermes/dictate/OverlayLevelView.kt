package net.hermes.dictate

import android.content.Context
import android.graphics.Canvas
import android.graphics.Paint
import android.util.AttributeSet
import android.view.View
import androidx.core.content.ContextCompat

/** A literal 0..100 level meter; unlike themed ProgressBar drawables its fill is width-proportional. */
class OverlayLevelView @JvmOverloads constructor(
    context: Context,
    attrs: AttributeSet? = null,
) : View(context, attrs) {
    private val paint = Paint(Paint.ANTI_ALIAS_FLAG)

    var level: Int = 0
        set(value) {
            field = value.coerceIn(0, 100)
            invalidate()
        }

    var fillColor: Int = ContextCompat.getColor(context, R.color.listening)
        set(value) {
            field = value
            invalidate()
        }

    override fun onDraw(canvas: Canvas) {
        super.onDraw(canvas)
        paint.color = ContextCompat.getColor(context, R.color.overlay_track)
        canvas.drawRect(0f, 0f, width.toFloat(), height.toFloat(), paint)
        paint.color = fillColor
        canvas.drawRect(0f, 0f, width * (level / 100f), height.toFloat(), paint)
    }
}
