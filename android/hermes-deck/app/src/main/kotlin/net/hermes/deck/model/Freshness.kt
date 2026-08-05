package net.hermes.deck.model

/**
 * How old the number on screen is — and whether it is still worth believing.
 *
 * Until now the deck loaded its agent data once, when a screen appeared, and
 * every value then claimed to be current for as long as the user looked at it.
 * On a screen whose whole purpose is "what is happening right now", that is the
 * most expensive kind of wrong: it is indistinguishable from being right.
 *
 * So freshness is a value the UI has to render, not a detail it may forget.
 */
data class Freshness(val ageSeconds: Int, val state: State) {

    enum class State {
        /** Younger than one poll interval — say the age, quietly. */
        FRESH,

        /** A few intervals old: a tick was missed. Say so plainly. */
        AGING,

        /** Old enough that the screen is no longer evidence. Dim it. */
        STALE,
    }

    val isStale: Boolean get() = state == State.STALE

    /** "gerade eben" / "vor 12 s" / "vor 4 min" — never an absolute timestamp. */
    val label: String
        get() = when {
            ageSeconds < 5 -> "gerade eben"
            ageSeconds < 60 -> "vor $ageSeconds s"
            ageSeconds < 3600 -> "vor ${ageSeconds / 60} min"
            ageSeconds < 86_400 -> "vor ${ageSeconds / 3600} h"
            else -> "vor ${ageSeconds / 86_400} d"
        }

    /** What a header shows: the age, prefixed once, so the unit reads as a stamp. */
    val headerLabel: String get() = "Stand $label"

    companion object {
        /** One poll interval. Anything younger is simply current. */
        const val FRESH_SECONDS: Int = 15

        /** Three missed intervals — at that point the loop is not keeping up. */
        const val STALE_SECONDS: Int = 60

        /**
         * @param receivedAtMillis device clock when the payload arrived
         * @param nowMillis device clock now — both from the *same* clock, which
         *   is the only reason subtracting them is legitimate here.
         */
        fun of(
            receivedAtMillis: Long,
            nowMillis: Long,
            freshSeconds: Int = FRESH_SECONDS,
            staleSeconds: Int = STALE_SECONDS,
        ): Freshness {
            if (receivedAtMillis <= 0L) {
                // Nothing has ever arrived. That is stale by definition, and
                // pretending it is fresh would show an empty screen as a calm one.
                return Freshness(ageSeconds = 0, state = State.STALE)
            }
            val age = ((nowMillis - receivedAtMillis) / 1000L).coerceAtLeast(0L).toInt()
            val state = when {
                age <= freshSeconds -> State.FRESH
                age < staleSeconds -> State.AGING
                else -> State.STALE
            }
            return Freshness(ageSeconds = age, state = state)
        }
    }
}
