package net.hermes.deck.model

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * The ranking is the product. If it is wrong, the operator works the wrong thing
 * first — which is worse than showing four separate panels, because the stack
 * looks authoritative.
 */
class ActionStackTest {

    /** A plausible epoch. Small values would make `started_at` go negative and
     *  the model would — correctly — refuse to compute a runtime from it. */
    private val CLOCK = 1_800_000_000L

    private fun agent(
        stem: String,
        state: String = "active",
        silentFor: Int? = 10,
    ) = AgentRow(
        agent = BuzzAgent(
            stem = stem,
            displayName = stem.replaceFirstChar { it.uppercase() },
            model = "m",
            agentCommand = "c",
            activeState = state,
            lastStart = "",
            toolCallsWindow = 0,
            toolCallsRecent = 0,
            lastToolCallSecondsAgo = silentFor,
            working = false,
        ),
        pulse = null,
    )

    private fun run(
        id: Long,
        title: String,
        runtime: Int,
        budget: Int? = null,
        noteAge: Int? = null,
    ) = WorkerRun(
        runId = id,
        taskId = "T-$id",
        title = title,
        profile = "coder",
        startedAt = CLOCK - runtime,
        checkedAt = CLOCK,
        maxRuntimeSeconds = budget,
        runProgress = null,
        etaP50Seconds = null,
        livenessState = "alive",
        livenessReason = null,
        blockReason = null,
        heartbeatNote = noteAge?.let { "arbeitet" },
        heartbeatNoteAt = noteAge?.let { CLOCK - it },
        inputTokens = null,
        outputTokens = null,
    )

    private fun hold(id: String, bucket: String) =
        DispatchHold(taskId = id, title = "Karte $id", bucket = bucket, reason = null, holder = null, checkedAt = 0)

    @Test
    fun `an empty system produces an empty stack and says so`() {
        val items = ActionStack.of()
        assertTrue(items.isEmpty())
        assertEquals("Nichts liegt an", ActionStack.headline(items))
    }

    @Test
    fun `approvals outrank everything else`() {
        val items = ActionStack.of(
            decisions = listOf(ActionStack.PendingDecision("T-1", "Deploy freigeben", 30)),
            overdue = listOf(task("T-2", "alte Karte")),
            runs = listOf(run(1, "läuft lang", runtime = 500, noteAge = 3600)),
        )
        assertEquals(ActionStack.Kind.DECISION, items.first().kind)
        assertEquals(ActionStack.Severity.CRITICAL, items.first().severity)
    }

    @Test
    fun `only holds that need a person enter the stack`() {
        val items = ActionStack.of(
            holds = listOf(
                hold("T-1", "budget_held"),
                hold("T-2", "repo_serialized"),
                hold("T-3", "chain_worktree_serialized"),
                hold("T-4", "role_mismatch"),
            )
        )
        // A repo or worktree lock clears when the holder finishes. Listing it as
        // an action would train the operator to ignore the list.
        assertEquals(2, items.size)
        assertEquals(setOf("T-1", "T-4"), items.map { (it.target as ActionStack.Target.Task).taskId }.toSet())
    }

    @Test
    fun `a run over its budget is critical, a merely quiet one is a warning`() {
        val items = ActionStack.of(
            runs = listOf(
                run(1, "überzogen", runtime = 900, budget = 600),
                run(2, "still", runtime = 900, noteAge = 25 * 60),
                run(3, "gesund", runtime = 100, budget = 600, noteAge = 30),
            )
        )
        assertEquals(2, items.size)
        assertEquals(ActionStack.Kind.RUNAWAY, items[0].kind)
        assertEquals(ActionStack.Severity.CRITICAL, items[0].severity)
        assertEquals(ActionStack.Kind.STALLED, items[1].kind)
        assertEquals(ActionStack.Severity.WARNING, items[1].severity)
    }

    @Test
    fun `an overrunning run is not also reported as stalled`() {
        // Both conditions hold for the same run; reporting both would put one
        // problem in the list twice and inflate the "dringend" count.
        val items = ActionStack.of(runs = listOf(run(1, "beides", runtime = 9000, budget = 600, noteAge = 7200)))
        assertEquals(1, items.size)
        assertEquals(ActionStack.Kind.RUNAWAY, items.single().kind)
    }

    @Test
    fun `a failed unit is not also reported as silent`() {
        val items = ActionStack.of(agents = listOf(agent("grok", state = "failed", silentFor = 99_999)))
        assertEquals(1, items.size)
        assertEquals(ActionStack.Kind.AGENT_FAILED, items.single().kind)
    }

    @Test
    fun `silence only counts past the threshold`() {
        val quiet = ActionStack.of(agents = listOf(agent("kimi", silentFor = 60)))
        assertTrue(quiet.isEmpty())

        val stuck = ActionStack.of(agents = listOf(agent("kimi", silentFor = 7 * 3600)))
        assertEquals(ActionStack.Kind.AGENT_SILENT, stuck.single().kind)
    }

    @Test
    fun `an agent whose activity was never measured is not accused of idling`() {
        // `lastToolCallSecondsAgo == null` means the journal could not be read.
        // Treating that as silence would blame the agent for a server problem.
        val items = ActionStack.of(agents = listOf(agent("qwen", silentFor = null)))
        assertTrue(items.isEmpty())
    }

    @Test
    fun `only the tightest budget is reported, and only when it is tight`() {
        val comfortable = ActionStack.of(usage = listOf(usage("Opus", 40.0)))
        assertTrue(comfortable.isEmpty())

        val items = ActionStack.of(usage = listOf(usage("Opus", 88.0), usage("Sonnet", 97.0)))
        assertEquals(1, items.size)
        assertEquals("Sonnet", items.single().title)
        assertEquals(ActionStack.Severity.CRITICAL, items.single().severity)
    }

    @Test
    fun `age breaks ties within a severity`() {
        val items = ActionStack.of(
            decisions = listOf(
                ActionStack.PendingDecision("T-neu", "neu", 10),
                ActionStack.PendingDecision("T-alt", "alt", 9_000),
            )
        )
        assertEquals(listOf("alt", "neu"), items.map { it.title })
    }

    @Test
    fun `severity beats age across bands`() {
        val items = ActionStack.of(
            decisions = listOf(ActionStack.PendingDecision("T-1", "frische Freigabe", 5)),
            agents = listOf(agent("kimi", silentFor = 40 * 3600)),
        )
        assertEquals("frische Freigabe", items.first().title)
    }

    @Test
    fun `keys are stable and unique per row`() {
        val items = ActionStack.of(
            decisions = listOf(ActionStack.PendingDecision("T-1", "a", 1)),
            holds = listOf(hold("T-1", "budget_held")),
            runs = listOf(run(1, "b", runtime = 900, budget = 60)),
        )
        assertEquals(items.size, items.map { it.key }.toSet().size)
    }

    @Test
    fun `the headline counts the urgent band separately`() {
        val items = ActionStack.of(
            decisions = listOf(ActionStack.PendingDecision("T-1", "a", 1)),
            agents = listOf(agent("kimi", silentFor = 40 * 3600)),
        )
        assertEquals("1 dringend · 2 gesamt", ActionStack.headline(items))

        val warningsOnly = ActionStack.of(agents = listOf(agent("kimi", silentFor = 40 * 3600)))
        assertEquals("1 Sache", ActionStack.headline(warningsOnly))
        assertFalse(ActionStack.headline(warningsOnly).contains("dringend"))
    }

    private fun task(id: String, title: String) = DeckTask(
        id = id,
        channelId = "c",
        authorPubkey = "a",
        title = title,
        body = "",
        status = TaskStatus.OPEN,
        priority = TaskPriority.NORMAL,
        due = "2026-08-01",
        createdAt = 0L,
        updatedAt = 0L,
        attachments = emptyList(),
        assignees = emptyList(),
    )

    private fun usage(name: String, percent: Double) = AccountUsage(
        provider = name.lowercase(),
        available = true,
        title = name,
        plan = null,
        windows = listOf(
            AccountUsage.Window(
                label = "Woche",
                windowKey = "week",
                usedPercent = percent,
                resetAt = null,
                detail = null,
            )
        ),
        details = emptyList(),
        unavailableReason = null,
        fallback = false,
        cached = false,
        fetchedAt = null,
    )
}
