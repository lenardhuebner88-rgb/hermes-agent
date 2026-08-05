package net.hermes.deck.model

/**
 * One ranked stack of things that want the operator, assembled from five sources.
 *
 * This is the deck's answer to a question the old screens kept handing back:
 * four separate panels — agents, runs, holds, budget — each true, none of them
 * saying what to *do*. The operator was left to diff them in their head every
 * time they picked up the phone. So the stack inverts it: everything that needs
 * a decision lands in one list, ranked, each row carrying exactly one action.
 *
 * Assembled here rather than in a composable for the same reason
 * [ControlSummary] is: a ranking spread across four `if` blocks in a UI tree
 * cannot be tested, and will eventually disagree with itself about what counts
 * as urgent.
 *
 * The ranking is severity first, then age. Age breaks ties on purpose — of two
 * equally bad things, the one that has been bad longer has already cost more.
 */
object ActionStack {

    enum class Severity { CRITICAL, WARNING, INFO }

    enum class Kind {
        /** A Buzz approval waiting in a channel. */
        DECISION,

        /** A dispatch hold that will not clear on its own. */
        HOLD,

        /** A run past its own declared time budget. */
        RUNAWAY,

        /** A run whose worker stopped reporting. */
        STALLED,

        /** A systemd unit that is down. */
        AGENT_FAILED,

        /** A unit that is up and has done nothing for a long time. */
        AGENT_SILENT,

        /** A provider budget close enough to bite. */
        BUDGET,

        /** A task whose due date has passed. */
        OVERDUE,
    }

    /** What tapping the row acts on. The UI maps this to a sheet, not to a call. */
    sealed interface Target {
        data class Run(val runId: Long, val taskId: String) : Target
        data class Task(val taskId: String) : Target
        data class Agent(val stem: String) : Target
        data object Budget : Target
    }

    data class Item(
        val kind: Kind,
        val severity: Severity,
        val title: String,
        val detail: String?,
        /** Null when the source cannot say how long — shown as nothing, never as 0. */
        val ageSeconds: Int?,
        val target: Target,
    ) {
        val key: String get() = "${kind.name}:${targetKey}"

        private val targetKey: String
            get() = when (target) {
                is Target.Run -> "run-${target.runId}"
                is Target.Task -> "task-${target.taskId}"
                is Target.Agent -> "agent-${target.stem}"
                Target.Budget -> "budget"
            }
    }

    /** A Buzz approval, reduced to what the stack needs to rank it. */
    data class PendingDecision(val taskId: String, val title: String, val ageSeconds: Int?)

    /**
     * An agent that is up but has made no tool call for this long counts as
     * stuck rather than idle. Six hours is deliberately generous: the fleet has
     * genuinely quiet lanes, and a stack that cries every evening gets ignored.
     */
    const val SILENT_AGENT_SECONDS: Int = 6 * 3600

    /** Budget above this share is close enough that the next long run may hit it. */
    const val TIGHT_BUDGET_PERCENT: Double = 85.0

    /**
     * A run whose last heartbeat note is this old has stopped narrating. It may
     * still be working — which is why this is a warning and not a kill order.
     */
    const val STALLED_NOTE_SECONDS: Int = 20 * 60

    fun of(
        decisions: List<PendingDecision> = emptyList(),
        holds: List<DispatchHold> = emptyList(),
        runs: List<WorkerRun> = emptyList(),
        agents: List<AgentRow> = emptyList(),
        usage: Collection<AccountUsage> = emptyList(),
        overdue: List<DeckTask> = emptyList(),
        silentAfterSeconds: Int = SILENT_AGENT_SECONDS,
    ): List<Item> = buildList {
        decisions.forEach { decision ->
            add(
                Item(
                    kind = Kind.DECISION,
                    severity = Severity.CRITICAL,
                    title = decision.title,
                    detail = "wartet auf deine Freigabe",
                    ageSeconds = decision.ageSeconds,
                    target = Target.Task(decision.taskId),
                )
            )
        }

        holds.filter { it.needsOperator }.forEach { hold ->
            add(
                Item(
                    kind = Kind.HOLD,
                    severity = Severity.CRITICAL,
                    title = hold.displayTitle,
                    detail = listOfNotNull(hold.label, hold.detail).joinToString(" · "),
                    ageSeconds = null,
                    target = Target.Task(hold.taskId),
                )
            )
        }

        runs.forEach { run ->
            if (run.isOverBudget) {
                add(
                    Item(
                        kind = Kind.RUNAWAY,
                        severity = Severity.CRITICAL,
                        title = run.title,
                        detail = "über Zeitbudget · ${run.profile}",
                        ageSeconds = run.runtimeSeconds,
                        target = Target.Run(run.runId, run.taskId),
                    )
                )
            } else {
                val noteAge = run.noteAgeSeconds
                if (noteAge != null && noteAge >= STALLED_NOTE_SECONDS) {
                    add(
                        Item(
                            kind = Kind.STALLED,
                            severity = Severity.WARNING,
                            title = run.title,
                            detail = "meldet nichts mehr · ${run.profile}",
                            ageSeconds = noteAge,
                            target = Target.Run(run.runId, run.taskId),
                        )
                    )
                }
            }
        }

        agents.forEach { row ->
            if (row.agent.isFailed) {
                add(
                    Item(
                        kind = Kind.AGENT_FAILED,
                        severity = Severity.CRITICAL,
                        title = row.agent.displayName,
                        detail = "Unit gescheitert",
                        ageSeconds = null,
                        target = Target.Agent(row.agent.stem),
                    )
                )
                return@forEach
            }
            // Silence only counts against an agent that is supposed to be up.
            // A stopped unit is a different problem, and claiming both at once
            // would double-count the same agent in one list.
            val silentFor = row.agent.lastToolCallSecondsAgo
            if (row.agent.isRunning && silentFor != null && silentFor >= silentAfterSeconds) {
                add(
                    Item(
                        kind = Kind.AGENT_SILENT,
                        severity = Severity.WARNING,
                        title = row.agent.displayName,
                        detail = "läuft, tut aber nichts",
                        ageSeconds = silentFor,
                        target = Target.Agent(row.agent.stem),
                    )
                )
            }
        }

        usage.mapNotNull { entry -> entry.peakPercent?.let { entry to it } }
            .filter { it.second >= TIGHT_BUDGET_PERCENT }
            .maxByOrNull { it.second }
            ?.let { (entry, percent) ->
                add(
                    Item(
                        kind = Kind.BUDGET,
                        severity = if (percent >= 95.0) Severity.CRITICAL else Severity.WARNING,
                        title = entry.displayName,
                        detail = "${percent.toInt()} % verbraucht" +
                            if (entry.isStale) " · Zwischenspeicher" else "",
                        ageSeconds = null,
                        target = Target.Budget,
                    )
                )
            }

        overdue.forEach { task ->
            add(
                Item(
                    kind = Kind.OVERDUE,
                    severity = Severity.INFO,
                    title = task.title,
                    detail = "überfällig seit ${task.due}",
                    ageSeconds = null,
                    target = Target.Task(task.id),
                )
            )
        }
    }.sortedWith(
        compareBy<Item> { it.severity.ordinal }
            // Descending age: of two equally bad rows the older one has cost more.
            .thenByDescending { it.ageSeconds ?: -1 }
            .thenBy { it.title }
    )

    /**
     * The one line the deck's control card shows. An empty stack is a result,
     * not a blank — saying so is the difference between "nothing to do" and
     * "this panel is broken", which the app has confused before.
     */
    fun headline(items: List<Item>): String = when {
        items.isEmpty() -> "Nichts liegt an"
        else -> {
            val critical = items.count { it.severity == Severity.CRITICAL }
            if (critical > 0) "$critical dringend · ${items.size} gesamt"
            else "${items.size} ${if (items.size == 1) "Sache" else "Sachen"}"
        }
    }
}
