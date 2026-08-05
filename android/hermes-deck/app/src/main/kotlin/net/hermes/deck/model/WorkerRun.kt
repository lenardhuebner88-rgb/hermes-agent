package net.hermes.deck.model

import org.json.JSONObject

/**
 * One Hermes kanban worker that is running right now.
 *
 * This is the half of "who is working" the deck never had: Buzz agents answer
 * *which model is awake*, a worker run answers *which card is being worked*.
 *
 * Every duration here is derived against `checked_at` — the server's own clock,
 * shipped with the payload — never against the phone's. Two clocks that differ
 * by a minute would otherwise turn a healthy run into an overdue one.
 */
data class WorkerRun(
    val runId: Long,
    val taskId: String,
    val title: String,
    val profile: String,
    val startedAt: Long,
    val checkedAt: Long,
    val maxRuntimeSeconds: Int?,
    val runProgress: Double?,
    val etaP50Seconds: Int?,
    val livenessState: String,
    val livenessReason: String?,
    val blockReason: String?,
    val heartbeatNote: String?,
    val heartbeatNoteAt: Long?,
    val inputTokens: Int?,
    val outputTokens: Int?,
) {
    val runtimeSeconds: Int
        get() = if (startedAt <= 0L || checkedAt <= 0L) 0 else (checkedAt - startedAt).coerceAtLeast(0L).toInt()

    /** Silence since the last heartbeat note, or null when there never was one. */
    val noteAgeSeconds: Int?
        get() = heartbeatNoteAt?.takeIf { it > 0L && checkedAt > 0L }
            ?.let { (checkedAt - it).coerceAtLeast(0L).toInt() }

    /**
     * Past its own declared budget. Only claimed when the run actually carries
     * one — uncapped lanes exist, and a missing cap is not an infinite one.
     */
    val isOverBudget: Boolean
        get() = maxRuntimeSeconds?.let { it > 0 && runtimeSeconds > it } == true

    val isBlocked: Boolean
        get() = livenessState.equals("blocked", ignoreCase = true) ||
            livenessState.equals("offline", ignoreCase = true) ||
            blockReason?.isNotBlank() == true

    val totalTokens: Int?
        get() = when {
            inputTokens == null && outputTokens == null -> null
            else -> (inputTokens ?: 0) + (outputTokens ?: 0)
        }

    /**
     * Progress the server is willing to stand behind, else the ETA heuristic,
     * else nothing. Never an invented percentage: an uncapped run showing 60 %
     * is worse than a run showing no bar at all.
     */
    val progressFraction: Double?
        get() = runProgress
            ?: etaP50Seconds?.takeIf { it > 0 }?.let { (runtimeSeconds.toDouble() / it).coerceIn(0.0, 1.0) }

    companion object {
        fun fromJson(json: JSONObject, checkedAt: Long): WorkerRun = WorkerRun(
            runId = json.optLong("run_id"),
            taskId = json.optString("task_id"),
            title = json.optString("task_title").ifBlank { json.optString("task_id") },
            profile = json.optString("profile").ifBlank { "—" },
            startedAt = json.optLong("started_at"),
            checkedAt = checkedAt,
            maxRuntimeSeconds = json.optIntOrNull("max_runtime_seconds"),
            runProgress = if (json.isNull("run_progress")) null else json.optDouble("run_progress").takeIf { !it.isNaN() },
            etaP50Seconds = json.optIntOrNull("eta_p50_seconds"),
            livenessState = json.optString("liveness_state").ifBlank { "unknown" },
            livenessReason = json.optString("liveness_reason").ifBlank { null },
            blockReason = json.optString("block_reason").ifBlank { null },
            heartbeatNote = json.optString("last_heartbeat_note").ifBlank { null },
            heartbeatNoteAt = json.optLong("last_heartbeat_note_at").takeIf { it > 0L },
            inputTokens = json.optIntOrNull("input_tokens"),
            outputTokens = json.optIntOrNull("output_tokens"),
        )

        private fun JSONObject.optIntOrNull(key: String): Int? =
            if (!has(key) || isNull(key)) null else optInt(key)
    }
}
