package net.hermes.deck.net

/**
 * Keeps the app off `/auth/password-login` once the dashboard has said "enough".
 *
 * The dashboard throttles password logins per client IP (ten in sixty seconds)
 * and answers the eleventh with HTTP 429. Without a client-side memory of that
 * answer the poll loop simply tries again eight seconds later, which keeps the
 * server's sliding window permanently full: the phone locks *itself* out and
 * the screen shows an authentication error for as long as it is open.
 *
 * So a 429 starts a cooldown during which no login leaves the phone at all.
 * That is deliberately longer than the server's own window — a retry that lands
 * exactly at the window edge, with the poll loop's other requests behind it,
 * would refill the bucket in one go.
 */
class LoginThrottle(
    private val cooldownMillis: Long = COOLDOWN_MILLIS,
    private val now: () -> Long = System::currentTimeMillis,
) {

    @Volatile
    private var blockedUntil: Long = 0L

    /** Milliseconds a login still has to wait; zero when it may go out. */
    fun waitMillis(): Long = (blockedUntil - now()).coerceAtLeast(0L)

    /** The server answered 429 — hold everything back for the cooldown. */
    fun rateLimited() {
        blockedUntil = now() + cooldownMillis
    }

    /** A login went through; the window is demonstrably open again. */
    fun succeeded() {
        blockedUntil = 0L
    }

    private companion object {
        /** Server window is 60 s; half a window of slack on top. */
        const val COOLDOWN_MILLIS = 90_000L
    }
}
