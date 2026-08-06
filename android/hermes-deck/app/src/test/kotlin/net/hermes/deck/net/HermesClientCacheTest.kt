package net.hermes.deck.net

import org.junit.Assert.assertNotSame
import org.junit.Assert.assertSame
import org.junit.Test

/**
 * The bug this class exists for was invisible in the UI and loud in the server
 * log: a client built per call carries no session, so every request logs in
 * first. On 2026-08-06 that produced 24 logins in six minutes from the phone
 * and then HTTP 429 from the dashboard's brute-force throttle.
 */
class HermesClientCacheTest {

    private var built = 0

    private fun cache() = HermesClientCache { url, user, pw ->
        built += 1
        HermesClient(url, user, pw)
    }

    @Test
    fun `the same credentials keep the same client, so the session survives`() {
        val cache = cache()
        val first = cache.get("https://dash", "piet", "geheim")
        repeat(20) { assertSame(first, cache.get("https://dash", "piet", "geheim")) }
        // Twenty polls, one client — the point of the whole class.
        org.junit.Assert.assertEquals(1, built)
    }

    @Test
    fun `changed credentials must not keep the old session`() {
        val cache = cache()
        val first = cache.get("https://dash", "piet", "geheim")
        assertNotSame(first, cache.get("https://dash", "piet", "neu"))
        assertNotSame(first, cache.get("https://andere", "piet", "geheim"))
        assertNotSame(first, cache.get("https://dash", "wer-anders", "geheim"))
        org.junit.Assert.assertEquals(4, built)
    }
}
