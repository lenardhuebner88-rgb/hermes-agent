package net.hermes.voice

/** Pure transaction policy for swapping MediaProjection surfaces without leaking candidates. */
enum class CaptureSurfaceSwapOutcome { COMMITTED, ROLLED_BACK, FATAL }

object CaptureSurfaceSwap {
    fun execute(
        install: () -> Unit,
        rollback: () -> Unit,
        discardCandidate: () -> Unit,
        canCommit: () -> Boolean = { true },
    ): CaptureSurfaceSwapOutcome {
        val installed = try {
            install()
            true
        } catch (_: Exception) {
            false
        }
        val commitAllowed = installed && try {
            canCommit()
        } catch (_: Exception) {
            false
        }
        if (commitAllowed) return CaptureSurfaceSwapOutcome.COMMITTED

        val candidateDiscarded = try {
            discardCandidate()
            true
        } catch (_: Exception) {
            false
        }
        return try {
            rollback()
            if (candidateDiscarded) {
                CaptureSurfaceSwapOutcome.ROLLED_BACK
            } else {
                CaptureSurfaceSwapOutcome.FATAL
            }
        } catch (_: Exception) {
            CaptureSurfaceSwapOutcome.FATAL
        }
    }
}

/** Pure ownership rule used to keep MediaProjection teardown on its capture looper. */
object CaptureThreadOwnership {
    fun shouldDispatchStop(hasCaptureHandler: Boolean, onCaptureThread: Boolean): Boolean =
        hasCaptureHandler && !onCaptureThread
}

/** Final privacy gate for a captured frame, evaluated while stop ordering is locked. */
object CaptureDeliveryPolicy {
    fun shouldDeliver(hasEncodedFrame: Boolean, stopRequested: Boolean): Boolean =
        hasEncodedFrame && !stopRequested

    fun shouldNotifyUnavailable(stopRequested: Boolean): Boolean = !stopRequested
}
