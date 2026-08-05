package net.hermes.deck.model

/**
 * Which subject the entry screen puts in its one focus card.
 *
 * The screen has no tabs and no switch: it shows whatever most needs the
 * operator, and it changes because the *system* changed, not because someone
 * pressed something. That makes the ranking a load-bearing rule rather than a
 * detail, so it lives here as tested logic instead of inside a composable.
 *
 * Two guarantees the UI depends on:
 *
 *  1. **Alarm beats normal.** A budget about to bite or an agent that stopped
 *     reporting outranks "fullest context", which is only the resting state.
 *  2. **The screen never moves under the finger.** A pinned subject — the
 *     operator tapped a capsule in the fleet rail, or is typing — wins over
 *     everything the ranking would otherwise choose. A focus card that jumps
 *     away mid-read is the fastest way to make a live screen unusable.
 */
object FocusPick {

    sealed interface Subject {
        data class Agent(val stem: String) : Subject
        data object Budget : Subject
    }

    enum class Rank {
        /** Something is going wrong now. */
        ALARM,

        /** Nothing is wrong; this is simply the most interesting agent. */
        RESTING,

        /** Nothing to show at all. */
        NONE,
    }

    data class Choice(
        val subject: Subject?,
        val rank: Rank,
        /** The card's own kicker line — why *this* subject is here. */
        val reason: String,
        /** True when the operator, not the ranking, decided. */
        val pinned: Boolean = false,
    )

    /** Budget share from which the next long run may hit the wall. */
    const val BUDGET_ALARM_PERCENT: Double = 90.0

    /**
     * An open call with no life sign for this long is a hang, not long work.
     * The `in_progress` tick fires roughly every 30 s, so 90 s is three misses.
     */
    const val STALL_SECONDS: Int = 90

    fun of(
        rows: List<AgentRow>,
        usage: Collection<AccountUsage> = emptyList(),
        sessions: Map<String, SessionFacts> = emptyMap(),
        pinnedStem: String? = null,
    ): Choice {
        // 1. The operator's choice always wins — including over an alarm. An
        //    alarm that yanks the screen away while he is reading another agent
        //    is worse than an alarm he reaches one tap later; the dock keeps
        //    shouting either way.
        pinnedStem?.let { stem ->
            rows.firstOrNull { it.agent.stem == stem }?.let { row ->
                return Choice(
                    subject = Subject.Agent(stem),
                    rank = if (isStalled(row)) Rank.ALARM else Rank.RESTING,
                    reason = if (isStalled(row)) "Angeheftet · steht" else "Angeheftet",
                    pinned = true,
                )
            }
        }

        // 2. Budget first among alarms: it is the only one that ends *all* work.
        val tightest = usage.mapNotNull { entry -> entry.peakPercent?.let { entry to it } }
            .maxByOrNull { it.second }
        if (tightest != null && tightest.second >= BUDGET_ALARM_PERCENT) {
            return Choice(
                subject = Subject.Budget,
                rank = Rank.ALARM,
                reason = "Dringend · Verbrennung",
            )
        }

        // 3. A failed unit outranks a hang: it is not working at all.
        rows.firstOrNull { it.agent.isFailed }?.let { row ->
            return Choice(
                subject = Subject.Agent(row.agent.stem),
                rank = Rank.ALARM,
                reason = "Dringend · Unit gescheitert",
            )
        }

        // 4. The longest hang.
        rows.filter { isStalled(it) }
            .maxByOrNull { it.pulse?.latest?.secondsAgo ?: 0 }
            ?.let { row ->
                return Choice(
                    subject = Subject.Agent(row.agent.stem),
                    rank = Rank.ALARM,
                    reason = "Dringend · steht",
                )
            }

        // 5. Resting state: the fullest measured context. Deliberately not "the
        //    busiest" — busy is normal and needs no attention, while a context
        //    filling up is the thing that will interrupt work without warning.
        val fullest = rows
            .mapNotNull { row -> sessions[row.agent.stem]?.percent?.let { row to it } }
            .maxByOrNull { it.second }
        if (fullest != null) {
            return Choice(
                subject = Subject.Agent(fullest.first.agent.stem),
                rank = Rank.RESTING,
                reason = "Im Blick · vollster Kontext",
            )
        }

        // 6. No session numbers anywhere — fall back to whoever is working, so
        //    the card is still about something real.
        rows.firstOrNull { it.pulse?.looksOpen == true }?.let { row ->
            return Choice(
                subject = Subject.Agent(row.agent.stem),
                rank = Rank.RESTING,
                reason = "Im Blick · arbeitet",
            )
        }

        return Choice(subject = null, rank = Rank.NONE, reason = "Niemand arbeitet gerade")
    }

    /**
     * An open call whose last life sign is older than the call itself, by more
     * than [STALL_SECONDS]. Both numbers come from the server, so no clock of
     * the phone's enters into it.
     */
    fun isStalled(row: AgentRow, stallSeconds: Int = STALL_SECONDS): Boolean {
        val pulse = row.pulse ?: return false
        if (!pulse.looksOpen) return false
        val signal = pulse.lastSignalSecondsAgo ?: return false
        return signal >= stallSeconds
    }
}
