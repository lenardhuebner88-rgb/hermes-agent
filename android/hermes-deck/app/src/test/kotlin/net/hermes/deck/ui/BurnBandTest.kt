package net.hermes.deck.ui

import net.hermes.deck.model.AccountUsage
import net.hermes.deck.model.BurnRate
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * The band at the top of the Leitstand.
 *
 * Its old text was "Budget unbekannt" for every case that was not a clean
 * percentage, which is the failure this file exists to prevent: a sentence that
 * is technically true, carries no next move, and hides which of four very
 * different situations the operator is actually in.
 */
class BurnBandTest {

    private fun usage(
        provider: String,
        percent: Double?,
        available: Boolean = true,
        reason: String? = null,
        cached: Boolean = false,
    ) = AccountUsage(
        provider = provider,
        available = available,
        title = AccountUsage.GENERIC_TITLE,
        plan = null,
        windows = percent?.let {
            listOf(AccountUsage.Window("Weekly", "weekly", it, null, null))
        }.orEmpty(),
        details = emptyList(),
        unavailableReason = reason,
        fallback = false,
        cached = cached,
        fetchedAt = null,
    )

    @Test
    fun `nothing fetched yet says so and offers the tap`() {
        val line = BudgetLine.of(emptyList())
        assertNull(line.percent)
        assertTrue(line.headline.contains("noch nicht abgerufen"))
        assertEquals("antippen", line.trailer)
    }

    @Test
    fun `providers that answered without a number surface the reason`() {
        val line = BudgetLine.of(listOf(usage("kimi", null, available = false, reason = "Kimi API key not configured (KIMI_API_KEY).")))
        assertNull(line.percent)
        // The reason is the useful part; "unbekannt" would have thrown it away.
        assertTrue(line.headline.contains("KIMI_API_KEY"))
    }

    @Test
    fun `the tightest provider leads and the runner-up trails`() {
        // The real measured situation: codex weekly 87, anthropic weekly 83.
        val line = BudgetLine.of(
            listOf(usage("anthropic", 83.0), usage("openai-codex", 87.0), usage("xai", 52.0)),
        )
        assertEquals(87.0, line.percent!!, 0.001)
        assertTrue("tightest must lead", line.headline.startsWith("Codex 87"))
        // One provider at 87 % is a lane switch; two at 87 % is not — so the
        // runner-up is part of the decision, not decoration.
        assertTrue("runner-up must be named", line.trailer.startsWith("Claude 83"))
    }

    @Test
    fun `a cached percentage is never rendered as a fresh one`() {
        val line = BudgetLine.of(listOf(usage("anthropic", 83.0, cached = true)))
        assertTrue(line.headline.contains("Zwischenspeicher"))
    }

    // --- the rate --------------------------------------------------------

    private fun burn(json: String) = BurnRate.fromJson(JSONObject(json))

    @Test
    fun `a measured rate is formatted per hour`() {
        val rate = burn(
            """{"rate_billable_tokens_per_hour": 850992.01, "rate_reason": null,
                "calls": 261, "billable_tokens": 675593, "cache_read_tokens": 43917269,
                "covered_seconds": 2945.3, "lane_attribution": {"attributed_share": 0.0}}""",
        )
        assertNotNull(rate)
        assertEquals("850,9 k/h", rate!!.perHourLabel)
        // Cache reads are carried, but never inside the rate: the same hour
        // reads as 43.9 M with them and 0.68 M without.
        assertEquals(43_917_269L, rate.cacheReadTokens)
        assertEquals(675_593L, rate.billableTokens)
    }

    @Test
    fun `no rate yields no number and keeps the reason`() {
        val rate = burn(
            """{"rate_billable_tokens_per_hour": null, "rate_reason": "zu wenig Messpunkte",
                "calls": 1, "billable_tokens": 12, "cache_read_tokens": 0,
                "covered_seconds": 4.0, "lane_attribution": {"attributed_share": 0.0}}""",
        )
        // A single call extrapolated to an hour is a number shaped like a
        // measurement, so there must be none at all — and never a 0.
        assertNull(rate!!.perHourLabel)
        assertNull(rate.tokensPerHour)
        assertEquals("zu wenig Messpunkte", rate.reason)
    }

    @Test
    fun `lane attribution is reported as unusable rather than hidden`() {
        // Measured: 0 of 280 calls carried a lane. The UI must be able to see
        // that, or it would draw a token bar per agent out of nothing.
        val rate = burn(
            """{"rate_billable_tokens_per_hour": 1.0, "rate_reason": null, "calls": 280,
                "billable_tokens": 1, "cache_read_tokens": 0, "covered_seconds": 3600.0,
                "lane_attribution": {"attributed_share": 0.0}}""",
        )
        assertEquals(0.0, rate!!.attributedShare, 0.0001)
    }

    @Test
    fun `millions read as M per hour`() {
        val rate = burn(
            """{"rate_billable_tokens_per_hour": 14200000.0, "rate_reason": null, "calls": 90,
                "billable_tokens": 1, "cache_read_tokens": 0, "covered_seconds": 3600.0,
                "lane_attribution": {"attributed_share": 0.0}}""",
        )
        assertEquals("14,2 M/h", rate!!.perHourLabel)
    }
}
