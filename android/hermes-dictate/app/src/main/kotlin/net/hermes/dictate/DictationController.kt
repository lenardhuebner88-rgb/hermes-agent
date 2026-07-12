package net.hermes.dictate

/** Side effects the controller asks the IME service to perform, in order. */
sealed class Cmd {
    /** Start (or chain-restart) an on-device recognition segment. */
    object StartRecognizer : Cmd()

    /** Graceful stop — final results are still expected. */
    object StopRecognizer : Cmd()

    /** Hard abort — no further recognizer events matter. */
    object CancelRecognizer : Cmd()

    object StartRecording : Cmd()

    /** Stop the recorder; the service reports back via [DictationController.recordingReady]. */
    object StopRecording : Cmd()

    /** Discard recorder state and any buffered audio without uploading. */
    object AbortRecording : Cmd()

    /** Upload the buffered audio; [token] guards against stale results. */
    data class Upload(val token: Int) : Cmd()

    /** Show an in-flight segment as composing text (already punctuation-mapped). */
    data class Preview(val text: String) : Cmd()

    /** Commit a finished segment (already punctuation-mapped) into the field. */
    data class CommitSegment(val text: String) : Cmd()

    object UndoLastSegment : Cmd()
    object DeleteLastSentence : Cmd()

    object ClearPreview : Cmd()
    data class Status(val status: UiStatus) : Cmd()

    /** The on-device/cloud mode changed (toggle or per-use reset) — refresh the chip. */
    object ModeChanged : Cmd()
}

enum class Mode { ON_DEVICE, CLOUD }

sealed class UiStatus {
    object Idle : UiStatus()
    object Listening : UiStatus()
    object Recording : UiStatus()
    object Uploading : UiStatus()
    data class CloudDone(val provider: String?) : UiStatus()
    data class Failed(val kind: ErrorKind) : UiStatus()
}

enum class ErrorKind {
    NO_SPEECH,
    LANGUAGE_UNAVAILABLE,
    RECOGNIZER_UNAVAILABLE,
    RECOGNIZER_BUSY,
    RECOGNIZER_OTHER,
    MIC_PERMISSION,
    RECORDING_FAILED,
    CLOUD_AUTH,
    CLOUD_NETWORK,
    CLOUD_SERVER,
    CLOUD_TOO_LARGE,
    CLOUD_EMPTY,

    /** Overlay only: the recognized text could not be inserted into the focused field. */
    INSERT_FAILED,
}

/** Semantic view of SpeechRecognizer error codes; the service does the int mapping. */
enum class RecognizerFailure {
    NO_SPEECH,
    BUSY,
    LANGUAGE_UNAVAILABLE,
    UNAVAILABLE,
    PERMISSION,
    OTHER,
}

sealed class CloudOutcome {
    data class Success(val transcript: String, val provider: String?) : CloudOutcome()
    object AuthRequired : CloudOutcome()
    data class Network(val detail: String) : CloudOutcome()
    data class Server(val detail: String) : CloudOutcome()
    object TooLarge : CloudOutcome()
}

/**
 * Pure push-to-talk state machine: one mic button drives everything, every transition returns
 * the side effects as [Cmd]s. Keeping this free of Android types makes the whole dictation
 * flow unit-testable on the host JVM.
 *
 * On-device dictation chains recognizer segments (commit each final result, restart) until the
 * user taps stop or [MAX_EMPTY_ROUNDS] consecutive silent rounds pass. Cloud mode is opt-in
 * PER USE (PlanSpec): after every upload — success or failure — the mode falls back to
 * ON_DEVICE, and failures surface visibly instead of silently retrying cloud.
 */
class DictationController(
    private val transform: (String) -> DictationTransform = {
        DictationTransform.Text(PunctuationMapper.map(it))
    },
) {
    enum class Phase { IDLE, LISTENING, STOPPING, RECORDING, WAITING_FILE, UPLOADING }

    var phase: Phase = Phase.IDLE
        private set
    var mode: Mode = Mode.ON_DEVICE
        private set

    private var emptyRounds = 0
    private var busyRestarts = 0
    private var uploadToken = 0
    private var retryAvailable = false
    private var retryConsumed = false

    fun micTapped(): List<Cmd> = when (phase) {
        Phase.IDLE -> {
            emptyRounds = 0
            busyRestarts = 0
            if (mode == Mode.CLOUD) {
                phase = Phase.RECORDING
                listOf(Cmd.StartRecording, Cmd.Status(UiStatus.Recording))
            } else {
                phase = Phase.LISTENING
                listOf(Cmd.StartRecognizer, Cmd.Status(UiStatus.Listening))
            }
        }
        Phase.LISTENING -> {
            phase = Phase.STOPPING
            listOf(Cmd.StopRecognizer)
        }
        Phase.RECORDING -> {
            phase = Phase.WAITING_FILE
            listOf(Cmd.StopRecording)
        }
        // Taps while a stop or upload is already in flight are ignored.
        Phase.STOPPING, Phase.WAITING_FILE, Phase.UPLOADING -> emptyList()
    }

    /** Cloud chip tap. Only meaningful while idle and while the master opt-in is enabled. */
    fun cloudToggleTapped(cloudEnabled: Boolean): List<Cmd> {
        if (phase != Phase.IDLE) return emptyList()
        val newMode = if (mode == Mode.CLOUD || !cloudEnabled) Mode.ON_DEVICE else Mode.CLOUD
        if (newMode == mode) return emptyList()
        mode = newMode
        return listOf(Cmd.ModeChanged)
    }

    fun recognizerPartial(text: String): List<Cmd> {
        if (phase != Phase.LISTENING && phase != Phase.STOPPING) return emptyList()
        val result = transform(text)
        return if (result is DictationTransform.Text && result.value.isNotEmpty()) {
            listOf(Cmd.Preview(result.value))
        } else {
            emptyList()
        }
    }

    fun recognizerFinal(text: String): List<Cmd> = when (phase) {
        Phase.LISTENING -> {
            val result = transform(text)
            if (result is DictationTransform.Text && result.value.isEmpty()) {
                emptySegmentRound()
            } else {
                emptyRounds = 0
                commandsFor(result) + Cmd.StartRecognizer
            }
        }
        Phase.STOPPING -> {
            phase = Phase.IDLE
            val result = transform(text)
            if (result is DictationTransform.Text && result.value.isEmpty()) {
                listOf(Cmd.ClearPreview, Cmd.Status(UiStatus.Idle))
            } else {
                commandsFor(result) + Cmd.Status(UiStatus.Idle)
            }
        }
        else -> emptyList()
    }

    fun recognizerError(failure: RecognizerFailure): List<Cmd> = when (phase) {
        Phase.LISTENING -> when (failure) {
            RecognizerFailure.NO_SPEECH -> emptySegmentRound()
            RecognizerFailure.BUSY ->
                if (busyRestarts < MAX_BUSY_RESTARTS) {
                    busyRestarts += 1
                    listOf(Cmd.CancelRecognizer, Cmd.StartRecognizer)
                } else {
                    failStop(ErrorKind.RECOGNIZER_BUSY)
                }
            RecognizerFailure.LANGUAGE_UNAVAILABLE -> failStop(ErrorKind.LANGUAGE_UNAVAILABLE)
            RecognizerFailure.UNAVAILABLE -> failStop(ErrorKind.RECOGNIZER_UNAVAILABLE)
            RecognizerFailure.PERMISSION -> failStop(ErrorKind.MIC_PERMISSION)
            RecognizerFailure.OTHER -> failStop(ErrorKind.RECOGNIZER_OTHER)
        }
        Phase.STOPPING -> {
            // The user already asked to stop; whatever went wrong, just end quietly.
            phase = Phase.IDLE
            listOf(Cmd.ClearPreview, Cmd.Status(UiStatus.Idle))
        }
        else -> emptyList()
    }

    /** Recorder failed to start or died mid-recording. */
    fun recordingError(): List<Cmd> {
        if (phase != Phase.RECORDING && phase != Phase.WAITING_FILE) return emptyList()
        phase = Phase.IDLE
        return listOf(Cmd.AbortRecording) + resetModeToOnDevice() +
            Cmd.Status(UiStatus.Failed(ErrorKind.RECORDING_FAILED))
    }

    fun maxDurationReached(): List<Cmd> {
        if (phase != Phase.RECORDING) return emptyList()
        phase = Phase.WAITING_FILE
        return listOf(Cmd.StopRecording)
    }

    /** The service stopped the recorder; [ok] = a non-empty recording was captured. */
    fun recordingReady(ok: Boolean): List<Cmd> {
        if (phase != Phase.WAITING_FILE) return listOf(Cmd.AbortRecording)
        return if (ok) {
            retryAvailable = false
            retryConsumed = false
            phase = Phase.UPLOADING
            uploadToken += 1
            listOf(Cmd.Upload(uploadToken), Cmd.Status(UiStatus.Uploading))
        } else {
            phase = Phase.IDLE
            listOf(Cmd.AbortRecording) + resetModeToOnDevice() +
                Cmd.Status(UiStatus.Failed(ErrorKind.RECORDING_FAILED))
        }
    }

    fun uploadFinished(token: Int, outcome: CloudOutcome): List<Cmd> {
        if (phase != Phase.UPLOADING || token != uploadToken) return emptyList()
        phase = Phase.IDLE
        // Cloud is opt-in per use: whatever happened, the next dictation is on-device again.
        val cmds = resetModeToOnDevice().toMutableList()
        when (outcome) {
            is CloudOutcome.Success -> {
                retryAvailable = false
                val result = transform(outcome.transcript)
                if (result is DictationTransform.Text && result.value.isEmpty()) {
                    cmds += Cmd.Status(UiStatus.Failed(ErrorKind.CLOUD_EMPTY))
                } else {
                    cmds += commandsFor(result)
                    cmds += Cmd.Status(UiStatus.CloudDone(outcome.provider))
                }
            }
            CloudOutcome.AuthRequired -> cmds += Cmd.Status(UiStatus.Failed(ErrorKind.CLOUD_AUTH))
            is CloudOutcome.Network -> {
                retryAvailable = !retryConsumed
                cmds += Cmd.Status(UiStatus.Failed(ErrorKind.CLOUD_NETWORK))
            }
            is CloudOutcome.Server -> {
                retryAvailable = !retryConsumed
                cmds += Cmd.Status(UiStatus.Failed(ErrorKind.CLOUD_SERVER))
            }
            CloudOutcome.TooLarge -> cmds += Cmd.Status(UiStatus.Failed(ErrorKind.CLOUD_TOO_LARGE))
        }
        return cmds
    }

    /** Exactly one explicit retry is allowed for the most recent retryable cloud failure. */
    fun retryCloud(): List<Cmd> {
        if (phase != Phase.IDLE || !retryAvailable || retryConsumed) return emptyList()
        retryAvailable = false
        retryConsumed = true
        uploadToken += 1
        phase = Phase.UPLOADING
        return listOf(Cmd.Upload(uploadToken), Cmd.Status(UiStatus.Uploading))
    }

    /**
     * The user pressed a manual key (space, punctuation, enter, backspace) while dictation was
     * active. The service finalizes any composing preview BEFORE calling this, so the partial
     * text survives as committed text; the recognizer is cancelled so no final result arrives
     * on top of it, and a cloud recording is aborted (typing means the user moved on).
     * An in-flight upload is left alone — its commit inserts at the (possibly moved) cursor.
     */
    fun interrupted(): List<Cmd> = when (phase) {
        Phase.LISTENING, Phase.STOPPING -> {
            phase = Phase.IDLE
            listOf(Cmd.CancelRecognizer, Cmd.Status(UiStatus.Idle))
        }
        Phase.RECORDING, Phase.WAITING_FILE -> {
            phase = Phase.IDLE
            listOf(Cmd.AbortRecording) + resetModeToOnDevice() + Cmd.Status(UiStatus.Idle)
        }
        Phase.UPLOADING, Phase.IDLE -> emptyList()
    }

    /**
     * The input view went away (field closed, app switch, keyboard hidden). Everything that
     * touches the mic stops HARD; an in-flight upload result is invalidated because its target
     * field no longer exists.
     */
    fun hidden(): List<Cmd> {
        val cmds = mutableListOf<Cmd>()
        when (phase) {
            Phase.LISTENING, Phase.STOPPING -> {
                cmds += Cmd.CancelRecognizer
                cmds += Cmd.ClearPreview
            }
            Phase.RECORDING, Phase.WAITING_FILE -> cmds += Cmd.AbortRecording
            Phase.UPLOADING -> uploadToken += 1
            Phase.IDLE -> {}
        }
        phase = Phase.IDLE
        cmds += resetModeToOnDevice()
        cmds += Cmd.Status(UiStatus.Idle)
        return cmds
    }

    private fun emptySegmentRound(): List<Cmd> {
        emptyRounds += 1
        return if (emptyRounds >= MAX_EMPTY_ROUNDS) {
            phase = Phase.IDLE
            listOf(Cmd.ClearPreview, Cmd.CancelRecognizer, Cmd.Status(UiStatus.Failed(ErrorKind.NO_SPEECH)))
        } else {
            listOf(Cmd.ClearPreview, Cmd.StartRecognizer)
        }
    }

    private fun commandsFor(result: DictationTransform): List<Cmd> = when (result) {
        is DictationTransform.Text -> listOf(Cmd.CommitSegment(result.value))
        DictationTransform.UndoLastSegment -> listOf(Cmd.ClearPreview, Cmd.UndoLastSegment)
        DictationTransform.DeleteLastSentence -> listOf(Cmd.ClearPreview, Cmd.DeleteLastSentence)
    }

    private fun failStop(kind: ErrorKind): List<Cmd> {
        phase = Phase.IDLE
        return listOf(Cmd.ClearPreview, Cmd.CancelRecognizer, Cmd.Status(UiStatus.Failed(kind)))
    }

    private fun resetModeToOnDevice(): List<Cmd> {
        if (mode == Mode.ON_DEVICE) return emptyList()
        mode = Mode.ON_DEVICE
        return listOf(Cmd.ModeChanged)
    }

    companion object {
        /** Consecutive silent recognizer rounds (~8s each) before dictation auto-stops. */
        const val MAX_EMPTY_ROUNDS = 5

        /** ERROR_RECOGNIZER_BUSY recovery attempts per mic session. */
        const val MAX_BUSY_RESTARTS = 2
    }
}
