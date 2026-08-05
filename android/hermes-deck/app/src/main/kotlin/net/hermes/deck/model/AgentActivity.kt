package net.hermes.deck.model

/**
 * Turns an agent's heartbeat numbers into the one sentence the card shows.
 *
 * Pure and parameterised on purpose. The search bug of 2026-08-05 came from a
 * derivation that read its inputs itself instead of taking them, which made it
 * invisible to Compose and to tests alike; this takes the agent and the window
 * and returns a value.
 *
 * The states are deliberately five, not two. A green/grey split would have to
 * put "systemd says active but nothing has happened for an hour" on one side or
 * the other, and either choice is a lie — that gap is precisely what this
 * feature exists to show.
 */
object AgentActivity {

    enum class State {
        /** Tool calls inside the recent window: working right now. */
        WORKING,

        /** Worked inside the long window, but not just now. */
        RECENT,

        /** Running, reachable, and has done nothing in the whole window. */
        QUIET,

        /** The unit itself is not up. */
        FAILED,

        /** The journal could not be read — not the same as "did nothing". */
        UNKNOWN,
    }

    data class Reading(val state: State, val label: String)

    fun of(agent: BuzzAgent, windowSeconds: Int, recentSeconds: Int): Reading {
        if (agent.isFailed) {
            return Reading(State.FAILED, "Unit gescheitert — der Agent läuft nicht")
        }
        val working = agent.working
            ?: return Reading(State.UNKNOWN, "Aktivität nicht lesbar")
        val window = agent.toolCallsWindow ?: 0
        val recent = agent.toolCallsRecent ?: 0

        if (working) {
            return Reading(
                State.WORKING,
                "arbeitet gerade · $recent ${calls(recent)} in ${span(recentSeconds)}",
            )
        }
        if (window > 0) {
            val ago = agent.lastToolCallSecondsAgo
            val since = if (ago == null) "zuletzt in dieser ${span(windowSeconds)}" else "zuletzt ${ago(ago)}"
            return Reading(State.RECENT, "$since · $window ${calls(window)} in ${span(windowSeconds)}")
        }
        // Zero is a measurement over the window, not a claim about all time —
        // the query is bounded, so "nie" is something we cannot know here.
        return Reading(State.QUIET, "keine Aktivität in ${span(windowSeconds)}")
    }

    private fun calls(count: Int): String = if (count == 1) "Tool-Call" else "Tool-Calls"

    /** "vor 3 Minuten" / "gerade eben", never a raw timestamp on a card. */
    fun ago(seconds: Int): String = when {
        seconds < 60 -> "gerade eben"
        seconds < 3600 -> "vor ${seconds / 60} min"
        seconds < 86_400 -> "vor ${seconds / 3600} h"
        else -> "vor ${seconds / 86_400} d"
    }

    /** "5 Minuten" / "1 Stunde" — reads the window off the server, never hardcodes it. */
    fun span(seconds: Int): String = when {
        seconds < 60 -> "$seconds s"
        seconds < 3600 -> "${seconds / 60} min"
        seconds == 3600 -> "1 Stunde"
        seconds % 3600 == 0 -> "${seconds / 3600} Stunden"
        else -> "${seconds / 60} min"
    }
}
