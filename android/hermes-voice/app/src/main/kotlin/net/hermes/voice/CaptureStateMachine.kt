package net.hermes.voice

/**
 * Legal-transition tracking for the screen-capture lifecycle. Pure state logic — no
 * android.* imports, no side effects — so the actual cleanup (stopping the poller, closing
 * the projection, removing the notification, ...) lives in the caller and is triggered only
 * when a transition here reports success.
 *
 * IDLE -> REQUESTING -> STARTING -> CAPTURING -> STOPPING -> IDLE
 */
enum class CaptureState {
    IDLE,
    REQUESTING,
    STARTING,
    CAPTURING,
    STOPPING,
}

class CaptureStateMachine(initial: CaptureState = CaptureState.IDLE) {

    var state: CaptureState = initial
        private set

    /**
     * Starts a new capture request. Only legal from IDLE. Returns false (rejected — caller
     * should reply screen_capture_error "busy") if a capture is already in flight.
     */
    fun start(): Boolean {
        if (state != CaptureState.IDLE) return false
        state = CaptureState.REQUESTING
        return true
    }

    /** REQUESTING -> STARTING, once the capture-intent result (RESULT_OK) is in hand. */
    fun advanceToStarting(): Boolean {
        if (state != CaptureState.REQUESTING) return false
        state = CaptureState.STARTING
        return true
    }

    /** STARTING -> CAPTURING, once the foreground service + virtual display are live. */
    fun advanceToCapturing(): Boolean {
        if (state != CaptureState.STARTING) return false
        state = CaptureState.CAPTURING
        return true
    }

    /**
     * Requests a stop. Idempotent: returns true exactly once per capture session (the first
     * call that actually moves into STOPPING — the caller should run cleanup then), and false
     * for every subsequent call (already stopping) or when there was nothing running (IDLE).
     * Never throws, regardless of current state.
     */
    fun stop(): Boolean {
        return when (state) {
            CaptureState.IDLE, CaptureState.STOPPING -> false
            CaptureState.REQUESTING, CaptureState.STARTING, CaptureState.CAPTURING -> {
                state = CaptureState.STOPPING
                true
            }
        }
    }

    /**
     * Marks cleanup as complete and returns to IDLE, allowing a new capture to start. Safe to
     * call from any state (including IDLE already) and safe to call more than once.
     */
    fun finishStop() {
        state = CaptureState.IDLE
    }
}
