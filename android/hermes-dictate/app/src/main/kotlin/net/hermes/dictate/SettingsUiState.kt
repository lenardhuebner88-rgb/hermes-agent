package net.hermes.dictate

/**
 * The four one-time setup checks the old screen rendered as four separate rows. Kept as a plain
 * enum (no Android types) so the readiness card's collapsing behaviour is a pure function and
 * testable off-device.
 */
enum class ReadinessStepId { MIC, ENABLE, SELECT, OVERLAY }

data class ReadinessStep(val id: ReadinessStepId, val done: Boolean)

data class ReadinessState(val steps: List<ReadinessStep>) {
    val doneCount: Int get() = steps.count { it.done }
    val total: Int get() = steps.size
    val allDone: Boolean get() = total > 0 && doneCount == total
}

/**
 * The cloud-login row's state machine. `true`/`false`/`null` mirror [SessionProbe.check]'s
 * return contract (signed in / signed out / server unreachable); [Checking] is the local
 * "probe in flight" state before the first result lands.
 *
 * This is the fix for defect 2 from the audit: exactly one of these ever renders a *primary*
 * "Anmelden" action — [SignedIn] never does, everything else does (never both at once).
 */
sealed interface LoginStatus {
    data object Checking : LoginStatus
    data object SignedIn : LoginStatus
    data object SignedOut : LoginStatus
    data object Unreachable : LoginStatus

    companion object {
        fun fromProbe(signedIn: Boolean?): LoginStatus = when (signedIn) {
            true -> SignedIn
            false -> SignedOut
            null -> Unreachable
        }
    }
}

/** The one row-rendering rule the whole login card exists to enforce. */
fun LoginStatus.showsPrimaryLoginButton(): Boolean = this != LoginStatus.SignedIn
