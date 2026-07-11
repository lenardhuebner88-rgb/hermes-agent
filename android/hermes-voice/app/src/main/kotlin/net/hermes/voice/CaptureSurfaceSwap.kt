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
