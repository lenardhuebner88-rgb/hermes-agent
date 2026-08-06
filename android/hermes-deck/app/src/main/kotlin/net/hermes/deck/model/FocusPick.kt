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
 *     reporting outranks the resting state, which is simply whoever is working.
 *     A context filling up is not an alarm until it is nearly full: it is a
 *     number that barely moves, and a card that sits on it for hours teaches the
 *     operator to stop reading the card.
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
     * Context share at which a compaction is imminent rather than eventual.
     * Deliberately far above the old resting rule's threshold of "the fullest
     * one, whatever that is": at 42 % nothing is about to happen, at 96 % the
     * next long turn interrupts itself.
     */
    const val CONTEXT_ALARM_PERCENT: Int = 95

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

        // 4b. A context at the brim. Last of the alarms: it interrupts one
        //     agent's turn, not the whole fleet, and unlike a hang it is
        //     something the operator can still act on calmly.
        rows.mapNotNull { row -> sessions[row.agent.stem]?.percent?.let { row to it } }
            .filter { it.second >= CONTEXT_ALARM_PERCENT }
            .maxByOrNull { it.second }
            ?.let { (row, _) ->
                return Choice(
                    subject = Subject.Agent(row.agent.stem),
                    rank = Rank.ALARM,
                    reason = "Dringend · Kontext fast voll",
                )
            }

        // 5. Resting state: whoever is actually working — and of those, the one
        //    doing the most. A context percentage was the old answer here and it
        //    was the wrong question: it moves slowly, it is the same number for
        //    hours, and it puts an idle agent on the card while three others are
        //    mid-turn. What the screen is for is *who is working, and since when*.
        //
        //    Ranked by calls in the window rather than by the freshest signal:
        //    the signal ticks every few seconds and would hand the card back and
        //    forth between two busy agents on every poll, while the call count
        //    only moves as work is actually done.
        val working = rows.filter { it.pulse?.looksOpen == true }
        if (working.isNotEmpty()) {
            val busiest = working.maxWith(
                compareBy<AgentRow> { it.pulse?.callsInWindow ?: 0 }
                    .thenByDescending { it.pulse?.lastSignalSecondsAgo ?: Int.MAX_VALUE },
            )
            return Choice(
                subject = Subject.Agent(busiest.agent.stem),
                rank = Rank.RESTING,
                reason = "Im Blick · arbeitet",
            )
        }

        // 6. Nobody has an open turn: the one who worked most recently. Still an
        //    answer to "wer arbeitet wann", just in the past tense.
        rows.mapNotNull { row -> row.pulse?.latest?.let { row to it.secondsAgo } }
            .minByOrNull { it.second }
            ?.let { (row, _) ->
                return Choice(
                    subject = Subject.Agent(row.agent.stem),
                    rank = Rank.RESTING,
                    reason = "Im Blick · zuletzt aktiv",
                )
            }

        // 7. No measurable activity anywhere — the session numbers are the only
        //    thing left that is real, so the fullest context keeps the card from
        //    going blank. Last resort, not the resting state.
        rows.mapNotNull { row -> sessions[row.agent.stem]?.percent?.let { row to it } }
            .maxByOrNull { it.second }
            ?.let { (row, _) ->
                return Choice(
                    subject = Subject.Agent(row.agent.stem),
                    rank = Rank.RESTING,
                    reason = "Im Blick · vollster Kontext",
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
