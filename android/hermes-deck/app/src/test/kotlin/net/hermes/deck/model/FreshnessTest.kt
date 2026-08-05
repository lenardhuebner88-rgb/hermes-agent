package net.hermes.deck.model

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Freshness is the app's admission that a screen is a measurement, not a fact.
 * The old behaviour — load once on screen entry, then look current forever — is
 * the failure these tests exist to make impossible.
 */
class FreshnessTest {

    private fun at(ageSeconds: Int) =
        Freshness.of(receivedAtMillis = 1_000_000L, nowMillis = 1_000_000L + ageSeconds * 1000L)

    @Test
    fun `a fresh payload is simply current`() {
        assertEquals(Freshness.State.FRESH, at(3).state)
        assertEquals("gerade eben", at(3).label)
        assertFalse(at(3).isStale)
    }

    @Test
    fun `a missed tick is named, not hidden`() {
        assertEquals(Freshness.State.AGING, at(30).state)
        assertEquals("vor 30 s", at(30).label)
    }

    @Test
    fun `past the stale threshold the screen stops being evidence`() {
        assertEquals(Freshness.State.STALE, at(120).state)
        assertTrue(at(120).isStale)
        assertEquals("Stand vor 2 min", at(120).headerLabel)
    }

    @Test
    fun `nothing ever received counts as stale, never as calm`() {
        val never = Freshness.of(receivedAtMillis = 0L, nowMillis = 5_000_000L)
        assertTrue(never.isStale)
        assertEquals(0, never.ageSeconds)
    }

    @Test
    fun `a clock that jumps backwards does not produce a negative age`() {
        val skewed = Freshness.of(receivedAtMillis = 2_000_000L, nowMillis = 1_000_000L)
        assertEquals(0, skewed.ageSeconds)
        assertEquals(Freshness.State.FRESH, skewed.state)
    }

    @Test
    fun `labels never show an absolute timestamp`() {
        assertEquals("vor 4 min", at(4 * 60).label)
        assertEquals("vor 3 h", at(3 * 3600).label)
        assertEquals("vor 2 d", at(2 * 86_400).label)
    }
}
