package net.hermes.deck.model

import org.json.JSONObject

/**
 * One Buzz agent as the Hermes dashboard reports it.
 *
 * The model is whatever `BUZZ_ACP_MODEL` says in the agent's env file; it only
 * takes effect after the unit restarts, which is why [activeState] and
 * [lastStart] are shown next to it — a changed value with a stale start time
 * means the agent is still answering with the old model.
 *
 * [activeState] alone is not evidence of work. Measured on 2026-08-05 it read
 * `active` for all eight agents while four had made no tool call for hours, so
 * the heartbeat fields below carry the part that can actually be believed.
 */
data class BuzzAgent(
    val stem: String,
    val displayName: String,
    val model: String,
    val agentCommand: String,
    val activeState: String,
    val lastStart: String,
    val toolCallsWindow: Int?,
    val toolCallsRecent: Int?,
    val lastToolCallSecondsAgo: Int?,
    val working: Boolean?,
) {
    val isRunning: Boolean get() = activeState == "active"
    val isFailed: Boolean get() = activeState == "failed"

    companion object {
        fun fromJson(json: JSONObject): BuzzAgent = BuzzAgent(
            stem = json.optString("stem"),
            displayName = json.optString("display_name").ifBlank { json.optString("stem") },
            model = json.optString("model"),
            agentCommand = json.optString("agent_command"),
            activeState = json.optString("active_state").ifBlank { "unknown" },
            lastStart = json.optString("last_start"),
            // `optInt` would turn an absent or null count into 0 — which is a
            // claim ("did no work"), not the absence of one. Every heartbeat
            // field stays nullable all the way to the card.
            toolCallsWindow = json.optIntOrNull("tool_calls_window"),
            toolCallsRecent = json.optIntOrNull("tool_calls_recent"),
            lastToolCallSecondsAgo = json.optIntOrNull("last_tool_call_seconds_ago"),
            working = json.optBooleanOrNull("working"),
        )

        private fun JSONObject.optIntOrNull(key: String): Int? =
            if (isNull(key)) null else optInt(key).takeIf { has(key) }

        private fun JSONObject.optBooleanOrNull(key: String): Boolean? =
            if (!has(key) || isNull(key)) null else optBoolean(key)
    }
}
