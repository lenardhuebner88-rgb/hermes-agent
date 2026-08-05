package net.hermes.deck.model

import org.json.JSONArray
import org.json.JSONObject

/**
 * One tool invocation, as the server read it out of the agent's journal.
 *
 * The age travels as a number of seconds rather than a timestamp, for the same
 * reason [BuzzAgent] does it: the phone's clock and the server's clock disagree
 * often enough that subtracting them produces a confident, wrong "vor 3 Stunden".
 */
data class ToolCall(
    val secondsAgo: Int,
    val label: String,
    val kind: Kind,
) {
    /**
     * The tool families the ACP log distinguishes. [UNKNOWN] is not a defect: a
     * multi-line shell command logs only its first physical line, which carries
     * no `(kind)` suffix — and that line is still the most informative thing
     * about the call, so it is kept rather than dropped.
     */
    enum class Kind(val label: String) {
        EXECUTE("Terminal"),
        EDIT("Ändern"),
        READ("Lesen"),
        SEARCH("Suchen"),
        THINK("Denken"),
        FETCH("Holen"),
        OTHER("Sonstiges"),
        UNKNOWN("Unbekannt"),
        ;

        companion object {
            fun fromWire(value: String): Kind = when (value.trim().lowercase()) {
                "execute" -> EXECUTE
                "edit" -> EDIT
                "read" -> READ
                "search", "grep" -> SEARCH
                "think" -> THINK
                "fetch" -> FETCH
                "other" -> OTHER
                else -> UNKNOWN
            }
        }
    }

    companion object {
        fun fromJson(json: JSONObject): ToolCall = ToolCall(
            secondsAgo = json.optInt("seconds_ago", 0),
            label = json.optString("label").ifBlank { "—" },
            kind = Kind.fromWire(json.optString("kind")),
        )
    }
}

/**
 * What one agent has been doing inside the server's activity window.
 *
 * [looksOpen] is a deliberately weak claim and the UI must keep it weak. The
 * journal puts no call id on the line that starts a tool call and no label on
 * the line that ends one, so the two streams cannot be joined; calls overlap in
 * practice. All the server can say is "nothing has reported finishing since
 * this call started". So a card may show *läuft*, but it may never show a
 * duration as if the call were proven to still be running — the age shown is
 * always the age of the call itself, which is a fact.
 */
data class AgentPulse(
    val stem: String,
    val callsInWindow: Int,
    val latest: ToolCall?,
    val looksOpen: Boolean,
    val lastSignalSecondsAgo: Int?,
    val recent: List<ToolCall>,
) {
    /** "läuft" or "zuletzt" — never a claim the journal cannot support. */
    val statusWord: String get() = if (looksOpen) "läuft" else "zuletzt"

    val hasActivity: Boolean get() = latest != null

    companion object {
        fun fromJson(stem: String, json: JSONObject?): AgentPulse? {
            // Null is the server saying "the journal could not be read". That is
            // not the same as an agent that made no calls, and the two must not
            // collapse into one grey row.
            if (json == null) return null
            val recent = json.optJSONArray("recent") ?: JSONArray()
            return AgentPulse(
                stem = stem,
                callsInWindow = json.optInt("calls_in_window", 0),
                latest = json.optJSONObject("latest")?.let(ToolCall::fromJson),
                looksOpen = json.optBoolean("looks_open", false),
                lastSignalSecondsAgo =
                    if (json.isNull("last_signal_seconds_ago")) null
                    else json.optInt("last_signal_seconds_ago"),
                recent = (0 until recent.length()).mapNotNull {
                    recent.optJSONObject(it)?.let(ToolCall::fromJson)
                },
            )
        }
    }
}
