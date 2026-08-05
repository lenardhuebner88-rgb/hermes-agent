package net.hermes.deck.model

import org.json.JSONObject

/**
 * A task the dispatcher refuses to start, and the reason it gives.
 *
 * These are the chains that stand still. Most of the buckets resolve
 * themselves — a repo lock frees when the holder finishes — but two of them
 * (`budget_held`, `role_mismatch`) wait for a person, and the operator is the
 * only person there is. Naming the bucket in German is the whole value: the raw
 * label `chain_worktree_serialized` on a phone screen explains nothing.
 */
data class DispatchHold(
    val taskId: String,
    val title: String?,
    val bucket: String,
    val reason: String?,
    val holder: String?,
    val checkedAt: Long,
) {
    val displayTitle: String get() = title?.takeIf { it.isNotBlank() } ?: taskId

    /** True when the hold cannot clear itself — those go to the action stack. */
    val needsOperator: Boolean
        get() = bucket in NEEDS_OPERATOR

    val label: String
        get() = when (bucket) {
            "repo_serialized" -> "Repo belegt"
            "chain_worktree_serialized" -> "Kette hält den Worktree"
            "budget_held" -> "Budget gesperrt"
            "respawn_guarded" -> "Neustart gebremst"
            "role_mismatch" -> "Rolle passt nicht"
            "per_profile_capped" -> "Profil am Limit"
            else -> bucket.replace('_', ' ')
        }

    /** One line that says what stands in the way, without repeating the label. */
    val detail: String?
        get() = when {
            !reason.isNullOrBlank() -> reason
            !holder.isNullOrBlank() -> "gehalten von $holder"
            else -> null
        }

    companion object {
        private val NEEDS_OPERATOR = setOf("budget_held", "role_mismatch")

        fun fromJson(json: JSONObject, checkedAt: Long): DispatchHold = DispatchHold(
            taskId = json.optString("task_id"),
            title = json.optString("title").ifBlank { null },
            bucket = json.optString("bucket").ifBlank { "unknown" },
            reason = json.optString("reason").ifBlank { null },
            holder = json.optString("holder").ifBlank { null },
            checkedAt = checkedAt,
        )
    }
}
