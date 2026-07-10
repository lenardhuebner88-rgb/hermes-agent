package net.hermes.dictate

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class DictationControllerTest {

    private fun controller() = DictationController()

    // --- On-device push-to-talk ---

    @Test
    fun `mic tap from idle starts on-device listening`() {
        val c = controller()
        val cmds = c.micTapped()
        assertEquals(listOf<Cmd>(Cmd.StartRecognizer, Cmd.Status(UiStatus.Listening)), cmds)
        assertEquals(DictationController.Phase.LISTENING, c.phase)
    }

    @Test
    fun `partials become mapped previews`() {
        val c = controller()
        c.micTapped()
        assertEquals(listOf<Cmd>(Cmd.Preview("hallo welt.")), c.recognizerPartial("hallo welt punkt"))
    }

    @Test
    fun `final result while listening commits and chains the next segment`() {
        val c = controller()
        c.micTapped()
        val cmds = c.recognizerFinal("erster teil")
        assertEquals(listOf<Cmd>(Cmd.CommitSegment("erster teil"), Cmd.StartRecognizer), cmds)
        assertEquals(DictationController.Phase.LISTENING, c.phase)
    }

    @Test
    fun `two consecutive dictations work without restarting the IME`() {
        val c = controller()
        // First dictation: tap, final, stop tap, final.
        c.micTapped()
        assertEquals(listOf<Cmd>(Cmd.CommitSegment("erster teil"), Cmd.StartRecognizer), c.recognizerFinal("erster teil"))
        assertEquals(listOf<Cmd>(Cmd.StopRecognizer), c.micTapped())
        val firstStop = c.recognizerFinal("erster teil.")
        assertEquals(listOf<Cmd>(Cmd.CommitSegment("erster teil."), Cmd.Status(UiStatus.Idle)), firstStop)
        assertEquals(DictationController.Phase.IDLE, c.phase)

        // Second dictation in the same field: must start again from idle.
        assertEquals(listOf<Cmd>(Cmd.StartRecognizer, Cmd.Status(UiStatus.Listening)), c.micTapped())
        assertEquals(listOf<Cmd>(Cmd.Preview("zweiter teil")), c.recognizerPartial("zweiter teil"))
        assertEquals(listOf<Cmd>(Cmd.CommitSegment("zweiter teil"), Cmd.StartRecognizer), c.recognizerFinal("zweiter teil"))
        assertEquals(listOf<Cmd>(Cmd.StopRecognizer), c.micTapped())
        val secondStop = c.recognizerFinal("zweiter teil.")
        assertEquals(listOf<Cmd>(Cmd.CommitSegment("zweiter teil."), Cmd.Status(UiStatus.Idle)), secondStop)
    }

    @Test
    fun `second tap stops gracefully and the final result ends the session`() {
        val c = controller()
        c.micTapped()
        assertEquals(listOf<Cmd>(Cmd.StopRecognizer), c.micTapped())
        assertEquals(DictationController.Phase.STOPPING, c.phase)
        val cmds = c.recognizerFinal("letzter teil")
        assertEquals(listOf<Cmd>(Cmd.CommitSegment("letzter teil"), Cmd.Status(UiStatus.Idle)), cmds)
        assertEquals(DictationController.Phase.IDLE, c.phase)
    }

    @Test
    fun `empty final while stopping just clears up`() {
        val c = controller()
        c.micTapped()
        c.micTapped()
        assertEquals(
            listOf<Cmd>(Cmd.ClearPreview, Cmd.Status(UiStatus.Idle)),
            c.recognizerFinal(""),
        )
    }

    @Test
    fun `silent rounds restart the recognizer until the cap then stop visibly`() {
        val c = controller()
        c.micTapped()
        repeat(DictationController.MAX_EMPTY_ROUNDS - 1) {
            assertEquals(
                listOf<Cmd>(Cmd.ClearPreview, Cmd.StartRecognizer),
                c.recognizerError(RecognizerFailure.NO_SPEECH),
            )
        }
        val last = c.recognizerError(RecognizerFailure.NO_SPEECH)
        assertTrue(last.contains(Cmd.Status(UiStatus.Failed(ErrorKind.NO_SPEECH))))
        assertEquals(DictationController.Phase.IDLE, c.phase)
    }

    @Test
    fun `a committed segment resets the silence counter`() {
        val c = controller()
        c.micTapped()
        repeat(DictationController.MAX_EMPTY_ROUNDS - 1) {
            c.recognizerError(RecognizerFailure.NO_SPEECH)
        }
        c.recognizerFinal("text")
        // Counter reset: the next silent round restarts instead of stopping.
        assertEquals(
            listOf<Cmd>(Cmd.ClearPreview, Cmd.StartRecognizer),
            c.recognizerError(RecognizerFailure.NO_SPEECH),
        )
    }

    @Test
    fun `busy recognizer is retried a limited number of times`() {
        val c = controller()
        c.micTapped()
        repeat(DictationController.MAX_BUSY_RESTARTS) {
            assertEquals(
                listOf<Cmd>(Cmd.CancelRecognizer, Cmd.StartRecognizer),
                c.recognizerError(RecognizerFailure.BUSY),
            )
        }
        val last = c.recognizerError(RecognizerFailure.BUSY)
        assertTrue(last.contains(Cmd.Status(UiStatus.Failed(ErrorKind.RECOGNIZER_BUSY))))
        assertEquals(DictationController.Phase.IDLE, c.phase)
    }

    @Test
    fun `language unavailable surfaces visibly`() {
        val c = controller()
        c.micTapped()
        val cmds = c.recognizerError(RecognizerFailure.LANGUAGE_UNAVAILABLE)
        assertTrue(cmds.contains(Cmd.Status(UiStatus.Failed(ErrorKind.LANGUAGE_UNAVAILABLE))))
    }

    @Test
    fun `late recognizer final after stop is ignored`() {
        val c = controller()
        c.micTapped()
        c.micTapped()
        // User stops and then a delayed final from the previous segment arrives.
        c.recognizerFinal("noch da")
        // After finishing idle, a new dictation should start normally.
        assertEquals(listOf<Cmd>(Cmd.StartRecognizer, Cmd.Status(UiStatus.Listening)), c.micTapped())
    }

    @Test
    fun `late recognizer error after stop is ignored`() {
        val c = controller()
        c.micTapped()
        c.micTapped()
        c.recognizerError(RecognizerFailure.NO_SPEECH)
        assertEquals(DictationController.Phase.IDLE, c.phase)
        // Must be able to start a fresh dictation.
        assertEquals(listOf<Cmd>(Cmd.StartRecognizer, Cmd.Status(UiStatus.Listening)), c.micTapped())
    }

    // --- Cloud opt-in per use ---

    @Test
    fun `cloud toggle only works when the master switch is enabled`() {
        val c = controller()
        assertEquals(emptyList<Cmd>(), c.cloudToggleTapped(cloudEnabled = false))
        assertEquals(Mode.ON_DEVICE, c.mode)
        assertEquals(listOf<Cmd>(Cmd.ModeChanged), c.cloudToggleTapped(cloudEnabled = true))
        assertEquals(Mode.CLOUD, c.mode)
    }

    @Test
    fun `cloud toggle is ignored while dictating`() {
        val c = controller()
        c.micTapped()
        assertEquals(emptyList<Cmd>(), c.cloudToggleTapped(cloudEnabled = true))
    }

    @Test
    fun `cloud happy path records uploads commits and falls back to on-device`() {
        val c = controller()
        c.cloudToggleTapped(cloudEnabled = true)
        assertEquals(listOf<Cmd>(Cmd.StartRecording, Cmd.Status(UiStatus.Recording)), c.micTapped())
        assertEquals(listOf<Cmd>(Cmd.StopRecording), c.micTapped())
        val uploadCmds = c.recordingReady(ok = true)
        val token = (uploadCmds.first() as Cmd.Upload).token
        assertEquals(Cmd.Status(UiStatus.Uploading), uploadCmds[1])

        val done = c.uploadFinished(token, CloudOutcome.Success("hallo welt punkt", "openai"))
        assertEquals(
            listOf(
                Cmd.ModeChanged,
                Cmd.CommitSegment("hallo welt."),
                Cmd.Status(UiStatus.CloudDone("openai")),
            ),
            done,
        )
        assertEquals(Mode.ON_DEVICE, c.mode)
        assertEquals(DictationController.Phase.IDLE, c.phase)
    }

    @Test
    fun `cloud failure is visible and resets the mode`() {
        val c = controller()
        c.cloudToggleTapped(cloudEnabled = true)
        c.micTapped()
        c.micTapped()
        val token = (c.recordingReady(ok = true).first() as Cmd.Upload).token
        val cmds = c.uploadFinished(token, CloudOutcome.Network("timeout"))
        assertTrue(cmds.contains(Cmd.Status(UiStatus.Failed(ErrorKind.CLOUD_NETWORK))))
        assertTrue(cmds.contains(Cmd.ModeChanged))
        assertEquals(Mode.ON_DEVICE, c.mode)
    }

    @Test
    fun `auth failure tells the user to sign in`() {
        val c = controller()
        c.cloudToggleTapped(cloudEnabled = true)
        c.micTapped()
        c.micTapped()
        val token = (c.recordingReady(ok = true).first() as Cmd.Upload).token
        val cmds = c.uploadFinished(token, CloudOutcome.AuthRequired)
        assertTrue(cmds.contains(Cmd.Status(UiStatus.Failed(ErrorKind.CLOUD_AUTH))))
    }

    @Test
    fun `stale upload results are ignored`() {
        val c = controller()
        c.cloudToggleTapped(cloudEnabled = true)
        c.micTapped()
        c.micTapped()
        val token = (c.recordingReady(ok = true).first() as Cmd.Upload).token
        c.hidden() // invalidates the upload
        assertEquals(emptyList<Cmd>(), c.uploadFinished(token, CloudOutcome.Success("text", null)))
    }

    @Test
    fun `blank cloud transcript surfaces as empty result`() {
        val c = controller()
        c.cloudToggleTapped(cloudEnabled = true)
        c.micTapped()
        c.micTapped()
        val token = (c.recordingReady(ok = true).first() as Cmd.Upload).token
        val cmds = c.uploadFinished(token, CloudOutcome.Success("   ", null))
        assertTrue(cmds.contains(Cmd.Status(UiStatus.Failed(ErrorKind.CLOUD_EMPTY))))
    }

    @Test
    fun `failed recording is visible and resets the mode`() {
        val c = controller()
        c.cloudToggleTapped(cloudEnabled = true)
        c.micTapped()
        c.micTapped()
        val cmds = c.recordingReady(ok = false)
        assertTrue(cmds.contains(Cmd.Status(UiStatus.Failed(ErrorKind.RECORDING_FAILED))))
        assertTrue(cmds.contains(Cmd.ModeChanged))
        assertEquals(Mode.ON_DEVICE, c.mode)
    }

    @Test
    fun `max duration auto-stops the recording`() {
        val c = controller()
        c.cloudToggleTapped(cloudEnabled = true)
        c.micTapped()
        assertEquals(listOf<Cmd>(Cmd.StopRecording), c.maxDurationReached())
        assertEquals(DictationController.Phase.WAITING_FILE, c.phase)
    }

    // --- Manual keys interrupt an active dictation ---

    @Test
    fun `manual key while listening cancels the recognizer but keeps no preview command`() {
        val c = controller()
        c.micTapped()
        // The service finalizes the composing preview itself; the controller must only
        // cancel the recognizer without emitting ClearPreview (that would erase kept text).
        assertEquals(
            listOf<Cmd>(Cmd.CancelRecognizer, Cmd.Status(UiStatus.Idle)),
            c.interrupted(),
        )
        assertEquals(DictationController.Phase.IDLE, c.phase)
    }

    @Test
    fun `manual key while cloud recording aborts and resets the mode`() {
        val c = controller()
        c.cloudToggleTapped(cloudEnabled = true)
        c.micTapped()
        val cmds = c.interrupted()
        assertTrue(cmds.contains(Cmd.AbortRecording))
        assertTrue(cmds.contains(Cmd.ModeChanged))
        assertEquals(Mode.ON_DEVICE, c.mode)
        assertEquals(DictationController.Phase.IDLE, c.phase)
    }

    @Test
    fun `manual key during upload leaves the upload alone`() {
        val c = controller()
        c.cloudToggleTapped(cloudEnabled = true)
        c.micTapped()
        c.micTapped()
        val token = (c.recordingReady(ok = true).first() as Cmd.Upload).token
        assertEquals(emptyList<Cmd>(), c.interrupted())
        // The upload still lands afterwards.
        val done = c.uploadFinished(token, CloudOutcome.Success("text", null))
        assertTrue(done.contains(Cmd.CommitSegment("text")))
    }

    @Test
    fun `manual key while idle is a no-op`() {
        assertEquals(emptyList<Cmd>(), controller().interrupted())
    }

    // --- Hiding the keyboard stops everything ---

    @Test
    fun `hiding while listening cancels the recognizer`() {
        val c = controller()
        c.micTapped()
        val cmds = c.hidden()
        assertTrue(cmds.contains(Cmd.CancelRecognizer))
        assertTrue(cmds.contains(Cmd.ClearPreview))
        assertEquals(DictationController.Phase.IDLE, c.phase)
    }

    @Test
    fun `hiding while recording aborts without upload`() {
        val c = controller()
        c.cloudToggleTapped(cloudEnabled = true)
        c.micTapped()
        val cmds = c.hidden()
        assertTrue(cmds.contains(Cmd.AbortRecording))
        assertEquals(Mode.ON_DEVICE, c.mode)
    }

    @Test
    fun `taps during upload are ignored`() {
        val c = controller()
        c.cloudToggleTapped(cloudEnabled = true)
        c.micTapped()
        c.micTapped()
        c.recordingReady(ok = true)
        assertEquals(emptyList<Cmd>(), c.micTapped())
    }

    @Test
    fun `late recording file after hide is discarded`() {
        val c = controller()
        c.cloudToggleTapped(cloudEnabled = true)
        c.micTapped()
        c.hidden()
        assertEquals(listOf<Cmd>(Cmd.AbortRecording), c.recordingReady(ok = true))
        assertEquals(DictationController.Phase.IDLE, c.phase)
    }
}
