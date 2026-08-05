package net.hermes.deck.model

import net.hermes.deck.net.HermesPayloads
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Test

/**
 * Pinned against a real `/api/buzz/agents` response recorded on 2026-08-05.
 *
 * The three agents in it are the three states that matter and were all live at
 * the same moment: qwen working, claude worked-this-hour, terra silent — while
 * `active_state` said `active` for every one of them. That coincidence is the
 * whole reason this feature exists, so the fixture keeps it verbatim.
 */
class AgentActivityTest {

    private val recorded = """
    {
      "agents": [
        {
          "stem": "claude", "display_name": "Claude", "model": "opus[1m]",
          "agent_command": "claude-agent-acp", "active_state": "active",
          "last_start": "Wed 2026-08-05 04:02:46 CEST",
          "tool_calls_window": 144, "tool_calls_recent": 0,
          "last_tool_call_at": "2026-08-05T10:23:23+0200",
          "last_tool_call_seconds_ago": 523, "working": false
        },
        {
          "stem": "qwen", "display_name": "Qwen", "model": "qwen-route:v1:WRuqteCqSoPIdAbL",
          "agent_command": "qwen", "active_state": "active",
          "last_start": "Wed 2026-08-05 04:02:52 CEST",
          "tool_calls_window": 45, "tool_calls_recent": 3,
          "last_tool_call_at": "2026-08-05T10:31:58+0200",
          "last_tool_call_seconds_ago": 8, "working": true
        },
        {
          "stem": "terra", "display_name": "Terra", "model": "gpt-5.6-terra",
          "agent_command": "codex-acp", "active_state": "active",
          "last_start": "Wed 2026-08-05 04:02:55 CEST",
          "tool_calls_window": 0, "tool_calls_recent": 0,
          "last_tool_call_at": null, "last_tool_call_seconds_ago": null,
          "working": false
        }
      ],
      "heartbeat_window_seconds": 3600,
      "heartbeat_recent_seconds": 300,
      "heartbeat_error": null
    }
    """.trimIndent()

    private fun snapshot() = HermesPayloads.agentsSnapshot(JSONObject(recorded))

    private fun readingFor(stem: String): AgentActivity.Reading {
        val snap = snapshot()
        val agent = snap.agents.first { it.stem == stem }
        return AgentActivity.of(agent, snap.windowSeconds, snap.recentSeconds)
    }

    @Test
    fun `all three agents report active while only one is working`() {
        val snap = snapshot()

        assertEquals(listOf("active", "active", "active"), snap.agents.map { it.activeState })
        assertEquals(
            listOf(AgentActivity.State.RECENT, AgentActivity.State.WORKING, AgentActivity.State.QUIET),
            snap.agents.map { AgentActivity.of(it, snap.windowSeconds, snap.recentSeconds).state },
        )
    }

    @Test
    fun `working agent is named as working and quotes the recent window`() {
        val reading = readingFor("qwen")

        assertEquals(AgentActivity.State.WORKING, reading.state)
        assertEquals("arbeitet gerade · 3 Tool-Calls in 5 min", reading.label)
    }

    @Test
    fun `agent that worked this hour is not sold as working now`() {
        val reading = readingFor("claude")

        assertEquals(AgentActivity.State.RECENT, reading.state)
        assertEquals("zuletzt vor 8 min · 144 Tool-Calls in 1 Stunde", reading.label)
    }

    @Test
    fun `silent agent says nothing happened in the window, not never`() {
        val reading = readingFor("terra")

        assertEquals(AgentActivity.State.QUIET, reading.state)
        // The query is bounded, so "nie" would be a claim the data cannot make.
        assertEquals("keine Aktivität in 1 Stunde", reading.label)
    }

    @Test
    fun `null heartbeat is unknown, never quiet`() {
        val agent = agent(working = null, window = null, recent = null, ago = null)

        val reading = AgentActivity.of(agent, 3600, 300)

        assertEquals(AgentActivity.State.UNKNOWN, reading.state)
    }

    @Test
    fun `a failed unit outranks any heartbeat number`() {
        val agent = agent(working = true, window = 9, recent = 9, ago = 1, state = "failed")

        assertEquals(AgentActivity.State.FAILED, AgentActivity.of(agent, 3600, 300).state)
    }

    @Test
    fun `missing heartbeat keys parse as null, not as zero`() {
        // An older dashboard omits the fields entirely. Reading them as 0 would
        // paint every agent as provably idle on the strength of nothing.
        val legacy = JSONObject(
            """{"stem":"claude","display_name":"Claude","model":"opus[1m]",
               "agent_command":"claude-agent-acp","active_state":"active","last_start":"x"}"""
        )

        val agent = BuzzAgent.fromJson(legacy)

        assertNull(agent.working)
        assertNull(agent.toolCallsWindow)
        assertEquals(AgentActivity.State.UNKNOWN, AgentActivity.of(agent, 3600, 300).state)
    }

    @Test
    fun `explicit json null is also null, not zero`() {
        val agent = snapshot().agents.first { it.stem == "terra" }

        assertNull(agent.lastToolCallSecondsAgo)
        assertEquals(0, agent.toolCallsWindow)
    }

    @Test
    fun `heartbeat failure produces a sentence instead of silence`() {
        val json = JSONObject(
            """{"agents":[],"heartbeat_window_seconds":3600,
               "heartbeat_recent_seconds":300,"heartbeat_error":"journal_unavailable"}"""
        )

        val snap = HermesPayloads.agentsSnapshot(json)

        assertNotNull(snap.heartbeatError)
        assertEquals(
            "Aktivität nicht messbar — das Journal liess sich nicht lesen.",
            snap.heartbeatError,
        )
    }

    @Test
    fun `no heartbeat error means no banner`() {
        assertNull(snapshot().heartbeatError)
    }

    @Test
    fun `labels follow the server's windows instead of hardcoding them`() {
        val agent = agent(working = true, window = 5, recent = 5, ago = 2)

        // A server retuned to a 15-minute recent window must change the text.
        assertEquals(
            "arbeitet gerade · 5 Tool-Calls in 15 min",
            AgentActivity.of(agent, 7200, 900).label,
        )
    }

    @Test
    fun `singular tool call is not pluralised`() {
        val agent = agent(working = true, window = 1, recent = 1, ago = 5)

        assertEquals("arbeitet gerade · 1 Tool-Call in 5 min", AgentActivity.of(agent, 3600, 300).label)
    }

    @Test
    fun `relative time reads as a human would say it`() {
        assertEquals("gerade eben", AgentActivity.ago(0))
        assertEquals("gerade eben", AgentActivity.ago(59))
        assertEquals("vor 1 min", AgentActivity.ago(60))
        assertEquals("vor 8 min", AgentActivity.ago(523))
        assertEquals("vor 1 h", AgentActivity.ago(3600))
        assertEquals("vor 1 d", AgentActivity.ago(90_000))
    }

    @Test
    fun `window spans read naturally`() {
        assertEquals("5 min", AgentActivity.span(300))
        assertEquals("1 Stunde", AgentActivity.span(3600))
        assertEquals("2 Stunden", AgentActivity.span(7200))
    }

    private fun agent(
        working: Boolean?,
        window: Int?,
        recent: Int?,
        ago: Int?,
        state: String = "active",
    ) = BuzzAgent(
        stem = "test",
        displayName = "Test",
        model = "m",
        agentCommand = "c",
        activeState = state,
        lastStart = "x",
        toolCallsWindow = window,
        toolCallsRecent = recent,
        lastToolCallSecondsAgo = ago,
        working = working,
    )
}
