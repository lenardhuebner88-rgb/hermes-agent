package net.hermes.deck.model

import org.json.JSONArray
import org.json.JSONObject

/**
 * The whole `/api/deck/pulse` payload, one section at a time.
 *
 * The endpoint bundles four independent sources so the phone makes one request
 * per tick instead of four. The price of bundling is that a client written the
 * obvious way — read the payload, render it — turns one broken source into four
 * empty screens. So the section is a first-class type here: it carries its own
 * error, and a UI that wants to render a section has to decide what to do with
 * that error first, because it cannot reach the data without passing it.
 */
data class Section<T>(val data: T?, val error: String?) {
    val isBroken: Boolean get() = error != null
    fun orEmpty(fallback: T): T = data ?: fallback

    companion object {
        fun <T> of(json: JSONObject?, parse: (JSONObject) -> T): Section<T> {
            if (json == null) return Section(null, "Antwort ohne diesen Abschnitt")
            val error = json.optString("error").ifBlank { null }?.takeIf { it != "null" }
            val payload = json.optJSONObject("data")
            if (payload == null) return Section(null, error ?: "Abschnitt leer")
            return Section(runCatching { parse(payload) }.getOrNull(), error)
        }
    }
}

/** One entry of the cross-worker ticker: what finished, failed or blocked. */
data class DeckEvent(
    val id: Long,
    val kind: String,
    val taskId: String,
    val title: String,
    val profile: String?,
    val note: String?,
    val at: Long,
) {
    val isFailure: Boolean get() = kind in FAILURE_KINDS

    /**
     * German, and a *verb* wherever possible.
     *
     * The board's own kinds are English snake_case and leak straight onto the
     * screen otherwise — a stream reading "heartbeat / unblocked / claimed"
     * next to German prose. Anything not listed still falls through to the raw
     * kind: an unknown event must stay visible and legible, not be swallowed.
     */
    val label: String
        get() = when (kind) {
            "completed" -> "fertig"
            "blocked" -> "blockiert"
            "unblocked" -> "entsperrt"
            "claimed" -> "übernommen"
            "failed", "infra_failed" -> "gescheitert"
            "landed" -> "gelandet"
            "heartbeat" -> "meldet sich"
            "started" -> "gestartet"
            "scheduled" -> "eingeplant"
            "submitted_for_review" -> "zur Prüfung"
            "review_diff_snapshot" -> "Diff geprüft"
            "integration_merged" -> "gemerged"
            "freigabe_released" -> "freigegeben"
            "freigabe_vetoed" -> "abgelehnt"
            "closeout_receipt_written" -> "Beleg geschrieben"
            else -> kind.replace('_', ' ')
        }

    companion object {
        private val FAILURE_KINDS = setOf("failed", "infra_failed", "blocked")

        fun fromJson(json: JSONObject): DeckEvent = DeckEvent(
            id = json.optLong("id"),
            kind = json.optString("kind").ifBlank { "unknown" },
            taskId = json.nullable("task_id").orEmpty(),
            title = json.nullable("task_title") ?: json.nullable("task_id") ?: "ohne Titel",
            profile = json.nullable("profile"),
            note = json.nullable("note"),
            at = json.optLong("at"),
        )

        /**
         * `optString` on a JSON `null` hands back the four letters "null", and
         * the stream printed exactly that under six of twenty events before a
         * screenshot showed it. A missing field has to stay missing.
         */
        private fun JSONObject.nullable(key: String): String? =
            if (isNull(key)) null else optString(key).ifBlank { null }
    }
}

/**
 * One agent, what it is doing, and what its session is made of.
 *
 * The three travel together because every card needs all three and any two of
 * them alone tell a misleading story: activity without context hides an agent
 * about to hit the wall, context without activity hides a hang.
 */
data class AgentRow(
    val agent: BuzzAgent,
    val pulse: AgentPulse?,
    val session: SessionFacts? = null,
)

/**
 * The agent section. Both windows travel with it so no card hardcodes a number
 * the server is free to retune.
 */
data class AgentsBlock(
    val rows: List<AgentRow>,
    val activityError: String?,
    val activityWindowSeconds: Int,
    val heartbeatWindowSeconds: Int,
    val heartbeatRecentSeconds: Int,
    val heartbeatError: String?,
    /**
     * The server saw journal lines and understood none of them. That is a
     * format change, not a quiet fleet, and it is the one failure this whole
     * feature is most likely to die of silently.
     */
    val parserSuspicious: Boolean,
)

data class DeckPulse(
    /** Device clock at the moment the payload arrived — the basis for freshness. */
    val receivedAt: Long,
    /**
     * The *server's* clock at capture, in seconds. Board events carry an
     * absolute `at` from that same clock, so `serverNow - at` is a legitimate
     * age — where mixing in the device clock would not be.
     */
    val serverNow: Long,
    val board: String?,
    val agents: Section<AgentsBlock>,
    val workers: Section<List<WorkerRun>>,
    val holds: Section<List<DispatchHold>>,
    val events: Section<List<DeckEvent>>,
    val burn: Section<BurnRate>,
) {
    val agentRows: List<AgentRow> get() = agents.data?.rows.orEmpty()
    val workerRuns: List<WorkerRun> get() = workers.data.orEmpty()
    val heldTasks: List<DispatchHold> get() = holds.data.orEmpty()
    val recentEvents: List<DeckEvent> get() = events.data.orEmpty()
    val burnRate: BurnRate? get() = burn.data

    /** Sections that could not be read at all — shown as a band, never swallowed. */
    val brokenSections: List<String>
        get() = buildList {
            if (agents.isBroken) add("Agenten")
            if (workers.isBroken) add("Läufe")
            if (holds.isBroken) add("Blockaden")
            if (events.isBroken) add("Ereignisse")
        }

    companion object {
        fun fromJson(json: JSONObject, receivedAt: Long): DeckPulse {
            val checkedAt = json.optJSONObject("workers")
                ?.optJSONObject("data")?.optLong("checked_at")
                ?.takeIf { it > 0L }
                ?: (receivedAt / 1000L)

            return DeckPulse(
                receivedAt = receivedAt,
                serverNow = checkedAt,
                board = json.optString("board").ifBlank { null },
                agents = Section.of(json.optJSONObject("agents")) { parseAgents(it) },
                workers = Section.of(json.optJSONObject("workers")) { block ->
                    block.optJSONArray("workers").mapObjects { WorkerRun.fromJson(it, checkedAt) }
                },
                holds = Section.of(json.optJSONObject("holds")) { block ->
                    block.optJSONArray("holds").mapObjects {
                        DispatchHold.fromJson(it, block.optLong("checked_at", checkedAt))
                    }
                },
                events = Section.of(json.optJSONObject("events")) { block ->
                    block.optJSONArray("events").mapObjects(DeckEvent::fromJson)
                },
                burn = Section.of(json.optJSONObject("burn")) { block ->
                    checkNotNull(BurnRate.fromJson(block))
                },
            )
        }

        private fun parseAgents(json: JSONObject): AgentsBlock {
            val array = json.optJSONArray("agents") ?: JSONArray()
            val rows = (0 until array.length()).mapNotNull { index ->
                val entry = array.optJSONObject(index) ?: return@mapNotNull null
                val agent = BuzzAgent.fromJson(entry)
                AgentRow(
                    agent = agent,
                    pulse = AgentPulse.fromJson(agent.stem, entry.optJSONObject("activity")),
                    session = SessionFacts.fromJson(entry.optJSONObject("session")),
                )
            }
            return AgentsBlock(
                rows = rows,
                activityError = json.optString("activity_error").ifBlank { null }
                    ?.takeIf { it != "null" },
                activityWindowSeconds = json.optInt("activity_window_seconds", 1800),
                heartbeatWindowSeconds = json.optInt("heartbeat_window_seconds", 3600),
                heartbeatRecentSeconds = json.optInt("heartbeat_recent_seconds", 300),
                heartbeatError = json.optString("heartbeat_error").ifBlank { null }
                    ?.takeIf { it != "null" },
                parserSuspicious = json.optJSONObject("activity_diagnostics")
                    ?.optBoolean("suspicious", false) ?: false,
            )
        }

        private fun <T> JSONArray?.mapObjects(parse: (JSONObject) -> T): List<T> {
            if (this == null) return emptyList()
            return (0 until length()).mapNotNull { optJSONObject(it)?.let(parse) }
        }
    }
}
