package net.hermes.deck.model

import org.json.JSONObject

/**
 * What the fleet is actually burning, per hour.
 *
 * This is the one *rate* on the screen, and it exists because the band used to
 * say "Budget unbekannt" — a sentence with no next move in it. It is measured,
 * not modelled: the server counts real LLM calls in a real time window out of
 * the observability database.
 *
 * Two things it refuses to do, both of which were measured mistakes:
 *
 *  * **Cache reads are not in the number.** The same hour reads as 43.9 M
 *    tokens with them and 0.68 M without. Both are true; only one of them is
 *    what "the fleet is burning" means, and quoting the larger one in the same
 *    units would be a sixty-fold overstatement.
 *  * **A rate needs a window.** Fewer than two calls, or a covered span under
 *    five minutes, yields no rate at all — [reason] says why instead. One call
 *    extrapolated to an hour is a number with the shape of a measurement.
 *
 * There is deliberately no per-agent breakdown: of 280 calls measured in one
 * hour, **zero** carried a lane that could attribute them to an agent
 * ([attributedShare]). A bar chart per agent would have been invented.
 */
data class BurnRate(
    /** Billable tokens per hour: input + output + cache writes. Null when unmeasurable. */
    val tokensPerHour: Double?,
    /** Why there is no rate — shown instead of the number, never beside a zero. */
    val reason: String?,
    val calls: Int,
    val billableTokens: Long,
    val cacheReadTokens: Long,
    /** The span actually covered by the measurement, which is not the requested window. */
    val coveredSeconds: Int,
    /** Share of calls that could be attributed to a lane at all — 0.0 in practice. */
    val attributedShare: Double,
) {
    /** "0,83 M/h" — or null, so the caller must say something else. */
    val perHourLabel: String?
        get() {
            val rate = tokensPerHour ?: return null
            return when {
                rate >= 1_000_000 -> "${oneDecimal(rate / 1_000_000)} M/h"
                rate >= 1_000 -> "${oneDecimal(rate / 1_000)} k/h"
                else -> "${rate.toInt()}/h"
            }
        }

    /** True once the measurement is thin enough that the UI should say so. */
    val isNarrow: Boolean get() = coveredSeconds < 900

    private fun oneDecimal(value: Double): String {
        val tenths = (value * 10).toLong()
        return "${tenths / 10},${tenths % 10}"
    }

    companion object {
        fun fromJson(json: JSONObject?): BurnRate? {
            if (json == null) return null
            return BurnRate(
                tokensPerHour = if (json.isNull("rate_billable_tokens_per_hour")) null
                else json.optDouble("rate_billable_tokens_per_hour").takeIf { !it.isNaN() },
                reason = if (json.isNull("rate_reason")) null
                else json.optString("rate_reason").ifBlank { null },
                calls = json.optInt("calls"),
                billableTokens = json.optLong("billable_tokens"),
                cacheReadTokens = json.optLong("cache_read_tokens"),
                coveredSeconds = json.optDouble("covered_seconds", 0.0)
                    .takeIf { !it.isNaN() }?.toInt() ?: 0,
                attributedShare = json.optJSONObject("lane_attribution")
                    ?.optDouble("attributed_share", 0.0)
                    ?.takeIf { !it.isNaN() } ?: 0.0,
            )
        }
    }
}
