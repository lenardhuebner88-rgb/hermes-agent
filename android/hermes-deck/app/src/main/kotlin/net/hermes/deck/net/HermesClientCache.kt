package net.hermes.deck.net

/**
 * One [HermesClient] per credential set, kept for as long as the credentials
 * stand.
 *
 * The session cookie lives *inside* the client. Building a fresh client per
 * call therefore threw the session away every time: every single request met
 * the auth gate unauthenticated, logged in, and used the cookie once. With the
 * pulse loop polling every eight seconds that is seven or eight logins a
 * minute, and the dashboard's brute-force throttle (ten per minute per IP)
 * answered the rest with HTTP 429 — measured in `~/.hermes/logs/dashboard-auth.log`
 * on 2026-08-06: 24 `login_success` in six minutes from the phone, then
 * `login_failure reason=rate_limited`.
 *
 * Changing credentials must not keep the old session, so the cache is keyed on
 * them rather than merely holding one instance forever.
 */
class HermesClientCache(
    private val make: (String, String, String) -> HermesClient = ::HermesClient,
) {

    private data class Key(val baseUrl: String, val username: String, val password: String)

    private var key: Key? = null
    private var client: HermesClient? = null

    @Synchronized
    fun get(baseUrl: String, username: String, password: String): HermesClient {
        val wanted = Key(baseUrl, username, password)
        client?.takeIf { key == wanted }?.let { return it }
        return make(baseUrl, username, password).also {
            client = it
            key = wanted
        }
    }
}
