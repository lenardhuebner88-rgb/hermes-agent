package net.hermes.dictate

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class OverlayViewStateTest {

    private val err: (ErrorKind) -> String = { it.name }

    @Test
    fun `idle status collapses the pill`() {
        assertEquals(OverlayViewState.Idle, OverlayViewState.from(UiStatus.Idle, "", err))
    }

    @Test
    fun `listening shows the live preview, not busy`() {
        val s = OverlayViewState.from(UiStatus.Listening, "hallo welt", err)
        assertEquals(OverlayViewState.Dictating("hallo welt", busy = false), s)
    }

    @Test
    fun `recording (cloud) also expands the pill, not busy`() {
        val s = OverlayViewState.from(UiStatus.Recording, "", err)
        assertEquals(OverlayViewState.Dictating("", busy = false), s)
    }

    @Test
    fun `uploading expands the pill in a busy state`() {
        val s = OverlayViewState.from(UiStatus.Uploading, "letzter satz", err)
        assertEquals(OverlayViewState.Dictating("letzter satz", busy = true), s)
    }

    @Test
    fun `cloud done collapses back to idle`() {
        assertEquals(OverlayViewState.Idle, OverlayViewState.from(UiStatus.CloudDone("whisper"), "x", err))
    }

    @Test
    fun `failed status surfaces the mapped error text`() {
        val s = OverlayViewState.from(UiStatus.Failed(ErrorKind.NO_SPEECH), "", err)
        assertTrue(s is OverlayViewState.Error)
        assertEquals("NO_SPEECH", (s as OverlayViewState.Error).message)
    }
}
