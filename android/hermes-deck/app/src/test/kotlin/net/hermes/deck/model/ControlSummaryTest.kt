package net.hermes.deck.model

import java.time.LocalDate
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class ControlSummaryTest {

    private val today = LocalDate.of(2026, 8, 5)

    private fun agent(stem: String, working: Boolean?, window: Int? = 0, state: String = "active") =
        BuzzAgent(
            stem = stem,
            displayName = stem.replaceFirstChar { it.uppercase() },
            model = "m",
            agentCommand = "c",
            activeState = state,
            lastStart = "x",
            toolCallsWindow = window,
            toolCallsRecent = if (working == true) 3 else 0,
            lastToolCallSecondsAgo = if (working == true) 5 else null,
            working = working,
        )

    private fun task(id: String, due: String? = null, closed: Boolean = false) = DeckTask(
        id = id,
        channelId = "c",
        authorPubkey = "a",
        title = id,
        body = "",
        status = if (closed) TaskStatus.DONE else TaskStatus.OPEN,
        priority = TaskPriority.NORMAL,
        due = due,
        createdAt = 0,
        updatedAt = 0,
        attachments = emptyList(),
        assignees = emptyList(),
    )

    private fun summary(
        tasks: List<DeckTask> = emptyList(),
        agents: List<BuzzAgent> = emptyList(),
        usage: List<AccountUsage> = emptyList(),
        activeThreads: Int = 0,
        decisions: Int = 0,
        mentions: Int = 0,
        pending: Int = 0,
        lastSyncAt: Long = 1000,
        failure: String? = null,
        syncing: Boolean = false,
    ) = ControlSummary.of(
        tasks = tasks,
        agents = agents,
        heartbeatWindowSeconds = 3600,
        heartbeatRecentSeconds = 300,
        usage = usage,
        activeThreads = List(activeThreads) {
            ThreadActivity.Entry("root$it", "t$it", "T", "c", "chan", 1, 0, "p", "P", "x", true)
        },
        decisions = decisions,
        mentions = mentions,
        today = today,
        pending = pending,
        lastSyncAt = lastSyncAt,
        failure = failure,
        syncing = syncing,
    )

    @Test
    fun `the quiet half is spelled out, not hidden behind the working count`() {
        // The measured case of 2026-08-05: all eight units "active", three
        // working. "3 arbeiten" alone reads as all-good; the gap is the point.
        val agents = listOf(
            agent("claude", working = true),
            agent("qwen", working = true),
            agent("hermes", working = true),
            agent("fable", working = false),
            agent("grok", working = false),
            agent("kimi", working = false),
            agent("terra", working = false),
            agent("codex", working = false),
        )

        val pulse = summary(agents = agents).pulse

        assertEquals("3 arbeiten", pulse.headline)
        assertEquals(listOf("Claude", "Qwen", "Hermes"), pulse.working)
        assertEquals(5, pulse.idle)
        assertEquals("5 laufen leer", pulse.rest)
    }

    @Test
    fun `nobody working says so plainly instead of going silent`() {
        val pulse = summary(agents = listOf(agent("a", working = false))).pulse

        assertEquals("Niemand arbeitet", pulse.headline)
    }

    @Test
    fun `an unreadable heartbeat is unknown, never idle`() {
        val pulse = summary(agents = listOf(agent("a", working = null, window = null))).pulse

        assertEquals("Aktivität unbekannt", pulse.headline)
        assertEquals(1, pulse.unknown)
        assertEquals(0, pulse.idle)
    }

    @Test
    fun `no agents at all is distinct from agents that are idle`() {
        assertEquals("Keine Agenten gemeldet", summary().pulse.headline)
    }

    @Test
    fun `singular reads as a sentence`() {
        val pulse = summary(agents = listOf(agent("a", working = true))).pulse

        assertEquals("1 arbeitet", pulse.headline)
    }

    @Test
    fun `overdue and due-today are counted apart`() {
        val tasks = listOf(
            task("alt", due = "2026-08-01"),
            task("heute", due = "2026-08-05"),
            task("morgen", due = "2026-08-06"),
            task("erledigt", due = "2026-08-01", closed = true),
        )

        val waiting = summary(tasks = tasks).waiting

        assertEquals(1, waiting.overdue)
        assertEquals(1, waiting.dueToday)
        // A closed task is not waiting on anyone.
        assertFalse(waiting.isEmpty)
    }

    @Test
    fun `nothing waiting is empty so the band can disappear entirely`() {
        assertTrue(summary().waiting.isEmpty)
    }

    @Test
    fun `capacity names the tightest budget across providers`() {
        val usage = listOf(
            usageOf("Claude", 63.0),
            usageOf("Kimi", 74.0, stale = true),
            usageOf("Grok", 43.0),
        )

        val capacity = summary(usage = usage).capacity!!

        assertEquals("Kimi", capacity.provider)
        assertEquals(74.0, capacity.percent, 0.001)
        assertTrue(capacity.stale)
        assertEquals(2, capacity.others)
    }

    @Test
    fun `a provider without a percentage cannot be the tightest`() {
        val capacity = summary(usage = listOf(usageOf("OpenRouter", null))).capacity

        // No bar rather than a 0 % bar reading "plenty left".
        assertNull(capacity)
    }

    @Test
    fun `a sync failure outranks pending and freshness`() {
        val delivery = summary(pending = 4, failure = "Relay nicht erreichbar").delivery

        assertFalse(delivery.isHealthy)
        assertEquals("Relay nicht erreichbar", delivery.failure)
    }

    @Test
    fun `a never-synced deck is not healthy even with nothing pending`() {
        assertFalse(summary(lastSyncAt = 0).delivery.isHealthy)
        assertTrue(summary(lastSyncAt = 1000).delivery.isHealthy)
    }

    @Test
    fun `active threads reach the pulse band`() {
        assertEquals(4, summary(activeThreads = 4).pulse.activeThreads)
    }

    @Test
    fun `mentions count towards what is waiting`() {
        assertEquals(2, summary(mentions = 2).waiting.mentions)
        assertFalse(summary(mentions = 2).waiting.isEmpty)
    }

    private fun usageOf(name: String, percent: Double?, stale: Boolean = false) = AccountUsage(
        provider = name.lowercase(),
        available = true,
        title = name,
        plan = null,
        windows = listOfNotNull(
            percent?.let { AccountUsage.Window("W", "weekly", it, null, null) },
        ),
        details = emptyList(),
        unavailableReason = null,
        fallback = stale,
        cached = false,
        fetchedAt = null,
    )
}
