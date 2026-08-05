package net.hermes.deck.model

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/** The chain exists so "searching" and "stuck on a gate" stop looking alike. */
class TurnChainTest {

    private fun pulse(vararg kinds: ToolCall.Kind, open: Boolean = false, window: Int = kinds.size) =
        AgentPulse(
            stem = "codex",
            callsInWindow = window,
            latest = ToolCall(3, kinds.first().name, kinds.first()),
            looksOpen = open,
            lastSignalSecondsAgo = 2,
            // The wire delivers newest-first.
            recent = kinds.mapIndexed { i, k -> ToolCall(i * 10, k.name, k) },
        )

    @Test
    fun `the chain reads left to right in time`() {
        val chain = TurnChain.of(
            pulse(ToolCall.Kind.EXECUTE, ToolCall.Kind.EDIT, ToolCall.Kind.READ)
        )
        // recent = [T(newest), Ä, L(oldest)] → chain shows oldest first
        assertEquals(listOf("L", "Ä", "T"), chain.links.map { it.glyph })
    }

    @Test
    fun `only the newest link can be open`() {
        val chain = TurnChain.of(
            pulse(ToolCall.Kind.EXECUTE, ToolCall.Kind.READ, open = true)
        )
        assertTrue(chain.links.last().open)
        assertFalse(chain.links.first().open)
    }

    @Test
    fun `a closed turn has no open link`() {
        val chain = TurnChain.of(pulse(ToolCall.Kind.EXECUTE, ToolCall.Kind.READ, open = false))
        assertTrue(chain.links.none { it.open })
    }

    @Test
    fun `the chain is capped and reports the remainder`() {
        val kinds = Array(20) { ToolCall.Kind.EXECUTE }
        val chain = TurnChain.of(pulse(*kinds, window = 64))
        assertEquals(TurnChain.LENGTH, chain.links.size)
        assertEquals(64 - TurnChain.LENGTH, chain.more)
    }

    @Test
    fun `a window smaller than the chain reports no remainder`() {
        val chain = TurnChain.of(pulse(ToolCall.Kind.READ, ToolCall.Kind.READ, window = 2))
        assertEquals(0, chain.more)
    }

    @Test
    fun `the summary names what dominated the turn`() {
        val chain = TurnChain.of(
            pulse(
                ToolCall.Kind.READ, ToolCall.Kind.READ, ToolCall.Kind.READ,
                ToolCall.Kind.READ, ToolCall.Kind.EDIT,
            )
        )
        assertTrue(chain.summary.startsWith("4× gelesen"))
        assertTrue(chain.summary.contains("1× geändert"))
    }

    @Test
    fun `every kind has its own glyph`() {
        val glyphs = ToolCall.Kind.entries.map { TurnChain.glyphFor(it) }
        assertEquals(glyphs.size, glyphs.toSet().size)
    }

    @Test
    fun `no activity yields an empty chain, not a fake one`() {
        assertTrue(TurnChain.of(null).isEmpty)
        assertTrue(TurnChain.of(AgentPulse("x", 0, null, false, null, emptyList())).isEmpty)
    }
}
