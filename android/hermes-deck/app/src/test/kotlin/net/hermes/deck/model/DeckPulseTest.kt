package net.hermes.deck.model

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Wire tests for `/api/deck/pulse`.
 *
 * `deck-pulse-live.json` is a **real** response, captured 2026-08-05 from the
 * running dashboard against the live journal and the live kanban board. Every
 * previous wire bug in this app (five wrong field names in the model-control
 * payload, all at once) survived hand-written JSON and died on the first real
 * one, so the real one goes in the repo.
 */
class DeckPulseTest {

    private fun live(): JSONObject =
        JSONObject(
            checkNotNull(javaClass.classLoader?.getResourceAsStream("deck-pulse-live.json")) {
                "deck-pulse-live.json missing from test resources"
            }.bufferedReader().readText()
        )

    private fun parsed() = DeckPulse.fromJson(live(), receivedAt = 1_000_000L)

    @Test
    fun `every section of the live payload parses`() {
        val pulse = parsed()
        assertNull("agents", pulse.agents.error)
        assertNull("workers", pulse.workers.error)
        assertNull("holds", pulse.holds.error)
        assertNull("events", pulse.events.error)
        assertTrue(pulse.brokenSections.isEmpty())
    }

    @Test
    fun `agents arrive with their tool-call timelines attached`() {
        val rows = parsed().agentRows
        assertTrue("no agents in the live payload", rows.isNotEmpty())
        val withActivity = rows.filter { it.pulse?.hasActivity == true }
        assertTrue("no agent carried a tool call", withActivity.isNotEmpty())
        val call = withActivity.first().pulse!!.latest!!
        assertTrue("label was empty", call.label.isNotBlank())
        assertFalse("ANSI leaked to the client", call.label.contains('\u001B'))
    }

    @Test
    fun `the parser diagnostic travels to the client`() {
        // The one failure this feature dies of silently is a journal format
        // change: lines arrive, none parse, every agent renders as quiet. The
        // client must be able to see that, so the flag has to survive the wire.
        val block = parsed().agents.data!!
        assertFalse(block.parserSuspicious)
        assertTrue(block.activityWindowSeconds > 0)
        assertTrue(block.heartbeatWindowSeconds > 0)
    }

    @Test
    fun `an unreadable journal yields a null pulse, not an empty one`() {
        val json = JSONObject(
            """
            {"board":null,
             "agents":{"error":null,"as_of":"x","data":{
               "agents":[{"stem":"claude","display_name":"Claude","active_state":"active","activity":null}],
               "activity_error":"journal_unavailable","activity_window_seconds":1800}},
             "workers":{"error":null,"data":{"workers":[],"checked_at":100}},
             "holds":{"error":null,"data":{"holds":[],"checked_at":100}},
             "events":{"error":null,"data":{"events":[]}}}
            """.trimIndent()
        )
        val pulse = DeckPulse.fromJson(json, receivedAt = 1L)
        val row = pulse.agentRows.single()
        // Null, not an empty AgentPulse: "could not measure" must never render
        // the same way as "measured, and it did nothing".
        assertNull(row.pulse)
        assertEquals("journal_unavailable", pulse.agents.data!!.activityError)
    }

    @Test
    fun `a broken section does not take the others with it`() {
        val json = JSONObject(
            """
            {"board":"default",
             "agents":{"error":"RuntimeError: boom","data":null},
             "workers":{"error":null,"data":{"workers":[],"checked_at":100}},
             "holds":{"error":null,"data":{"holds":[],"checked_at":100}},
             "events":{"error":null,"data":{"events":[]}}}
            """.trimIndent()
        )
        val pulse = DeckPulse.fromJson(json, receivedAt = 1L)
        assertTrue(pulse.agents.isBroken)
        assertFalse(pulse.workers.isBroken)
        assertEquals(listOf("Agenten"), pulse.brokenSections)
    }

    @Test
    fun `a missing section is reported, not silently empty`() {
        val pulse = DeckPulse.fromJson(JSONObject("""{"board":"default"}"""), receivedAt = 1L)
        assertEquals(
            listOf("Agenten", "Läufe", "Blockaden", "Ereignisse"),
            pulse.brokenSections,
        )
    }

    @Test
    fun `worker durations are measured against the server clock`() {
        val json = JSONObject(
            """
            {"workers":{"error":null,"data":{"checked_at":1000,"workers":[
              {"run_id":7,"task_id":"T-1","task_title":"Puls bauen","profile":"coder",
               "started_at":400,"max_runtime_seconds":300,"liveness_state":"alive",
               "last_heartbeat_note":"baut","last_heartbeat_note_at":900,
               "input_tokens":10,"output_tokens":5}]}}}
            """.trimIndent()
        )
        val run = DeckPulse.fromJson(json, receivedAt = 999_999_999L).workerRuns.single()
        // 600 s of runtime against a 300 s budget — and the phone's own clock,
        // which is nowhere near 1000, must not enter into it.
        assertEquals(600, run.runtimeSeconds)
        assertTrue(run.isOverBudget)
        assertEquals(100, run.noteAgeSeconds)
        assertEquals(15, run.totalTokens)
    }

    @Test
    fun `an uncapped run reports no progress rather than a made-up one`() {
        val json = JSONObject(
            """
            {"workers":{"error":null,"data":{"checked_at":1000,"workers":[
              {"run_id":8,"task_id":"T-2","task_title":"offen","profile":"claude-cli",
               "started_at":400,"max_runtime_seconds":null,"run_progress":null,
               "liveness_state":"alive"}]}}}
            """.trimIndent()
        )
        val run = DeckPulse.fromJson(json, receivedAt = 1L).workerRuns.single()
        assertFalse(run.isOverBudget)
        assertNull(run.progressFraction)
        assertNull(run.totalTokens)
    }

    @Test
    fun `holds translate their bucket into something a phone can read`() {
        val json = JSONObject(
            """
            {"holds":{"error":null,"data":{"checked_at":50,"holds":[
              {"task_id":"T-9","title":"Nachtlauf","bucket":"budget_held","reason":"Tageslimit"},
              {"task_id":"T-8","title":"Sync","bucket":"repo_serialized","holder":"kimi"}]}}}
            """.trimIndent()
        )
        val holds = DeckPulse.fromJson(json, receivedAt = 1L).heldTasks
        assertEquals("Budget gesperrt", holds[0].label)
        assertTrue(holds[0].needsOperator)
        assertEquals("Repo belegt", holds[1].label)
        // A repo lock clears itself when the holder finishes — it is information,
        // not a task for the operator, and must not enter the action stack.
        assertFalse(holds[1].needsOperator)
        assertEquals("gehalten von kimi", holds[1].detail)
    }

    @Test
    fun `tool call kinds map to the families the log actually emits`() {
        assertEquals(ToolCall.Kind.EXECUTE, ToolCall.Kind.fromWire("execute"))
        assertEquals(ToolCall.Kind.EDIT, ToolCall.Kind.fromWire("edit"))
        assertEquals(ToolCall.Kind.SEARCH, ToolCall.Kind.fromWire("search"))
        // A multi-line command logs no kind at all; that is a state, not a bug.
        assertEquals(ToolCall.Kind.UNKNOWN, ToolCall.Kind.fromWire(""))
        assertEquals(ToolCall.Kind.UNKNOWN, ToolCall.Kind.fromWire("something-new"))
    }

    @Test
    fun `looks_open is reported as a word, never as a running duration`() {
        val open = AgentPulse.fromJson(
            "codex",
            JSONObject(
                """{"calls_in_window":3,"looks_open":true,"last_signal_seconds_ago":8,
                    "latest":{"seconds_ago":40,"label":"rg -n dispatch_once","kind":"execute"},
                    "recent":[]}"""
            ),
        )
        assertNotNull(open)
        assertEquals("läuft", open!!.statusWord)
        assertEquals(40, open.latest!!.secondsAgo)

        val closed = AgentPulse.fromJson(
            "kimi",
            JSONObject("""{"calls_in_window":1,"looks_open":false,"latest":{"seconds_ago":90,"label":"Edit","kind":"edit"},"recent":[]}"""),
        )
        assertEquals("zuletzt", closed!!.statusWord)
    }

    // --- session facts, end to end ---------------------------------------

    @Test
    fun `the live payload carries session facts for every agent that keeps a book`() {
        val rows = parsed().agentRows
        val measured = rows.filter { it.session?.hasMeasurement == true }
        // Seven of eight stems keep a session book; only `hermes` does not.
        assertTrue("nur ${measured.size} Agenten mit Messung", measured.size >= 7)
        measured.forEach { row ->
            val s = row.session!!
            assertTrue("${row.agent.stem}: keine Token", (s.promptTokens ?: 0) > 0)
            assertNotNull("${row.agent.stem}: keine Quelle", s.source)
            assertTrue(
                "${row.agent.stem}: Quelle NONE trotz Messung",
                s.source != SessionFacts.Source.NONE,
            )
        }
    }

    @Test
    fun `kimi and grok are measured exactly like the rest`() {
        // The operator asked for these two to look no different from the others.
        val rows = parsed().agentRows.associateBy { it.agent.stem }
        listOf("kimi" to SessionFacts.Source.KIMI, "grok" to SessionFacts.Source.GROK)
            .forEach { (stem, source) ->
                val session = rows[stem]?.session
                assertNotNull("$stem fehlt im Payload", session)
                assertEquals(source, session!!.source)
                assertTrue("$stem ohne Token", (session.promptTokens ?: 0) > 0)
                assertNotNull("$stem ohne Fenster", session.contextWindow)
                assertNotNull("$stem ohne Prozent", session.percent)
                assertNull("$stem darf keinen Fehlgrund tragen", session.absenceReason)
            }
    }

    @Test
    fun `an agent without a session book says so instead of showing zero`() {
        val hermes = parsed().agentRows.first { it.agent.stem == "hermes" }.session
        assertNotNull(hermes)
        assertEquals(SessionFacts.Source.NONE, hermes!!.source)
        assertNull("kein erfundener Prozentwert", hermes.percent)
        assertNull("kein erfundener Tokenwert", hermes.promptTokens)
        assertEquals("führt kein Sitzungsbuch", hermes.absenceReason)
    }

    @Test
    fun `steerability travels per agent and is never guessed`() {
        val rows = parsed().agentRows.associateBy { it.agent.stem }
        // Measured live: the ACP handshake reports true for the claude and codex
        // families, false for the rest. A missing line must stay null.
        assertEquals(true, rows["claude"]?.session?.steerable)
        assertEquals(true, rows["codex"]?.session?.steerable)
        assertEquals(false, rows["kimi"]?.session?.steerable)
        assertEquals(false, rows["qwen"]?.session?.steerable)
    }
}
