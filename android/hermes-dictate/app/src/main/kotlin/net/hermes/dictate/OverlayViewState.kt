package net.hermes.dictate

/** Visual tone is deliberately separate from copy so state never relies on color alone. */
enum class OverlayTone { READY, LISTENING, PROCESSING, SUCCESS, ERROR, CLOUD }

enum class OverlayLabel { READY, LISTENING, PROCESSING, DONE, RECORDING, UPLOADING, ERROR, COPY }

enum class OverlayConfirmAction { STOP, RETRY, COPY, UNDO, NONE }

/**
 * Complete, pure presentation contract for the accessibility overlay.
 *
 * The service renders this object instead of independently mutating text, buttons and colors.
 * That keeps the visible label, available actions and lifecycle state in sync.
 */
data class OverlayViewState(
    val expanded: Boolean,
    val label: OverlayLabel,
    val body: String?,
    val tone: OverlayTone,
    val cancelEnabled: Boolean,
    val confirmAction: OverlayConfirmAction,
    val hermesEnabled: Boolean,
    /** Whether this dictation runs, or would run, through the opt-in cloud recognizer. */
    val cloudMode: Boolean = false,
) {
    /** Mode marker tone. Never the only carrier of the mode — the status copy names it too. */
    val modeTone: OverlayTone
        get() = if (cloudMode) OverlayTone.CLOUD else OverlayTone.LISTENING

    companion object {
        fun from(
            status: UiStatus,
            previewText: String,
            committedText: String?,
            retryAvailable: Boolean,
            cloudMode: Boolean = false,
            // Kept last so callers can pass it as a trailing lambda.
            errorText: (ErrorKind) -> String,
        ): OverlayViewState {
            val preview = previewText.trim().takeIf(String::isNotEmpty)
            val committed = committedText?.trim()?.takeIf(String::isNotEmpty)
            val base = when (status) {
                UiStatus.Idle -> OverlayViewState(
                    expanded = false,
                    label = OverlayLabel.READY,
                    body = null,
                    tone = OverlayTone.READY,
                    cancelEnabled = false,
                    confirmAction = OverlayConfirmAction.NONE,
                    hermesEnabled = false,
                )
                UiStatus.Listening -> active(
                    label = OverlayLabel.LISTENING,
                    body = preview,
                    tone = OverlayTone.LISTENING,
                    cancelEnabled = true,
                    confirmAction = OverlayConfirmAction.STOP,
                )
                UiStatus.Recording -> active(
                    label = OverlayLabel.RECORDING,
                    body = preview,
                    tone = OverlayTone.CLOUD,
                    cancelEnabled = true,
                    confirmAction = OverlayConfirmAction.STOP,
                )
                UiStatus.Processing -> active(
                    label = OverlayLabel.PROCESSING,
                    body = preview,
                    tone = OverlayTone.PROCESSING,
                    cancelEnabled = true,
                )
                UiStatus.Uploading -> active(
                    label = OverlayLabel.UPLOADING,
                    body = preview,
                    tone = OverlayTone.CLOUD,
                    cancelEnabled = false,
                )
                // Undo existed only as a spoken phrase nobody could discover. In the success
                // state the confirm slot is free, so the just-written text can be taken back
                // with one visible tap.
                UiStatus.Done -> active(
                    label = OverlayLabel.DONE,
                    body = committed ?: preview,
                    tone = OverlayTone.SUCCESS,
                    cancelEnabled = false,
                    confirmAction = if (committed != null) {
                        OverlayConfirmAction.UNDO
                    } else {
                        OverlayConfirmAction.NONE
                    },
                    hermesEnabled = committed != null || preview != null,
                )
                is UiStatus.CloudDone -> active(
                    label = OverlayLabel.COPY,
                    body = committed,
                    tone = OverlayTone.SUCCESS,
                    cancelEnabled = true,
                    confirmAction = OverlayConfirmAction.COPY,
                    hermesEnabled = committed != null,
                )
                is UiStatus.Failed -> active(
                    label = OverlayLabel.ERROR,
                    body = errorText(status.kind),
                    tone = OverlayTone.ERROR,
                    cancelEnabled = true,
                    confirmAction = if (retryAvailable) {
                        OverlayConfirmAction.RETRY
                    } else {
                        OverlayConfirmAction.NONE
                    },
                )
            }
            // The recognizer that is actually in use overrides the per-status default, so the
            // marker is right during Idle and Listening too, not only while uploading.
            return base.copy(cloudMode = cloudMode || base.tone == OverlayTone.CLOUD)
        }

        private fun active(
            label: OverlayLabel,
            body: String?,
            tone: OverlayTone,
            cancelEnabled: Boolean,
            confirmAction: OverlayConfirmAction = OverlayConfirmAction.NONE,
            hermesEnabled: Boolean = false,
        ) = OverlayViewState(
            expanded = true,
            label = label,
            body = body,
            tone = tone,
            cancelEnabled = cancelEnabled,
            confirmAction = confirmAction,
            hermesEnabled = hermesEnabled,
        )
    }
}
