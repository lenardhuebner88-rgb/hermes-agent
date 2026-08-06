package net.hermes.dictate

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class OverlayViewStateTest {
    private val err: (ErrorKind) -> String = { "Fehler: ${it.name}" }

    private fun state(
        status: UiStatus,
        preview: String = "",
        committed: String? = null,
        retry: Boolean = false,
        cloud: Boolean = false,
    ) = OverlayViewState.from(status, preview, committed, retry, cloud, err)

    @Test
    fun `the mode marker follows the active recognizer`() {
        assertEquals(OverlayTone.LISTENING, state(UiStatus.Listening).modeTone)
        assertEquals(OverlayTone.CLOUD, state(UiStatus.Listening, cloud = true).modeTone)
        // Idle still has to say which recognizer the next dictation would use.
        assertEquals(OverlayTone.CLOUD, state(UiStatus.Idle, cloud = true).modeTone)
        // A cloud upload is a cloud dictation even when the preference was not read yet.
        assertEquals(OverlayTone.CLOUD, state(UiStatus.Uploading).modeTone)
    }

    @Test
    fun `idle is the only collapsed presentation`() {
        val value = state(UiStatus.Idle)
        assertFalse(value.expanded)
        assertEquals(OverlayLabel.READY, value.label)
        assertNull(value.body)
        assertEquals(OverlayConfirmAction.NONE, value.confirmAction)
    }

    @Test
    fun `listening keeps growing text and exposes stop plus cancel`() {
        val value = state(UiStatus.Listening, preview = "Hermes erstellt eine PlanSpec")
        assertTrue(value.expanded)
        assertEquals(OverlayLabel.LISTENING, value.label)
        assertEquals("Hermes erstellt eine PlanSpec", value.body)
        assertEquals(OverlayTone.LISTENING, value.tone)
        assertTrue(value.cancelEnabled)
        assertEquals(OverlayConfirmAction.STOP, value.confirmAction)
    }

    @Test
    fun `processing preserves text but has no misleading confirm action`() {
        val value = state(UiStatus.Processing, preview = "Letzter Satz")
        assertEquals("Letzter Satz", value.body)
        assertEquals(OverlayLabel.PROCESSING, value.label)
        assertTrue(value.cancelEnabled)
        assertEquals(OverlayConfirmAction.NONE, value.confirmAction)
    }

    @Test
    fun `upload is visibly busy and cannot pretend to cancel`() {
        val value = state(UiStatus.Uploading, preview = "Cloud-Satz")
        assertEquals(OverlayLabel.UPLOADING, value.label)
        assertFalse(value.cancelEnabled)
        assertEquals(OverlayConfirmAction.NONE, value.confirmAction)
    }

    @Test
    fun `done uses committed text and makes Hermes handoff explicit`() {
        val value = state(UiStatus.Done, preview = "alt", committed = "Finaler Satz.")
        assertEquals(OverlayLabel.DONE, value.label)
        assertEquals("Finaler Satz.", value.body)
        assertEquals(OverlayTone.SUCCESS, value.tone)
        assertTrue(value.hermesEnabled)
    }

    @Test
    fun `done shows no text row, just the confirm checkmark, and offers undo as a separate action`() {
        val committed = state(UiStatus.Done, committed = "Finaler Satz.")
        assertEquals(OverlayConfirmAction.NONE, committed.confirmAction)
        assertEquals(OverlayConfirmAction.UNDO, committed.secondaryAction)
        assertFalse(committed.showMessage)

        val withoutCommit = state(UiStatus.Done, preview = "alt")
        assertEquals(OverlayConfirmAction.NONE, withoutCommit.secondaryAction)
        assertFalse(withoutCommit.showMessage)
    }

    @Test
    fun `cloud done offers copy while preserving the committed text`() {
        val value = state(UiStatus.CloudDone("test"), committed = "Cloud final.")
        assertEquals(OverlayLabel.COPY, value.label)
        assertEquals("Cloud final.", value.body)
        assertEquals(OverlayConfirmAction.COPY, value.confirmAction)
        assertTrue(value.cancelEnabled)
        assertTrue(value.showMessage)
    }

    @Test
    fun `only retryable failures expose retry`() {
        val plain = state(UiStatus.Failed(ErrorKind.NO_SPEECH))
        val retry = state(UiStatus.Failed(ErrorKind.CLOUD_NETWORK), retry = true)
        assertEquals("Fehler: NO_SPEECH", plain.body)
        assertEquals(OverlayLabel.ERROR, plain.label)
        assertEquals(OverlayConfirmAction.NONE, plain.confirmAction)
        assertEquals(OverlayConfirmAction.RETRY, retry.confirmAction)
        assertTrue(plain.showMessage)
    }
}
