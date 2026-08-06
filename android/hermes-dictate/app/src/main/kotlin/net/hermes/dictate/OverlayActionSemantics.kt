package net.hermes.dictate

/** One source of truth for the iconography and spoken meaning of pill actions. */
data class OverlayActionSemantics(
    val cancelDescription: Int,
    val confirmDescription: Int,
    val confirmIcon: Int,
    /** Whether the separate icon-only undo button (pill_undo) should be shown at all. */
    val secondaryVisible: Boolean,
    val secondaryDescription: Int,
    val secondaryIcon: Int,
) {
    companion object {
        fun from(state: OverlayViewState): OverlayActionSemantics {
            val cancelDescription = when (state.label) {
                OverlayLabel.LISTENING,
                OverlayLabel.PROCESSING,
                OverlayLabel.RECORDING,
                OverlayLabel.UPLOADING,
                -> R.string.overlay_cancel_desc
                else -> R.string.overlay_dismiss_desc
            }
            val (confirmDescription, confirmIcon) = when (state.confirmAction) {
                OverlayConfirmAction.STOP -> R.string.overlay_stop_desc to R.drawable.ic_stop
                OverlayConfirmAction.RETRY -> R.string.retry_cloud to R.drawable.ic_retry
                OverlayConfirmAction.COPY -> R.string.copy_recent to R.drawable.ic_copy
                // Not reachable from confirmAction any more (undo lives in secondaryAction), but
                // the branch stays for exhaustiveness.
                OverlayConfirmAction.UNDO -> R.string.overlay_undo_desc to R.drawable.ic_undo
                OverlayConfirmAction.NONE -> when (state.label) {
                    // A failed cloud dictation with no retry left must say so, not "no action".
                    OverlayLabel.ERROR -> R.string.overlay_retry_exhausted_desc to R.drawable.ic_check
                    // The confirm slot only ever shows the "done" checkmark now — no status word.
                    OverlayLabel.DONE -> R.string.overlay_done_desc to R.drawable.ic_check
                    else -> R.string.overlay_confirm_unavailable_desc to R.drawable.ic_check
                }
            }
            val secondaryVisible = state.secondaryAction == OverlayConfirmAction.UNDO
            return OverlayActionSemantics(
                cancelDescription,
                confirmDescription,
                confirmIcon,
                secondaryVisible = secondaryVisible,
                secondaryDescription = R.string.overlay_undo_desc,
                secondaryIcon = R.drawable.ic_undo,
            )
        }
    }
}
