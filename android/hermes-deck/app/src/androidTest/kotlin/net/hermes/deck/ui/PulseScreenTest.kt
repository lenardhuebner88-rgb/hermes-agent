package net.hermes.deck.ui

import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Rule
import org.junit.Test
import net.hermes.deck.model.ActionStack
import net.hermes.deck.model.DeckPulse
import net.hermes.deck.model.Freshness

/**
 * The pulse screen's claims, checked on a real device.
 *
 * The model layer already proves what the *data* says; these check what the
 * *screen* says about it — and specifically the three sentences that must never
 * be missing, because each of them stands where a silent, plausible blank
 * would otherwise be: the unmeasurable journal, the stale reading, and the
 * empty action stack.
 */
class PulseScreenTest {

    @get:Rule
    val rule = createComposeRule()

    private fun pulse(json: String) = DeckPulse.fromJson(JSONObject(json), receivedAt = 1_000L)

    private fun render(
        pulse: DeckPulse?,
        freshness: Freshness = Freshness.of(1_000L, 1_000L),
        actions: List<ActionStack.Item> = emptyList(),
        error: String? = null,
        onRefresh: () -> Unit = {},
    ) {
        rule.setContent {
            HermesDeckTheme {
                PulseScreen(
                    pulse = pulse,
                    freshness = freshness,
                    actions = actions,
                    loading = false,
                    error = error,
                    silentAfterSeconds = ActionStack.SILENT_AGENT_SECONDS,
                    onRefresh = onRefresh,
                    onAction = {},
                    onAgent = {},
                    onRun = {},
                )
            }
        }
    }

    private val workingAgent = """
        {"agents":{"error":null,"data":{"activity_window_seconds":1800,"agents":[
          {"stem":"codex","display_name":"Codex","active_state":"active",
           "last_tool_call_seconds_ago":12,
           "activity":{"calls_in_window":9,"looks_open":true,
             "latest":{"seconds_ago":12,"label":"rg -n dispatch_once kanban_db.py","kind":"execute"},
             "recent":[]}}]}},
         "workers":{"error":null,"data":{"workers":[],"checked_at":100}},
         "holds":{"error":null,"data":{"holds":[],"checked_at":100}},
         "events":{"error":null,"data":{"events":[]}}}
    """.trimIndent()

    @Test
    fun anAgentInAToolCallShowsTheCommandItself() {
        render(pulse(workingAgent))
        rule.onNodeWithText("Codex").assertIsDisplayed()
        // The whole point of the feature: not "9 Tool-Calls", but the command.
        rule.onNodeWithText("rg -n dispatch_once kanban_db.py").assertIsDisplayed()
        rule.onNodeWithText("Terminal").assertIsDisplayed()
    }

    @Test
    fun anEmptyActionStackSaysSoInsteadOfShowingNothing() {
        render(pulse(workingAgent))
        rule.onNodeWithText("Nichts liegt an — alles läuft oder wartet auf niemanden.")
            .assertIsDisplayed()
    }

    @Test
    fun aStaleReadingIsLabelledWithItsAge() {
        render(pulse(workingAgent), freshness = Freshness.of(1_000L, 1_000L + 240_000L))
        rule.onNodeWithText("Stand vor 4 min").assertIsDisplayed()
    }

    @Test
    fun anUnreadableJournalIsNamed() {
        render(
            pulse(
                """
                {"agents":{"error":null,"data":{"activity_error":"journal_unavailable","agents":[
                   {"stem":"kimi","display_name":"Kimi","active_state":"active","activity":null}]}},
                 "workers":{"error":null,"data":{"workers":[],"checked_at":1}},
                 "holds":{"error":null,"data":{"holds":[],"checked_at":1}},
                 "events":{"error":null,"data":{"events":[]}}}
                """.trimIndent()
            )
        )
        rule.onNodeWithText("Aktivität nicht messbar — das Journal liess sich nicht lesen.")
            .assertIsDisplayed()
        // And the agent is still listed — as unmeasurable, not as idle.
        rule.onNodeWithText("nicht messbar").assertIsDisplayed()
    }

    @Test
    fun aJournalFormatChangeGetsItsOwnWarning() {
        render(
            pulse(
                """
                {"agents":{"error":null,"data":{
                   "activity_diagnostics":{"lines_seen":400,"lines_understood":0,"suspicious":true},
                   "agents":[]}},
                 "workers":{"error":null,"data":{"workers":[],"checked_at":1}},
                 "holds":{"error":null,"data":{"holds":[],"checked_at":1}},
                 "events":{"error":null,"data":{"events":[]}}}
                """.trimIndent()
            )
        )
        rule.onNodeWithText(
            "Das Journal liefert Zeilen, die der Server nicht mehr versteht — " +
                "die Aktivität unten ist unvollständig.",
        ).assertIsDisplayed()
    }

    @Test
    fun aBrokenSectionIsNamedRatherThanRenderedEmpty() {
        render(
            pulse(
                """
                {"agents":{"error":null,"data":{"agents":[]}},
                 "workers":{"error":"RuntimeError: board locked","data":null},
                 "holds":{"error":null,"data":{"holds":[],"checked_at":1}},
                 "events":{"error":null,"data":{"events":[]}}}
                """.trimIndent()
            )
        )
        rule.onNodeWithText("Nicht abrufbar: Läufe").assertIsDisplayed()
    }

    @Test
    fun anOverrunningRunIsMarkedOnTheCard() {
        render(
            pulse(
                """
                {"agents":{"error":null,"data":{"agents":[]}},
                 "workers":{"error":null,"data":{"checked_at":1000,"workers":[
                   {"run_id":3,"task_id":"T-3","task_title":"Nachtlauf","profile":"coder",
                    "started_at":100,"max_runtime_seconds":300,"liveness_state":"alive",
                    "last_heartbeat_note":"baut die Karte"}]}},
                 "holds":{"error":null,"data":{"holds":[],"checked_at":1}},
                 "events":{"error":null,"data":{"events":[]}}}
                """.trimIndent()
            )
        )
        rule.onNodeWithText("Nachtlauf").assertIsDisplayed()
        rule.onNodeWithText("über Budget").assertIsDisplayed()
        rule.onNodeWithText("baut die Karte").assertIsDisplayed()
    }

    @Test
    fun theHeaderIsTheRefreshGesture() {
        var refreshed = 0
        render(pulse(workingAgent), onRefresh = { refreshed++ })
        rule.onNodeWithText("PULS").performClick()
        assertEquals(1, refreshed)
    }
}
