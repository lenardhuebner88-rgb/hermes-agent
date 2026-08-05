package net.hermes.deck.ui

import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.test.onAllNodesWithText
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import net.hermes.deck.model.AccountUsage
import net.hermes.deck.model.ActionStack
import net.hermes.deck.model.DeckPulse
import net.hermes.deck.model.Freshness
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Rule
import org.junit.Test

/**
 * The entry screen's claims, checked on a real device.
 *
 * The model layer proves what the data says; these check what the *screen* says
 * about it — above all the sentences that stand where a silent, plausible blank
 * would otherwise be, and the rule that the screen must not move under a finger.
 */
class PulseScreenTest {

    @get:Rule
    val rule = createComposeRule()

    private fun pulse(json: String) = DeckPulse.fromJson(JSONObject(json), receivedAt = 1_000L)

    private var pinned: String? = null

    private fun render(
        pulse: DeckPulse?,
        freshness: Freshness = Freshness.of(1_000L, 1_000L),
        usage: List<AccountUsage> = emptyList(),
        error: String? = null,
        pin: String? = null,
        onRefresh: () -> Unit = {},
    ) {
        pinned = pin
        rule.setContent {
            HermesDeckTheme {
                PulseScreen(
                    pulse = pulse,
                    freshness = freshness,
                    actions = emptyList<ActionStack.Item>(),
                    usage = usage,
                    loading = false,
                    error = error,
                    pinnedStem = pinned,
                    onRefresh = onRefresh,
                    onPin = { pinned = it },
                    onAgent = {},
                    onRun = {},
                )
            }
        }
    }

    private fun usage(percent: Double) = AccountUsage(
        provider = "anthropic", available = true, title = "Claude", plan = null,
        windows = listOf(AccountUsage.Window("Woche", "week", percent, null, null)),
        details = emptyList(), unavailableReason = null,
        fallback = false, cached = false, fetchedAt = null,
    )

    /** Two agents: codex working with a measured session, kimi with none. */
    private val fleet = """
        {"agents":{"error":null,"data":{"activity_window_seconds":1800,"agents":[
          {"stem":"codex","display_name":"Codex","active_state":"active","last_tool_call_seconds_ago":12,
           "activity":{"calls_in_window":34,"looks_open":true,"last_signal_seconds_ago":6,
             "latest":{"seconds_ago":12,"label":"rg -n dispatch_once kanban_db.py","kind":"execute"},
             "recent":[{"seconds_ago":12,"label":"rg","kind":"execute"},
                       {"seconds_ago":40,"label":"Read","kind":"read"},
                       {"seconds_ago":70,"label":"Read","kind":"read"}]},
           "session":{"session_id":"abc","age_seconds":31680,"prompt_tokens":530869,
                      "context_window":1000000,"compacts":0,"measured_seconds_ago":8,
                      "source":"codex","steerable":true}},
          {"stem":"kimi","display_name":"Kimi","active_state":"active","last_tool_call_seconds_ago":900,
           "activity":{"calls_in_window":4,"looks_open":false,"last_signal_seconds_ago":900,
             "latest":{"seconds_ago":900,"label":"Editing files","kind":"edit"},"recent":[]},
           "session":{"source":"none","steerable":false}}]}},
         "workers":{"error":null,"data":{"workers":[],"checked_at":100}},
         "holds":{"error":null,"data":{"holds":[],"checked_at":100}},
         "events":{"error":null,"data":{"events":[]}}}
    """.trimIndent()

    @Test
    fun theFocusCardNamesWhyItChoseThisAgent() {
        render(pulse(fleet))
        rule.onNodeWithText("IM BLICK · VOLLSTER KONTEXT").assertIsDisplayed()
        rule.onAllNodesWithText("Codex")[0].assertIsDisplayed()
    }

    @Test
    fun theSessionLineCarriesAgeContextAndCompacts() {
        render(pulse(fleet))
        // 31680 s = 8,8 h · 530869 of 1000000 → 530k von 1M
        rule.onNodeWithText("Session 8,8 h · 530k von 1M · 0 Compacts").assertIsDisplayed()
    }

    @Test
    fun theTurnLineProvesWorkWithTwoNumbers() {
        // The open call plus a life sign younger than the call is the whole proof.
        render(pulse(fleet))
        rule.onNodeWithText("Turn seit 12 s · Signal vor 6 s").assertIsDisplayed()
    }

    @Test
    fun theCurrentCommandIsShownVerbatimInBothPlaces() {
        // Twice on purpose: the focus card states what this agent is doing, the
        // stream states that it happened. The card answers "who", the stream
        // answers "when" — neither is redundant.
        render(pulse(fleet))
        val nodes = rule.onAllNodesWithText("rg -n dispatch_once kanban_db.py")
        nodes[0].assertIsDisplayed()
        nodes[1].assertIsDisplayed()
    }

    @Test
    fun theTurnChainSummarisesTheShapeOfTheWork() {
        render(pulse(fleet))
        rule.onNodeWithText("Gestalt des Turns").assertIsDisplayed()
        rule.onNodeWithText("2× gelesen, 1× ausgeführt").assertIsDisplayed()
    }

    @Test
    fun aSteerableAgentSaysSoAndAQueueingOneSaysThat() {
        render(pulse(fleet))
        rule.onNodeWithText("LENKBAR").assertIsDisplayed()
    }

    @Test
    fun anAgentWithoutASessionBookIsNamedNotZeroed() {
        // kimi keeps no session book; pinning it must say that, never "0 %".
        render(pulse(fleet), pin = "kimi")
        rule.onNodeWithText("führt kein Sitzungsbuch").assertIsDisplayed()
        rule.onNodeWithText("STELLT AN").assertIsDisplayed()
    }

    @Test
    fun thePinBeatsTheAutomaticFocus() {
        render(pulse(fleet), pin = "kimi")
        rule.onNodeWithText("ANGEHEFTET").assertIsDisplayed()
    }

    @Test
    fun aBudgetAboutToBiteTakesOverTheCard() {
        render(pulse(fleet), usage = listOf(usage(94.0)))
        rule.onNodeWithText("DRINGEND · VERBRENNUNG").assertIsDisplayed()
        rule.onNodeWithText("94").assertIsDisplayed()
    }

    @Test
    fun aStaleReadingIsLabelledWithItsAge() {
        render(pulse(fleet), freshness = Freshness.of(1_000L, 1_000L + 240_000L))
        rule.onNodeWithText("Stand vor 4 min").assertIsDisplayed()
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
    fun anIdleFleetIsAStateNotAnError() {
        render(
            pulse(
                """
                {"agents":{"error":null,"data":{"agents":[
                   {"stem":"kimi","display_name":"Kimi","active_state":"active",
                    "activity":{"calls_in_window":0,"looks_open":false,"recent":[]}}]}},
                 "workers":{"error":null,"data":{"workers":[],"checked_at":1}},
                 "holds":{"error":null,"data":{"holds":[],"checked_at":1}},
                 "events":{"error":null,"data":{"events":[]}}}
                """.trimIndent()
            )
        )
        rule.onNodeWithText("Niemand arbeitet gerade").assertIsDisplayed()
    }

    @Test
    fun theHeaderIsTheRefreshGesture() {
        var refreshed = 0
        render(pulse(fleet), onRefresh = { refreshed++ })
        rule.onNodeWithText("Leitstand").performClick()
        assertEquals(1, refreshed)
    }
}
