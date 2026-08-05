package net.hermes.deck.model

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Pinned against a real `/api/account-usage` response recorded on 2026-08-05.
 *
 * The three providers kept from it are the three shapes that behave
 * differently: anthropic is fresh with two windows and a generic title, kimi is
 * a cached fallback with per-window detail text, and openrouter has no windows
 * at all — only prose. Guessing any of these from the endpoint's source would
 * have missed the fallback flags, which is the field that decides whether a
 * number may be shown as current.
 */
class AccountUsageTest {

    private val recorded = """
    {
      "providers": [
        {
          "provider": "anthropic", "available": true, "source": "oauth_usage_api",
          "fetched_at": "2026-08-05T08:52:29.752303+00:00", "signal_at": null,
          "title": "Account limits", "plan": "Max 20×",
          "windows": [
            {"label":"Current session","window_key":"session","used_percent":8.0,
             "reset_at":"2026-08-05T12:49:59.658844+00:00","detail":null},
            {"label":"Current week","window_key":"weekly","used_percent":63.0,
             "reset_at":"2026-08-07T03:59:59.658868+00:00","detail":null}
          ],
          "details": [], "unavailable_reason": null, "fallback": false, "cached": false
        },
        {
          "provider": "kimi", "available": true, "source": "usage_api",
          "fetched_at": "2026-08-05T08:24:43.170887+00:00", "signal_at": null,
          "title": "Kimi", "plan": "Standard · Purchase",
          "windows": [
            {"label":"Diese Woche","window_key":"weekly","used_percent":74.0,
             "reset_at":"2026-08-06T18:13:15.693301+00:00","detail":"26/100 verbleibend"},
            {"label":"5-Std-Fenster","window_key":"session","used_percent":2.0,
             "reset_at":"2026-08-05T11:13:15.693301+00:00","detail":"98/100 verbleibend"}
          ],
          "details": ["Parallel: 30"], "unavailable_reason": null,
          "fallback": true, "cached": true
        },
        {
          "provider": "openrouter", "available": true, "source": "credits_api",
          "fetched_at": "2026-08-05T08:52:30.652590+00:00", "signal_at": null,
          "title": "Account limits", "plan": null, "windows": [],
          "details": ["Credits balance: ${'$'}0.00", "API key usage: ${'$'}173.38 total"],
          "unavailable_reason": null, "fallback": false, "cached": false
        }
      ],
      "cache_ttl_seconds": 60
    }
    """.trimIndent()

    private fun all() = AccountUsage.listFromJson(JSONObject(recorded))

    private fun byName(provider: String) = all().first { it.provider == provider }

    @Test
    fun `every provider in the recorded response parses`() {
        assertEquals(listOf("anthropic", "kimi", "openrouter"), all().map { it.provider })
    }

    @Test
    fun `windows carry label and percent`() {
        val windows = byName("anthropic").windows

        assertEquals(listOf("Current session", "Current week"), windows.map { it.label })
        assertEquals(listOf(8.0, 63.0), windows.map { it.usedPercent })
        assertEquals(listOf("session", "weekly"), windows.map { it.windowKey })
    }

    @Test
    fun `the generic english title is replaced by the provider's real name`() {
        // Three of five providers report "Account limits", which would put the
        // same meaningless heading on three cards.
        assertEquals("Account limits", byName("anthropic").title)
        assertEquals("Claude", byName("anthropic").displayName)
        assertEquals("OpenRouter", byName("openrouter").displayName)
    }

    @Test
    fun `a real title is kept as it is`() {
        assertEquals("Kimi", byName("kimi").displayName)
    }

    @Test
    fun `a cached fallback is marked stale so it is not shown as a fresh reading`() {
        val kimi = byName("kimi")

        assertTrue(kimi.fallback)
        assertTrue(kimi.cached)
        assertTrue(kimi.isStale)
        // The fresh one must not be tarred with the same brush.
        assertFalse(byName("anthropic").isStale)
    }

    @Test
    fun `peak percent picks the tightest window, which decides the colour`() {
        assertEquals(63.0, byName("anthropic").peakPercent!!, 0.001)
        assertEquals(74.0, byName("kimi").peakPercent!!, 0.001)
    }

    @Test
    fun `a provider without windows has no peak instead of a zero`() {
        val openrouter = byName("openrouter")

        assertTrue(openrouter.windows.isEmpty())
        // Zero would draw an empty bar reading "nothing used yet".
        assertNull(openrouter.peakPercent)
        assertEquals(2, openrouter.details.size)
    }

    @Test
    fun `per-window detail text survives`() {
        assertEquals("26/100 verbleibend", byName("kimi").windows[0].detail)
        assertNull(byName("anthropic").windows[0].detail)
    }

    @Test
    fun `a null percent stays null rather than becoming NaN or zero`() {
        // `optDouble` yields NaN for JSON null, which would place a bar at an
        // unknowable position instead of admitting the value is missing.
        val json = JSONObject(
            """{"provider":"x","available":true,"title":"X","windows":[
               {"label":"W","window_key":null,"used_percent":null,"reset_at":null,"detail":null}],
               "details":[],"unavailable_reason":null,"fallback":false}"""
        )

        val usage = AccountUsage.fromJson(json)

        assertNull(usage.windows[0].usedPercent)
        assertNull(usage.peakPercent)
        assertNull(usage.windows[0].windowKey)
    }

    @Test
    fun `an unavailable provider keeps its reason and claims no budget`() {
        // This is the endpoint's `_account_usage_unavailable` shape verbatim —
        // note it has no "cached" key at all.
        val json = JSONObject(
            """{"provider":"anthropic","available":false,"source":null,
               "fetched_at":"2026-08-05T08:00:00+00:00","signal_at":null,
               "title":"Account limits","plan":null,"windows":[],"details":[],
               "unavailable_reason":"usage_unavailable","fallback":false}"""
        )

        val usage = AccountUsage.fromJson(json)

        assertFalse(usage.available)
        assertEquals("usage_unavailable", usage.unavailableReason)
        assertNull(usage.peakPercent)
        // Missing key means "not from cache", not an error.
        assertFalse(usage.cached)
        assertFalse(usage.isStale)
    }

    @Test
    fun `an empty or malformed payload yields no providers instead of throwing`() {
        assertEquals(emptyList<AccountUsage>(), AccountUsage.listFromJson(JSONObject("{}")))
        assertEquals(
            emptyList<AccountUsage>(),
            AccountUsage.listFromJson(JSONObject("""{"providers":[]}""")),
        )
    }

    @Test
    fun `plan is null when the endpoint reports none`() {
        assertEquals("Max 20×", byName("anthropic").plan)
        assertNull(byName("openrouter").plan)
    }
}
