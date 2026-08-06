package net.hermes.deck.model

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * The entry screen has no tabs: it shows whatever most needs the operator, and
 * it changes because the system changed. That makes this ranking the screen's
 * actual behaviour — if it is wrong, the wrong thing is on the phone.
 */
class FocusPickTest {

    private fun row(
        stem: String,
        state: String = "active",
        open: Boolean = false,
        callAgo: Int = 10,
        signalAgo: Int? = 5,
        calls: Int = 3,
    ) = AgentRow(
        agent = BuzzAgent(stem, stem.replaceFirstChar { it.uppercase() }, "m", "c", state, "",
            0, 0, callAgo, open),
        pulse = AgentPulse(
            stem = stem,
            callsInWindow = calls,
            latest = ToolCall(callAgo, "Terminal", ToolCall.Kind.EXECUTE),
            looksOpen = open,
            lastSignalSecondsAgo = signalAgo,
            recent = listOf(ToolCall(callAgo, "Terminal", ToolCall.Kind.EXECUTE)),
        ),
    )

    private fun AgentRow.copyPulseSignal(seconds: Int) =
        copy(pulse = pulse?.copy(lastSignalSecondsAgo = seconds))

    private fun session(percent: Int) = SessionFacts(
        sessionId = "s", ageSeconds = 3600, promptTokens = percent * 1000,
        contextWindow = 100_000, compacts = 0, lastCompaction = null,
        measuredSecondsAgo = 5, source = SessionFacts.Source.CLAUDE, steerable = true,
    )

    private fun usage(percent: Double) = AccountUsage(
        provider = "anthropic", available = true, title = "Claude", plan = null,
        windows = listOf(AccountUsage.Window("Woche", "week", percent, null, null)),
        details = emptyList(), unavailableReason = null,
        fallback = false, cached = false, fetchedAt = null,
    )

    @Test
    fun `the resting state is whoever works, not whoever has the fullest context`() {
        // The operator's ask, 2026-08-06: the card should say who is working
        // when — an idle agent with a fat context is not the news.
        val choice = FocusPick.of(
            rows = listOf(row("claude", open = true), row("qwen")),
            sessions = mapOf("claude" to session(20), "qwen" to session(88)),
        )
        assertEquals(FocusPick.Subject.Agent("claude"), choice.subject)
        assertEquals(FocusPick.Rank.RESTING, choice.rank)
        assertEquals("Im Blick · arbeitet", choice.reason)
    }

    @Test
    fun `among working agents the busiest one gets the card`() {
        val choice = FocusPick.of(
            rows = listOf(
                row("claude", open = true, calls = 4),
                row("codex", open = true, calls = 31),
            ),
            sessions = mapOf("claude" to session(90)),
        )
        assertEquals(FocusPick.Subject.Agent("codex"), choice.subject)
    }

    @Test
    fun `the card does not swap agents just because a signal ticked`() {
        // Ranked by calls, not by signal age: two busy agents would otherwise
        // hand the card back and forth on every eight-second poll.
        val rows = listOf(
            row("claude", open = true, calls = 20, signalAgo = 2),
            row("codex", open = true, calls = 31, signalAgo = 9),
        )
        assertEquals(FocusPick.Subject.Agent("codex"), FocusPick.of(rows).subject)
        val ticked = listOf(rows[0].copyPulseSignal(9), rows[1].copyPulseSignal(2))
        assertEquals(FocusPick.Subject.Agent("codex"), FocusPick.of(ticked).subject)
    }

    @Test
    fun `with nobody working the card shows who worked last`() {
        val choice = FocusPick.of(
            rows = listOf(row("claude", callAgo = 900), row("qwen", callAgo = 40)),
            sessions = mapOf("claude" to session(88)),
        )
        assertEquals(FocusPick.Subject.Agent("qwen"), choice.subject)
        assertEquals("Im Blick · zuletzt aktiv", choice.reason)
    }

    @Test
    fun `a context at the brim is an alarm, a merely full one is not`() {
        val rows = listOf(row("claude", open = true), row("qwen"))
        assertEquals(
            "Im Blick · arbeitet",
            FocusPick.of(rows, sessions = mapOf("qwen" to session(88))).reason,
        )
        val brim = FocusPick.of(rows, sessions = mapOf("qwen" to session(96)))
        assertEquals(FocusPick.Subject.Agent("qwen"), brim.subject)
        assertEquals(FocusPick.Rank.ALARM, brim.rank)
        assertEquals("Dringend · Kontext fast voll", brim.reason)
    }

    @Test
    fun `a budget about to bite outranks every agent`() {
        val choice = FocusPick.of(
            rows = listOf(row("claude"), row("qwen")),
            usage = listOf(usage(94.0)),
            sessions = mapOf("qwen" to session(88)),
        )
        assertEquals(FocusPick.Subject.Budget, choice.subject)
        assertEquals(FocusPick.Rank.ALARM, choice.rank)
    }

    @Test
    fun `a comfortable budget does not take the focus`() {
        val choice = FocusPick.of(
            rows = listOf(row("qwen")),
            usage = listOf(usage(40.0)),
            sessions = mapOf("qwen" to session(50)),
        )
        assertEquals(FocusPick.Subject.Agent("qwen"), choice.subject)
    }

    @Test
    fun `a failed unit outranks a hang`() {
        val choice = FocusPick.of(
            rows = listOf(
                row("grok", state = "failed"),
                row("claude", open = true, callAgo = 1800, signalAgo = 1800),
            ),
        )
        assertEquals(FocusPick.Subject.Agent("grok"), choice.subject)
        assertEquals("Dringend · Unit gescheitert", choice.reason)
    }

    @Test
    fun `an open call without a life sign is a hang`() {
        val stalled = row("claude", open = true, callAgo = 1400, signalAgo = 1400)
        assertTrue(FocusPick.isStalled(stalled))
        val choice = FocusPick.of(rows = listOf(stalled, row("fable")))
        assertEquals(FocusPick.Rank.ALARM, choice.rank)
        assertEquals("Dringend · steht", choice.reason)
    }

    @Test
    fun `long work with a fresh life sign is not a hang`() {
        // The in_progress tick fires every ~30 s. A 40-minute call that keeps
        // ticking is an agent doing something hard, not a dead one.
        val busy = row("codex", open = true, callAgo = 2400, signalAgo = 8)
        assertFalse(FocusPick.isStalled(busy))
        val choice = FocusPick.of(rows = listOf(busy))
        assertEquals(FocusPick.Rank.RESTING, choice.rank)
    }

    @Test
    fun `a closed call is never a hang however old`() {
        assertFalse(FocusPick.isStalled(row("kimi", open = false, callAgo = 99_999, signalAgo = 99_999)))
    }

    @Test
    fun `the operators pin beats even an alarm`() {
        // The screen must not move under the finger. The dock keeps shouting.
        val choice = FocusPick.of(
            rows = listOf(row("claude"), row("qwen")),
            usage = listOf(usage(99.0)),
            sessions = mapOf("qwen" to session(88)),
            pinnedStem = "claude",
        )
        assertEquals(FocusPick.Subject.Agent("claude"), choice.subject)
        assertTrue(choice.pinned)
    }

    @Test
    fun `a pin on an agent that vanished falls back to the ranking`() {
        val choice = FocusPick.of(
            rows = listOf(row("qwen")),
            sessions = mapOf("qwen" to session(30)),
            pinnedStem = "weg",
        )
        assertEquals(FocusPick.Subject.Agent("qwen"), choice.subject)
        assertFalse(choice.pinned)
    }

    @Test
    fun `without any session numbers the card still shows someone real`() {
        val choice = FocusPick.of(rows = listOf(row("hermes", open = true)))
        assertEquals(FocusPick.Subject.Agent("hermes"), choice.subject)
        assertEquals("Im Blick · arbeitet", choice.reason)
    }

    @Test
    fun `an empty fleet says so instead of showing a blank card`() {
        val choice = FocusPick.of(rows = emptyList())
        assertNull(choice.subject)
        assertEquals(FocusPick.Rank.NONE, choice.rank)
        assertEquals("Niemand arbeitet gerade", choice.reason)
    }
}
