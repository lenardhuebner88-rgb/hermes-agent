package net.hermes.deck.net

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class LoginThrottleTest {

    private var clock = 1_000_000L
    private fun throttle(cooldown: Long = 90_000L) = LoginThrottle(cooldown) { clock }

    @Test
    fun `nothing is held back until the server says 429`() {
        assertEquals(0L, throttle().waitMillis())
    }

    @Test
    fun `a 429 holds every further login back for the whole cooldown`() {
        val t = throttle()
        t.rateLimited()
        assertEquals(90_000L, t.waitMillis())
        clock += 89_000L
        assertTrue("noch gesperrt", t.waitMillis() > 0L)
        clock += 2_000L
        assertEquals("Fenster vorbei", 0L, t.waitMillis())
    }

    @Test
    fun `the cooldown outlasts the servers own sixty second window`() {
        // Retrying at the window edge refills the bucket in one poll cycle.
        val t = throttle()
        t.rateLimited()
        clock += 60_000L
        assertTrue(t.waitMillis() > 0L)
    }

    @Test
    fun `a successful login reopens the gate at once`() {
        val t = throttle()
        t.rateLimited()
        t.succeeded()
        assertEquals(0L, t.waitMillis())
    }
}
