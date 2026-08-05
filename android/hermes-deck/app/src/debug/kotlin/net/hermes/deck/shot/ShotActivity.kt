package net.hermes.deck.shot

import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import net.hermes.deck.model.AccountUsage
import net.hermes.deck.model.ActionStack
import net.hermes.deck.model.DeckPulse
import net.hermes.deck.model.Freshness
import net.hermes.deck.ui.HermesDeckTheme
import net.hermes.deck.ui.PulseScreen
import org.json.JSONObject

/**
 * Renders [PulseScreen] from the checked-in payload so the screen can be
 * photographed — debug builds only, never in a release APK.
 *
 * Why this exists: until now every statement about how the Leitstand *looks*
 * was a text assurance. The obvious route, a Compose `captureToImage()` test,
 * reproducibly kills the hermes-deck AVD (see AGENTS.md). This activity avoids
 * that entirely: the picture is taken from outside with
 * `adb exec-out screencap`, which reads surfaceflinger and never touches the
 * app process — and it costs no new Gradle dependency.
 *
 * The payload is `deck-pulse-live.json`, the *real* answer of the running
 * dashboard captured 2026-08-05, shared with the JVM and device tests. Nothing
 * here invents a number: a state the fixture cannot support is not rendered.
 *
 *     adb shell am start -n net.hermes.deck/net.hermes.deck.shot.ShotActivity \
 *         --es state normal
 *     adb exec-out screencap -p > shot.png
 */
class ShotActivity : ComponentActivity() {

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        val state = intent?.getStringExtra(EXTRA_STATE) ?: STATE_NORMAL
        val payload = assets.open(FIXTURE).bufferedReader().use { it.readText() }
        val pulse = DeckPulse.fromJson(JSONObject(payload), receivedAt = System.currentTimeMillis())

        // The budget line is fed from its own endpoint in the real app, so the
        // shot carries its own real capture of it — otherwise the band would be
        // photographed in its empty state and prove nothing.
        val usageJson = JSONObject(assets.open(USAGE_FIXTURE).bufferedReader().use { it.readText() })
        val providers = usageJson.optJSONArray("providers")
        val usage = (0 until (providers?.length() ?: 0)).mapNotNull { index ->
            providers?.optJSONObject(index)?.let(AccountUsage::fromJson)
        }

        // A pinned agent is the one focus the fixture can prove on demand: the
        // ranking would otherwise pick whatever the captured moment happened to
        // make loudest, and the shot would not be reproducible.
        val pinned = when (state) {
            STATE_PINNED -> intent?.getStringExtra(EXTRA_STEM) ?: PINNED_FALLBACK
            else -> null
        }

        setContent {
            HermesDeckTheme {
                PulseScreen(
                    pulse = pulse,
                    freshness = Freshness.of(System.currentTimeMillis(), System.currentTimeMillis()),
                    actions = ActionStack.of(
                        holds = pulse.heldTasks,
                        runs = pulse.workerRuns,
                        agents = pulse.agentRows,
                    ),
                    usage = usage,
                    loading = false,
                    error = null,
                    pinnedStem = pinned,
                    onRefresh = {},
                    onPin = {},
                    onAgent = {},
                    onRun = {},
                )
            }
        }
    }

    private companion object {
        const val FIXTURE = "deck-pulse-live.json"
        const val USAGE_FIXTURE = "account-usage-live.json"
        const val EXTRA_STATE = "state"
        const val EXTRA_STEM = "stem"
        const val STATE_NORMAL = "normal"
        const val STATE_PINNED = "pinned"

        /** `hermes` is the agent the fixture proves keeps no session book. */
        const val PINNED_FALLBACK = "hermes"
    }
}
