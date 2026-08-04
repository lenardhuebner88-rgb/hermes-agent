package net.hermes.dictate

import org.junit.Assert.assertEquals
import org.junit.Test

class OverlayActionSemanticsTest {
    @Test
    fun listeningUsesStopIconAndAbortMeaning() {
        val semantics = OverlayActionSemantics.from(state(UiStatus.Listening))

        assertEquals(R.string.overlay_cancel_desc, semantics.cancelDescription)
        assertEquals(R.string.overlay_stop_desc, semantics.confirmDescription)
        assertEquals(R.drawable.ic_stop, semantics.confirmIcon)
    }

    @Test
    fun retryableErrorUsesRetryIconAndDismissMeaning() {
        val presentation = OverlayViewState.from(
            status = UiStatus.Failed(ErrorKind.CLOUD_NETWORK),
            previewText = "",
            committedText = null,
            retryAvailable = true,
            errorText = { "Nicht erreichbar" },
        )
        val semantics = OverlayActionSemantics.from(presentation)

        assertEquals(R.string.overlay_dismiss_desc, semantics.cancelDescription)
        assertEquals(R.string.retry_cloud, semantics.confirmDescription)
        assertEquals(R.drawable.ic_retry, semantics.confirmIcon)
    }

    @Test
    fun copyStateUsesCopyIcon() {
        val semantics = OverlayActionSemantics.from(
            state(UiStatus.CloudDone(provider = "Hermes"), committed = "Text"),
        )

        assertEquals(R.string.copy_recent, semantics.confirmDescription)
        assertEquals(R.drawable.ic_copy, semantics.confirmIcon)
    }

    @Test
    fun committedTextOffersAVisibleUndo() {
        val presentation = state(UiStatus.Done, committed = "Hermes erstellt eine PlanSpec")
        val semantics = OverlayActionSemantics.from(presentation)

        assertEquals(OverlayConfirmAction.UNDO, presentation.confirmAction)
        assertEquals(R.string.overlay_undo_desc, semantics.confirmDescription)
        assertEquals(R.drawable.ic_undo, semantics.confirmIcon)
    }

    @Test
    fun doneWithoutCommittedTextHasNothingToUndo() {
        val presentation = state(UiStatus.Done)

        assertEquals(OverlayConfirmAction.NONE, presentation.confirmAction)
    }

    @Test
    fun exhaustedRetrySaysSoInsteadOfClaimingNoAction() {
        val presentation = OverlayViewState.from(
            status = UiStatus.Failed(ErrorKind.CLOUD_NETWORK),
            previewText = "",
            committedText = null,
            retryAvailable = false,
        ) { "Fehler" }

        assertEquals(OverlayConfirmAction.NONE, presentation.confirmAction)
        assertEquals(
            R.string.overlay_retry_exhausted_desc,
            OverlayActionSemantics.from(presentation).confirmDescription,
        )
    }

    private fun state(status: UiStatus, committed: String? = null): OverlayViewState =
        OverlayViewState.from(status, "", committed, false) { "Fehler" }
}
