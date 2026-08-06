package net.hermes.dictate

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
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
    fun committedTextShowsOnlyADoneCheckmarkNoTextButOffersASeparateUndo() {
        val presentation = state(UiStatus.Done, committed = "Hermes erstellt eine PlanSpec")
        val semantics = OverlayActionSemantics.from(presentation)

        // The confirm slot is the plain "done" checkmark, not the undo arrow, and the result
        // text row stays hidden — the operator asked for "kein Text, nur ein Fertig-Symbol".
        assertEquals(OverlayConfirmAction.NONE, presentation.confirmAction)
        assertEquals(R.drawable.ic_check, semantics.confirmIcon)
        assertFalse(presentation.showMessage)
        // Undo is not lost — it moves to its own icon-only secondary action.
        assertEquals(OverlayConfirmAction.UNDO, presentation.secondaryAction)
        assertTrue(semantics.secondaryVisible)
        assertEquals(R.string.overlay_undo_desc, semantics.secondaryDescription)
        assertEquals(R.drawable.ic_undo, semantics.secondaryIcon)
    }

    @Test
    fun doneWithoutCommittedTextHasNothingToUndo() {
        val presentation = state(UiStatus.Done)
        val semantics = OverlayActionSemantics.from(presentation)

        assertEquals(OverlayConfirmAction.NONE, presentation.confirmAction)
        assertEquals(OverlayConfirmAction.NONE, presentation.secondaryAction)
        assertFalse(semantics.secondaryVisible)
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
