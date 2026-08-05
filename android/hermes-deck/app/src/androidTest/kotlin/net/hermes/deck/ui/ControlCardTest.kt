package net.hermes.deck.ui

import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.onAllNodesWithText
import androidx.compose.ui.test.performClick
import androidx.test.ext.junit.runners.AndroidJUnit4
import net.hermes.deck.model.AccountUsage
import net.hermes.deck.model.ControlSummary
import net.hermes.deck.model.ThreadActivity
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith

/**
 * The Compose layer's first mechanical gate.
 *
 * Until this file existed, `android/` had no lint and no instrumented tests at
 * all: four of the six defects in the 2026-08-05 session lived in the
 * Compose/Activity layer and were found only by looking at a running emulator.
 * Everything here therefore asserts what actually reaches the screen, not what
 * the model computed — the two had never been checked against each other.
 */
@RunWith(AndroidJUnit4::class)
class ControlCardTest {

    @get:Rule
    val compose = createComposeRule()

    private fun summary(
        working: List<String> = emptyList(),
        idle: Int = 0,
        failed: Int = 0,
        unknown: Int = 0,
        activeThreads: Int = 0,
        decisions: Int = 0,
        overdue: Int = 0,
        dueToday: Int = 0,
        mentions: Int = 0,
        capacity: ControlSummary.Capacity? = null,
        pending: Int = 0,
        lastSyncAt: Long = 1000,
        failure: String? = null,
        syncing: Boolean = false,
    ) = ControlSummary(
        pulse = ControlSummary.Pulse(working, idle, failed, unknown, activeThreads),
        waiting = ControlSummary.Waiting(decisions, overdue, dueToday, mentions),
        capacity = capacity,
        delivery = ControlSummary.Delivery(pending, lastSyncAt, failure, syncing),
    )

    private fun show(summary: ControlSummary, onOverdue: () -> Unit = {}) {
        compose.setContent {
            HermesDeckTheme {
                ControlCard(
                    summary = summary,
                    onShowDecisions = {},
                    onShowOverdue = onOverdue,
                    onShowToday = {},
                    onSync = {},
                )
            }
        }
    }

    @Test
    fun theQuietHalfIsOnScreen_notJustTheWorkingCount() {
        // The measured case: eight units "active", three actually working. If
        // only the headline rendered, the card would repeat the very lie it was
        // built to replace.
        show(summary(working = listOf("Claude", "Qwen", "Hermes"), idle = 5))

        compose.onNodeWithText("3 arbeiten").assertIsDisplayed()
        compose.onNodeWithText("Claude, Qwen, Hermes · 5 laufen leer").assertIsDisplayed()
    }

    @Test
    fun nobodyWorkingSaysSoRatherThanRenderingNothing() {
        show(summary(idle = 8))

        compose.onNodeWithText("Niemand arbeitet").assertIsDisplayed()
    }

    @Test
    fun anUnreadableHeartbeatShowsAsUnknown() {
        show(summary(unknown = 8))

        compose.onNodeWithText("Aktivität unbekannt").assertIsDisplayed()
    }

    @Test
    fun activeThreadCountIsShownWhenThreadsAreActive() {
        show(summary(working = listOf("Claude"), activeThreads = 4))

        compose.onNodeWithText("4 aktiv").assertIsDisplayed()
    }

    @Test
    fun aZeroThreadCountDoesNotRenderAtAll() {
        // One `setContent` per test: the rule throws "has already set content"
        // on a second call, so the two halves of this claim cannot share a test.
        show(summary(working = listOf("Claude"), activeThreads = 0))

        compose.onAllNodesWithText("0 aktiv").fetchSemanticsNodes().let {
            assertTrue("Ein Null-Zähler darf gar nicht rendern", it.isEmpty())
        }
    }

    @Test
    fun waitingChipsCarryTheirCountsAndAreTappable() {
        var tapped = 0
        show(summary(decisions = 2, overdue = 3, dueToday = 1), onOverdue = { tapped++ })

        compose.onNodeWithText("Freigaben").assertIsDisplayed()
        compose.onNodeWithText("Überfällig").assertIsDisplayed()
        compose.onNodeWithText("3").assertIsDisplayed()

        compose.onNodeWithText("Überfällig").performClick()
        assertEquals(1, tapped)
    }

    @Test
    fun theWaitingBandDisappearsEntirelyWhenNothingWaits() {
        // Height is a constraint here: the screen has twice pushed its own
        // content below the fold, so an empty band must render nothing at all
        // rather than an empty heading.
        show(summary(working = listOf("Claude")))

        compose.onAllNodesWithText("Wartet auf dich").fetchSemanticsNodes().let {
            assertTrue("Leeres Band darf keine Überschrift zeigen", it.isEmpty())
        }
    }

    @Test
    fun theTightestBudgetIsNamedAndMarkedWhenStale() {
        show(summary(capacity = ControlSummary.Capacity("Kimi", 74.0, stale = true, others = 2)))

        compose.onNodeWithText("Knappstes Budget · Kimi").assertIsDisplayed()
        compose.onNodeWithText("74 %").assertIsDisplayed()
        // A cached value shown as a fresh one is the lie this whole session was
        // about avoiding.
        compose.onNodeWithText("2 weitere · Zwischenspeicher").assertIsDisplayed()
    }

    @Test
    fun aSyncFailureStaysOnScreenAndOutranksPending() {
        show(summary(pending = 4, failure = "Relay nicht erreichbar"))

        compose.onNodeWithText("Abgleich fehlgeschlagen: Relay nicht erreichbar").assertIsDisplayed()
        compose.onAllNodesWithText("4 wartet auf Verbindung zu Buzz").fetchSemanticsNodes().let {
            assertTrue("Der Fehler muss den Rückstau verdrängen", it.isEmpty())
        }
    }

    @Test
    fun pendingIsShownWhenThereIsNoFailure() {
        show(summary(pending = 4))

        compose.onNodeWithText("4 wartet auf Verbindung zu Buzz").assertIsDisplayed()
    }

    @Test
    fun anActiveThreadRowShowsWhoSpokeWhereAndWhat() {
        val entry = ThreadActivity.Entry(
            taskId = "t1",
            title = "Deck bauen",
            channelId = "c",
            channelName = "agent-lab",
            replyCount = 3,
            lastAt = System.currentTimeMillis() / 1000 - 120,
            lastAuthorPubkey = "b".repeat(64),
            lastAuthorLabel = "Claude",
            lastText = "Bin dran, Gate läuft.",
            awaitingMe = true,
        )
        var opened = false
        compose.setContent { HermesDeckTheme { ActiveThreadRow(entry) { opened = true } } }

        compose.onNodeWithText("Claude").assertIsDisplayed()
        compose.onNodeWithText("· agent-lab").assertIsDisplayed()
        compose.onNodeWithText("Deck bauen").assertIsDisplayed()
        compose.onNodeWithText("Bin dran, Gate läuft.").assertIsDisplayed()

        compose.onNodeWithText("Deck bauen").performClick()
        assertTrue(opened)
    }

    @Test
    fun aUsageCardWithoutWindowsStillRendersItsProse() {
        // openrouter reports no windows at all — only credit lines. A card that
        // drew a 0 % bar here would read as "plenty left".
        val usage = AccountUsage(
            provider = "openrouter",
            available = true,
            title = "Account limits",
            plan = null,
            windows = emptyList(),
            details = listOf("Credits balance: \$0.00"),
            unavailableReason = null,
            fallback = false,
            cached = false,
            fetchedAt = null,
        )
        compose.setContent { HermesDeckTheme { UsageCard(usage) } }

        compose.onNodeWithText("OpenRouter").assertIsDisplayed()
        compose.onNodeWithText("Credits balance: \$0.00").assertIsDisplayed()
        compose.onAllNodesWithText("0 %").fetchSemanticsNodes().let {
            assertTrue("Ohne Fenster darf kein Prozentwert erscheinen", it.isEmpty())
        }
    }
}
