package net.hermes.deck.model

import org.json.JSONObject

/**
 * One Buzz agent as the Hermes dashboard reports it.
 *
 * The model is whatever `BUZZ_ACP_MODEL` says in the agent's env file; it only
 * takes effect after the unit restarts, which is why [activeState] and
 * [lastStart] are shown next to it — a changed value with a stale start time
 * means the agent is still answering with the old model.
 */
data class BuzzAgent(
    val stem: String,
    val displayName: String,
    val model: String,
    val agentCommand: String,
    val activeState: String,
    val lastStart: String,
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
        )
    }
}
