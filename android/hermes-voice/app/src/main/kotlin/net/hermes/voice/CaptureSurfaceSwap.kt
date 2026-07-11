package net.hermes.voice

/** Pure transaction policy for swapping MediaProjection surfaces without leaking candidates. */
enum class CaptureSurfaceSwapOutcome { COMMITTED, ROLLED_BACK, FATAL }

object CaptureSurfaceSwap {
    fun execute(
        install: () -> Unit,
        rollback: () -> Unit,
        discardCandidate: () -> Unit,
    ): CaptureSurfaceSwapOutcome = try {
        install()
        CaptureSurfaceSwapOutcome.COMMITTED
    } catch (_: Exception) {
        val candidateDiscarded = try {
            discardCandidate()
            true
        } catch (_: Exception) {
            false
        }
        try {
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
