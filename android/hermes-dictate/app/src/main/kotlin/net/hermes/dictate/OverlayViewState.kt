package net.hermes.dictate

/**
 * Maps [UiStatus] (from [DictationController]) plus the current live preview text onto what the
 * overlay bubble should show. Pure so it is unit-testable without an Accessibility runtime; the
 * bubble/pill views in [DictateOverlayService] are dumb renderers of this state.
 */
sealed class OverlayViewState {
    /** Small mic bubble, nothing in flight. */
    object Idle : OverlayViewState()

    /** Expanded pill: cancel / live text or a listening placeholder / confirm. */
    data class Dictating(val text: String, val busy: Boolean) : OverlayViewState()

    /** Expanded pill, red flash with an error message, then auto-collapses. */
    data class Error(val message: String) : OverlayViewState()

    companion object {
        fun from(status: UiStatus, previewText: String, errorText: (ErrorKind) -> String): OverlayViewState =
            when (status) {
                UiStatus.Idle -> Idle
                UiStatus.Listening, UiStatus.Recording -> Dictating(previewText, busy = false)
                UiStatus.Uploading -> Dictating(previewText, busy = true)
                is UiStatus.CloudDone -> Idle
                is UiStatus.Failed -> Error(errorText(status.kind))
            }
    }
}
