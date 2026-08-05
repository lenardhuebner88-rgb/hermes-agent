package net.hermes.deck.model

import java.time.LocalDate

/**
 * Everything the first screen's control card states, in one value.
 *
 * Assembled here rather than in the composable so the claims can be tested
 * without a device — and so the card cannot quietly disagree with itself by
 * counting the same thing two ways in two places.
 *
 * The four bands answer, in order: what is running, what wants me, how much
 * budget is left, and did my last capture actually arrive. None of those are
 * things a chat client can show, which is the point of the card existing next
 * to Buzz rather than duplicating it.
 */
data class ControlSummary(
    val pulse: Pulse,
    val waiting: Waiting,
    val capacity: Capacity?,
    val delivery: Delivery,
) {
    data class Pulse(
        val working: List<String>,
        val idle: Int,
        val failed: Int,
        /** Null when the heartbeat could not be measured at all. */
        val unknown: Int,
        val activeThreads: Int,
    ) {
        val hasAnyAgentInfo: Boolean get() = working.isNotEmpty() || idle > 0 || failed > 0 || unknown > 0

        val headline: String
            get() = when {
                working.isNotEmpty() -> "${working.size} ${verb(working.size)}"
                unknown > 0 && idle == 0 && failed == 0 -> "Aktivität unbekannt"
                hasAnyAgentInfo -> "Niemand arbeitet"
                else -> "Keine Agenten gemeldet"
            }

        /**
         * The quiet half, spelled out. "3 arbeiten" alone reads as "all good"
         * when the other five are idle — the gap is the information.
         */
        val rest: String?
            get() = buildList {
                if (idle > 0) add("$idle ${if (idle == 1) "läuft leer" else "laufen leer"}")
                if (failed > 0) add("$failed gescheitert")
                if (unknown > 0) add("$unknown unbekannt")
            }.joinToString(" · ").ifBlank { null }

        private fun verb(count: Int) = if (count == 1) "arbeitet" else "arbeiten"
    }

    data class Waiting(
        val decisions: Int,
        val overdue: Int,
        val dueToday: Int,
        val mentions: Int,
    ) {
        val total: Int get() = decisions + overdue + dueToday + mentions
        val isEmpty: Boolean get() = total == 0
    }

    /** The tightest budget across all providers — the one that will bite first. */
    data class Capacity(
        val provider: String,
        val percent: Double,
        val stale: Boolean,
        val others: Int,
    )

    data class Delivery(
        val pending: Int,
        val lastSyncAt: Long,
        val failure: String?,
        val syncing: Boolean,
    ) {
        /**
         * A failure outranks everything: it stayed hidden once already because
         * it was shown as a transient message and vanished after four seconds,
         * leaving a screen indistinguishable from "nothing captured yet".
         */
        val isHealthy: Boolean get() = failure == null && pending == 0 && lastSyncAt > 0L
    }

    companion object {
        fun of(
            tasks: List<DeckTask>,
            agents: Collection<BuzzAgent>,
            heartbeatWindowSeconds: Int,
            heartbeatRecentSeconds: Int,
            usage: Collection<AccountUsage>,
            activeThreads: Collection<ThreadActivity.Entry>,
            decisions: Int,
            mentions: Int,
            today: LocalDate,
            pending: Int,
            lastSyncAt: Long,
            failure: String?,
            syncing: Boolean,
        ): ControlSummary {
            val readings = agents.map { it to AgentActivity.of(it, heartbeatWindowSeconds, heartbeatRecentSeconds) }
            val pulse = Pulse(
                working = readings
                    .filter { it.second.state == AgentActivity.State.WORKING }
                    .map { it.first.displayName },
                idle = readings.count {
                    it.second.state == AgentActivity.State.RECENT ||
                        it.second.state == AgentActivity.State.QUIET
                },
                failed = readings.count { it.second.state == AgentActivity.State.FAILED },
                unknown = readings.count { it.second.state == AgentActivity.State.UNKNOWN },
                activeThreads = activeThreads.size,
            )
            val tightest = usage
                .mapNotNull { entry -> entry.peakPercent?.let { entry to it } }
                .maxByOrNull { it.second }
            return ControlSummary(
                pulse = pulse,
                waiting = Waiting(
                    decisions = decisions,
                    overdue = TaskFilter.overdue(tasks, today).size,
                    dueToday = tasks.count { !it.status.isClosed && it.due == today.toString() },
                    mentions = mentions,
                ),
                capacity = tightest?.let { (entry, percent) ->
                    Capacity(
                        provider = entry.displayName,
                        percent = percent,
                        stale = entry.isStale,
                        others = usage.count { it.peakPercent != null } - 1,
                    )
                },
                delivery = Delivery(
                    pending = pending,
                    lastSyncAt = lastSyncAt,
                    failure = failure,
                    syncing = syncing,
                ),
            )
        }
    }
}
