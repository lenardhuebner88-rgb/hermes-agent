package net.hermes.deck.model

import org.json.JSONObject

/**
 * The dashboard's `/api/account-usage`, as the app needs it.
 *
 * Every field name here is a contract with `hermes_cli/web_server.py` and is
 * pinned in tests against a response recorded from the live endpoint. The
 * previous round of dashboard payload work found five wrong field names at
 * once, none of which could fail visibly — the list simply stayed empty.
 *
 * Two flags carry the honesty of the number and must never be dropped:
 * [fallback] means the endpoint served a persisted last-good value because the
 * live fetch failed, and [cached] means it came from the in-process cache. A
 * stale percentage rendered as a fresh one is the same class of lie as a green
 * dot for an idle agent.
 */
data class AccountUsage(
    val provider: String,
    val available: Boolean,
    val title: String,
    val plan: String?,
    val windows: List<Window>,
    val details: List<String>,
    val unavailableReason: String?,
    val fallback: Boolean,
    val cached: Boolean,
    val fetchedAt: String?,
) {
    data class Window(
        val label: String,
        val windowKey: String?,
        val usedPercent: Double?,
        val resetAt: String?,
        val detail: String?,
    )

    /** The tightest window, which is what decides the card's colour. */
    val peakPercent: Double? get() = windows.mapNotNull { it.usedPercent }.maxOrNull()

    /** True when the number on screen is not a fresh measurement. */
    val isStale: Boolean get() = fallback || cached

    /**
     * What to call this provider. The endpoint's `title` is a real name for
     * some providers ("Kimi", "Grok") and the generic English placeholder
     * "Account limits" for the rest, which would put the same meaningless
     * heading on three cards.
     */
    val displayName: String
        get() = if (title.isNotBlank() && title != GENERIC_TITLE) title else providerName(provider)

    companion object {
        const val GENERIC_TITLE = "Account limits"

        fun providerName(provider: String): String = when (provider) {
            "anthropic" -> "Claude"
            "openai-codex" -> "Codex"
            "openrouter" -> "OpenRouter"
            "xai" -> "Grok"
            "kimi" -> "Kimi"
            else -> provider
        }

        fun fromJson(json: JSONObject): AccountUsage {
            val windowsJson = json.optJSONArray("windows")
            val detailsJson = json.optJSONArray("details")
            return AccountUsage(
                provider = json.optString("provider"),
                available = json.optBoolean("available", false),
                title = json.optString("title"),
                plan = json.optNullableString("plan"),
                windows = (0 until (windowsJson?.length() ?: 0)).mapNotNull { index ->
                    windowsJson?.optJSONObject(index)?.let { window ->
                        Window(
                            label = window.optString("label"),
                            windowKey = window.optNullableString("window_key"),
                            // `optDouble` returns NaN for null, which would
                            // draw a bar at an unknowable position. A missing
                            // percentage stays missing all the way to the UI.
                            usedPercent = if (window.isNull("used_percent")) {
                                null
                            } else {
                                window.optDouble("used_percent").takeIf { !it.isNaN() }
                            },
                            resetAt = window.optNullableString("reset_at"),
                            detail = window.optNullableString("detail"),
                        )
                    }
                },
                details = (0 until (detailsJson?.length() ?: 0)).mapNotNull {
                    detailsJson?.optString(it)?.ifBlank { null }
                },
                unavailableReason = json.optNullableString("unavailable_reason"),
                fallback = json.optBoolean("fallback", false),
                // Absent in the endpoint's "unavailable" shape, so a missing
                // key means "not from cache", not an error.
                cached = json.optBoolean("cached", false),
                fetchedAt = json.optNullableString("fetched_at"),
            )
        }

        /** `{"providers":[…],"cache_ttl_seconds":60}` */
        fun listFromJson(json: JSONObject): List<AccountUsage> {
            val array = json.optJSONArray("providers") ?: return emptyList()
            return (0 until array.length()).mapNotNull { index ->
                array.optJSONObject(index)?.let(::fromJson)
            }
        }

        private fun JSONObject.optNullableString(key: String): String? =
            if (isNull(key)) null else optString(key).ifBlank { null }
    }
}
