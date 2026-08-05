package net.hermes.deck.model

import org.json.JSONObject

/**
 * What one agent's current session is made of: how full, how old, how often compacted.
 *
 * Six different agent CLIs keep six different books, and the server reads all of
 * them. That is invisible here except in one place: [source]. It exists so the
 * UI can tell "this agent keeps no session book" apart from "the book could not
 * be read" — the same distinction [AgentPulse] makes for the journal, and for
 * the same reason. A row that silently shows nothing teaches the operator to
 * distrust the whole screen.
 *
 * Every number is nullable all the way to the card. There is no default context
 * window: an unknown model yields no percentage rather than a plausible one.
 */
data class SessionFacts(
    val sessionId: String?,
    /** Seconds since the session's first turn — server-measured, not phone-clocked. */
    val ageSeconds: Int?,
    /** Prompt tokens of the last turn: what the model actually carried. */
    val promptTokens: Int?,
    val contextWindow: Int?,
    val compacts: Int?,
    val lastCompaction: Compaction?,
    /** Age of the measurement itself — a value between turns does not move. */
    val measuredSecondsAgo: Int?,
    val source: Source,
    /** Null when the journal never said; never guessed as false. */
    val steerable: Boolean?,
) {
    enum class Source {
        CLAUDE, CODEX, QWEN, KIMI, GROK,

        /** This agent keeps no session book. A state, not a failure. */
        NONE,

        /** A book exists and could not be read. A failure, and shown as one. */
        ERROR,
        ;

        companion object {
            fun fromWire(value: String): Source = when (value.trim().lowercase()) {
                "claude" -> CLAUDE
                "codex" -> CODEX
                "qwen" -> QWEN
                "kimi" -> KIMI
                "grok" -> GROK
                "error" -> ERROR
                else -> NONE
            }
        }
    }

    data class Compaction(val preTokens: Int?, val postTokens: Int?, val secondsAgo: Int?)

    /**
     * Fill level as a fraction, or null.
     *
     * Null when either number is missing — a bar drawn against a guessed window
     * is the failure this whole feature exists to avoid.
     */
    val fraction: Double?
        get() {
            val used = promptTokens ?: return null
            val window = contextWindow?.takeIf { it > 0 } ?: return null
            return (used.toDouble() / window).coerceIn(0.0, 1.0)
        }

    val percent: Int? get() = fraction?.let { (it * 100).toInt() }

    /** True once the context is close enough that the next long turn may hit it. */
    val isTight: Boolean get() = (percent ?: 0) >= 80

    val isCritical: Boolean get() = (percent ?: 0) >= 92

    /** Whether the bar should be drawn at all. */
    val hasMeasurement: Boolean get() = promptTokens != null

    /**
     * The card's one line when there is nothing to measure. Distinguishing the
     * two silences is the whole point of [source].
     */
    val absenceReason: String?
        get() = when {
            hasMeasurement -> null
            source == Source.ERROR -> "Session nicht lesbar"
            source == Source.NONE -> "führt kein Sitzungsbuch"
            else -> "noch keine Messung"
        }

    companion object {
        fun fromJson(json: JSONObject?): SessionFacts? {
            if (json == null) return null
            val compaction = json.optJSONObject("last_compaction")?.let {
                Compaction(
                    preTokens = it.optIntOrNull("pre"),
                    postTokens = it.optIntOrNull("post"),
                    secondsAgo = it.optIntOrNull("seconds_ago"),
                )
            }
            return SessionFacts(
                sessionId = json.optString("session_id").ifBlank { null },
                ageSeconds = json.optIntOrNull("age_seconds"),
                promptTokens = json.optIntOrNull("prompt_tokens"),
                contextWindow = json.optIntOrNull("context_window"),
                compacts = json.optIntOrNull("compacts"),
                lastCompaction = compaction,
                measuredSecondsAgo = json.optIntOrNull("measured_seconds_ago"),
                source = Source.fromWire(json.optString("source")),
                steerable = if (!json.has("steerable") || json.isNull("steerable")) null
                else json.optBoolean("steerable"),
            )
        }

        private fun JSONObject.optIntOrNull(key: String): Int? =
            if (!has(key) || isNull(key)) null else optInt(key)
    }
}
