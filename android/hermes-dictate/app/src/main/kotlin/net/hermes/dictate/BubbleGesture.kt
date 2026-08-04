package net.hermes.dictate

/** Pure gesture arbitration for the collapsed bubble; Android events are adapted by the service. */
class BubbleGesture(private val touchSlopPx: Float) {
    enum class Finish { TAP, DRAG, LONG_PRESS, CANCEL }

    private var downX = 0f
    private var downY = 0f
    var moved: Boolean = false
        private set

    fun begin(rawX: Float, rawY: Float) {
        downX = rawX
        downY = rawY
        moved = false
    }

    fun move(rawX: Float, rawY: Float): Boolean {
        if (!moved) {
            val dx = rawX - downX
            val dy = rawY - downY
            moved = dx * dx + dy * dy > touchSlopPx * touchSlopPx
        }
        return moved
    }

    fun finish(cancelled: Boolean, longPressed: Boolean): Finish = when {
        cancelled -> Finish.CANCEL
        longPressed -> Finish.LONG_PRESS
        moved -> Finish.DRAG
        else -> Finish.TAP
    }
}
