package net.hermes.dictate

/** Keeps the newest live words visible without discarding the full accessible transcript. */
object OverlayText {
    fun visibleBody(state: OverlayViewState, maxLiveChars: Int = 54): String? {
        val body = state.body ?: return null
        val isLive = state.label == OverlayLabel.LISTENING ||
            state.label == OverlayLabel.PROCESSING ||
            state.label == OverlayLabel.RECORDING ||
            state.label == OverlayLabel.UPLOADING
        if (!isLive || body.length <= maxLiveChars) return body

        val tail = body.takeLast((maxLiveChars - 2).coerceAtLeast(1))
        val wordBoundary = tail.indexOf(' ')
        val cleanTail = if (wordBoundary in 1 until tail.lastIndex) {
            tail.substring(wordBoundary + 1)
        } else {
            tail
        }
        return "… $cleanTail"
    }
}
