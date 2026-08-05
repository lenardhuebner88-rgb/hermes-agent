package net.hermes.deck.ui

import androidx.compose.animation.core.RepeatMode
import androidx.compose.animation.core.animateFloat
import androidx.compose.animation.core.infiniteRepeatable
import androidx.compose.animation.core.rememberInfiniteTransition
import androidx.compose.animation.core.tween
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.alpha
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import net.hermes.deck.model.ActionStack
import net.hermes.deck.model.AgentPulse
import net.hermes.deck.model.AgentRow
import net.hermes.deck.model.DeckEvent
import net.hermes.deck.model.DeckPulse
import net.hermes.deck.model.DispatchHold
import net.hermes.deck.model.Freshness
import net.hermes.deck.model.ToolCall
import net.hermes.deck.model.WorkerRun

/**
 * The instrument screen: who is working, on what, what stands still, what wants me.
 *
 * Five sections, in the order an operator asks the questions:
 *
 *  1. **JETZT DRAN** — the one ranked stack of things that need a decision.
 *     Everything below it is context for this.
 *  2. **ARBEITET** — Buzz agents with the tool call they are in.
 *  3. **LÄUFT** — Hermes kanban runs, the other half of "who is working".
 *  4. **STEHT** — dispatch holds.
 *  5. **STILL** — agents that are up and doing nothing. Last, but never absent:
 *     the measured failure this feature exists for was eight units reporting
 *     `active` while four had been silent for hours.
 *
 * `accent` barely appears here on purpose. The semantic colours carry the
 * states, and a pulse screen that is colourful is the surest sign the accent
 * doctrine was not understood.
 */
@Composable
fun PulseScreen(
    pulse: DeckPulse?,
    freshness: Freshness,
    actions: List<ActionStack.Item>,
    loading: Boolean,
    error: String?,
    silentAfterSeconds: Int,
    onRefresh: () -> Unit,
    onAction: (ActionStack.Item) -> Unit,
    onAgent: (AgentRow) -> Unit,
    onRun: (WorkerRun) -> Unit,
    onOpenAgents: () -> Unit = {},
) {
    val deck = LocalDeck.current
    val working = pulse?.agentRows.orEmpty().filter { it.pulse?.looksOpen == true }
    val quiet = pulse?.agentRows.orEmpty()
        .filter { it.pulse?.looksOpen != true }
        .sortedByDescending { it.agent.lastToolCallSecondsAgo ?: -1 }
    val runs = pulse?.workerRuns.orEmpty()
    val holds = pulse?.heldTasks.orEmpty()

    LazyColumn(
        Modifier.fillMaxSize().background(deck.background),
        contentPadding = androidx.compose.foundation.layout.PaddingValues(
            start = DeckMetrics.screenPadding,
            end = DeckMetrics.screenPadding,
            top = 12.dp,
            bottom = 96.dp,
        ),
        verticalArrangement = Arrangement.spacedBy(DeckMetrics.gap),
    ) {
        item { PulseHeader(freshness, loading, onRefresh) }

        if (error != null) {
            item { AlertBand(error, deck.danger) }
        }
        pulse?.brokenSections?.takeIf { it.isNotEmpty() }?.let { broken ->
            item { AlertBand("Nicht abrufbar: ${broken.joinToString(", ")}", deck.warning) }
        }
        // A journal whose format moved reads exactly like a fleet at rest. It is
        // the one failure mode of this whole feature, so it gets a band of its own.
        if (pulse?.agents?.data?.parserSuspicious == true) {
            item {
                AlertBand(
                    "Das Journal liefert Zeilen, die der Server nicht mehr versteht — " +
                        "die Aktivität unten ist unvollständig.",
                    deck.danger,
                )
            }
        }
        pulse?.agents?.data?.activityError?.let {
            item { AlertBand("Aktivität nicht messbar — das Journal liess sich nicht lesen.", deck.warning) }
        }

        section("Jetzt dran") {
            if (actions.isEmpty()) {
                // An empty stack is a result, not a blank. Saying so is what
                // separates "nothing to do" from "this panel is broken".
                EmptyHint("Nichts liegt an — alles läuft oder wartet auf niemanden.")
            }
        }
        items(actions, key = { it.key }) { item -> ActionRow(item) { onAction(item) } }

        section("Arbeitet") {
            if (working.isEmpty()) {
                EmptyHint(
                    if (pulse == null) "Noch nichts abgerufen — oben antippen."
                    else "Gerade arbeitet kein Agent."
                )
            }
        }
        items(working, key = { it.agent.stem }) { row -> WorkingAgentCard(row) { onAgent(row) } }

        section("Läuft") {
            if (runs.isEmpty()) EmptyHint("Keine Kanban-Läufe — das Board ist leer oder pausiert.")
        }
        items(runs, key = { it.runId }) { run -> RunCard(run) { onRun(run) } }

        if (holds.isNotEmpty()) {
            section("Steht") {}
            items(holds, key = { "${it.bucket}-${it.taskId}" }) { hold -> HoldRow(hold) }
        }

        if (quiet.isNotEmpty()) {
            section("Still") {}
            items(quiet, key = { it.agent.stem }) { row ->
                SilentAgentRow(row, silentAfterSeconds) { onAgent(row) }
            }
        }

        // What happened while nobody was looking. The operator's first question
        // in the morning is not "what is running" but "what finished, and did
        // anything die" — and the ticker already travels in the pulse payload.
        val events = pulse?.recentEvents.orEmpty().take(6)
        if (events.isNotEmpty()) {
            section("Zuletzt passiert") {}
            items(events, key = { it.id }) { event -> EventRow(event) }
        }

        item {
            Spacer(Modifier.height(16.dp))
            Text(
                "Modelle & Verbrauch",
                color = deck.accent,
                fontSize = 13.sp,
                modifier = Modifier.fillMaxWidth().clickable(onClick = onOpenAgents).padding(vertical = 8.dp),
            )
        }
    }
}

/** A section label plus an optional empty state, as one list item. */
private fun androidx.compose.foundation.lazy.LazyListScope.section(
    title: String,
    empty: @Composable () -> Unit,
) {
    item {
        Column {
            Spacer(Modifier.height(8.dp))
            SectionLabel(title)
            empty()
        }
    }
}

@Composable
private fun SectionLabel(text: String) {
    val deck = LocalDeck.current
    Text(
        text.uppercase(),
        style = labelStyle,
        color = deck.textFaint,
        modifier = Modifier.padding(bottom = 6.dp),
    )
}

/**
 * The screen's one header line: what this is, and how old it is.
 *
 * The age is not decoration — on a polled screen it is the only thing that
 * separates a reading from a memory. Tapping it forces a tick, which is the
 * gesture people try first anyway.
 */
@Composable
private fun PulseHeader(freshness: Freshness, loading: Boolean, onRefresh: () -> Unit) {
    val deck = LocalDeck.current
    Row(
        Modifier.fillMaxWidth().clickable(onClick = onRefresh).padding(vertical = 4.dp),
        horizontalArrangement = Arrangement.SpaceBetween,
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Text("PULS", style = labelStyle, color = deck.textFaint)
        Text(
            when {
                loading -> "wird geholt …"
                else -> freshness.headerLabel
            },
            style = tickerStyle,
            color = if (freshness.isStale) deck.warning else deck.textFaint,
        )
    }
}

@Composable
private fun AlertBand(text: String, tint: Color) {
    val deck = LocalDeck.current
    Row(
        Modifier
            .fillMaxWidth()
            .clip(RoundedCornerShape(DeckMetrics.chipRadius))
            .background(deck.surfaceRaised)
            .padding(horizontal = 12.dp, vertical = 10.dp),
        verticalAlignment = Alignment.Top,
    ) {
        Box(Modifier.padding(top = 5.dp).size(6.dp).clip(CircleShape).background(tint))
        Spacer(Modifier.width(10.dp))
        Text(text, color = deck.textSecondary, fontSize = 12.sp, lineHeight = 17.sp)
    }
}

/** One row of the action stack: what · seit wann · [tap]. */
@Composable
private fun ActionRow(item: ActionStack.Item, onClick: () -> Unit) {
    val deck = LocalDeck.current
    val tint = when (item.severity) {
        ActionStack.Severity.CRITICAL -> deck.danger
        ActionStack.Severity.WARNING -> deck.warning
        ActionStack.Severity.INFO -> deck.textSecondary
    }
    GlassCard(onClick = onClick) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            Box(Modifier.size(8.dp).clip(CircleShape).background(tint))
            Spacer(Modifier.width(12.dp))
            Column(Modifier.weight(1f)) {
                Text(
                    item.title,
                    color = deck.textPrimary,
                    fontSize = 15.sp,
                    fontWeight = FontWeight.SemiBold,
                    maxLines = 2,
                    overflow = TextOverflow.Ellipsis,
                )
                item.detail?.let {
                    Text(it, color = deck.textSecondary, fontSize = 12.sp, maxLines = 1, overflow = TextOverflow.Ellipsis)
                }
            }
            item.ageSeconds?.let {
                Spacer(Modifier.width(8.dp))
                Text(ago(it), style = tickerStyle, color = deck.textFaint)
            }
        }
    }
}

/**
 * An agent that is in a tool call, with the call spelled out.
 *
 * The dot breathes — and only here, and only for an agent whose call is open.
 * Motion is a claim about state in this app; a pulse that always pulses is
 * decoration, and decoration on a monitoring screen is a lie with a heartbeat.
 */
@Composable
private fun WorkingAgentCard(row: AgentRow, onClick: () -> Unit) {
    val deck = LocalDeck.current
    val pulse = row.pulse
    val call = pulse?.latest
    val transition = rememberInfiniteTransition(label = "puls")
    val alpha by transition.animateFloat(
        initialValue = 1f,
        targetValue = 0.35f,
        animationSpec = infiniteRepeatable(tween(1100), RepeatMode.Reverse),
        label = "atem",
    )
    GlassCard(onClick = onClick) {
        Column {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Box(
                    Modifier
                        .size(8.dp)
                        .alpha(alpha)
                        .clip(CircleShape)
                        .background(deck.success),
                )
                Spacer(Modifier.width(12.dp))
                Text(
                    row.agent.displayName,
                    color = deck.textPrimary,
                    fontSize = 15.sp,
                    fontWeight = FontWeight.SemiBold,
                    modifier = Modifier.weight(1f),
                )
                call?.let { Text(ago(it.secondsAgo), style = tickerStyle, color = deck.textFaint) }
            }
            if (call != null) {
                Spacer(Modifier.height(8.dp))
                Row(verticalAlignment = Alignment.CenterVertically) {
                    KindChip(call.kind)
                    Spacer(Modifier.width(8.dp))
                    // One line, the card owns the truncation. A journal label can
                    // be a whole heredoc; letting it wrap would push every card
                    // below it off the screen.
                    Text(
                        call.label,
                        style = monoStyle,
                        color = deck.textSecondary,
                        maxLines = 1,
                        overflow = TextOverflow.Ellipsis,
                    )
                }
            }
        }
    }
}

@Composable
private fun KindChip(kind: ToolCall.Kind) {
    val deck = LocalDeck.current
    Text(
        kind.label,
        style = labelStyle.copy(fontSize = 10.sp, letterSpacing = 0.5.sp),
        color = deck.textFaint,
        modifier = Modifier
            .clip(RoundedCornerShape(DeckMetrics.chipRadius))
            .background(deck.surfaceRaised)
            .padding(horizontal = 8.dp, vertical = 3.dp),
    )
}

/** A Hermes kanban run: which card, how long, what it last said. */
@Composable
private fun RunCard(run: WorkerRun, onClick: () -> Unit) {
    val deck = LocalDeck.current
    GlassCard(accent = deck.edgeFor(run.profile), onClick = onClick) {
        Column(Modifier.padding(start = 8.dp)) {
            Text(
                run.title,
                color = deck.textPrimary,
                fontSize = 15.sp,
                fontWeight = FontWeight.SemiBold,
                maxLines = 2,
                overflow = TextOverflow.Ellipsis,
            )
            Spacer(Modifier.height(6.dp))
            Row(verticalAlignment = Alignment.CenterVertically) {
                Text(run.profile, style = tickerStyle, color = deck.textFaint)
                Spacer(Modifier.width(8.dp))
                Text(
                    "läuft ${duration(run.runtimeSeconds)}",
                    style = tickerStyle,
                    color = if (run.isOverBudget) deck.danger else deck.textFaint,
                )
                if (run.isOverBudget) {
                    Spacer(Modifier.width(8.dp))
                    Text("über Budget", style = tickerStyle, color = deck.danger)
                }
            }
            run.heartbeatNote?.let { note ->
                Spacer(Modifier.height(6.dp))
                Text(
                    note,
                    color = deck.textSecondary,
                    fontSize = 12.sp,
                    maxLines = 2,
                    overflow = TextOverflow.Ellipsis,
                )
            }
            // Only when the server stands behind a number. An invented
            // percentage on an uncapped lane is worse than no bar at all.
            run.progressFraction?.let { fraction ->
                Spacer(Modifier.height(10.dp))
                ProgressBar(fraction)
            }
        }
    }
}

@Composable
private fun ProgressBar(fraction: Double) {
    val deck = LocalDeck.current
    Box(
        Modifier
            .fillMaxWidth()
            .height(4.dp)
            .clip(RoundedCornerShape(2.dp))
            .background(deck.accentSoft),
    ) {
        Box(
            Modifier
                .fillMaxWidth(fraction.coerceIn(0.0, 1.0).toFloat())
                .height(4.dp)
                .clip(RoundedCornerShape(2.dp))
                .background(deck.accent),
        )
    }
}

@Composable
private fun HoldRow(hold: DispatchHold) {
    val deck = LocalDeck.current
    Row(
        Modifier.fillMaxWidth().padding(vertical = 6.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Box(
            Modifier.size(6.dp).clip(CircleShape)
                .background(if (hold.needsOperator) deck.warning else deck.textFaint),
        )
        Spacer(Modifier.width(12.dp))
        Column(Modifier.weight(1f)) {
            Text(
                hold.displayTitle,
                color = deck.textPrimary,
                fontSize = 14.sp,
                maxLines = 1,
                overflow = TextOverflow.Ellipsis,
            )
            Text(
                listOfNotNull(hold.label, hold.detail).joinToString(" · "),
                color = deck.textSecondary,
                fontSize = 12.sp,
                maxLines = 1,
                overflow = TextOverflow.Ellipsis,
            )
        }
    }
}

/**
 * An agent that is up and doing nothing, with how long that has been true.
 *
 * Past the threshold the dot turns to `warning` — at that point silence is a
 * state the operator has to resolve, which is exactly what the semantic colour
 * is reserved for.
 */
@Composable
private fun SilentAgentRow(row: AgentRow, silentAfterSeconds: Int, onClick: () -> Unit) {
    val deck = LocalDeck.current
    val silentFor = row.agent.lastToolCallSecondsAgo
    val alarming = row.agent.isFailed ||
        (row.agent.isRunning && silentFor != null && silentFor >= silentAfterSeconds)
    Row(
        Modifier.fillMaxWidth().clickable(onClick = onClick).padding(vertical = 8.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Box(
            Modifier.size(6.dp).clip(CircleShape).background(
                when {
                    row.agent.isFailed -> deck.danger
                    alarming -> deck.warning
                    else -> deck.textFaint
                },
            ),
        )
        Spacer(Modifier.width(12.dp))
        Text(row.agent.displayName, color = deck.textSecondary, fontSize = 14.sp, modifier = Modifier.weight(1f))
        Text(
            when {
                row.agent.isFailed -> "Unit gescheitert"
                row.pulse == null -> "nicht messbar"
                silentFor == null -> "keine Messung"
                else -> "still seit ${duration(silentFor)}"
            },
            style = tickerStyle,
            color = if (alarming) deck.warning else deck.textFaint,
        )
    }
}

@Composable
private fun EventRow(event: DeckEvent) {
    val deck = LocalDeck.current
    Row(
        Modifier.fillMaxWidth().padding(vertical = 5.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Box(
            Modifier.size(5.dp).clip(CircleShape)
                .background(if (event.isFailure) deck.danger else deck.success),
        )
        Spacer(Modifier.width(12.dp))
        Text(
            event.title,
            color = deck.textSecondary,
            fontSize = 13.sp,
            maxLines = 1,
            overflow = TextOverflow.Ellipsis,
            modifier = Modifier.weight(1f),
        )
        Spacer(Modifier.width(8.dp))
        Text(
            event.label,
            style = tickerStyle,
            color = if (event.isFailure) deck.danger else deck.textFaint,
        )
    }
}

/** "vor 12 s" — relative, always, never an absolute stamp on a card. */
internal fun ago(seconds: Int): String = when {
    seconds < 5 -> "gerade eben"
    seconds < 60 -> "vor $seconds s"
    seconds < 3600 -> "vor ${seconds / 60} min"
    seconds < 86_400 -> "vor ${seconds / 3600} h"
    else -> "vor ${seconds / 86_400} d"
}

/** "8 min" — a span, without the "vor". */
internal fun duration(seconds: Int): String = when {
    seconds < 60 -> "$seconds s"
    seconds < 3600 -> "${seconds / 60} min"
    seconds < 86_400 -> "${seconds / 3600} h"
    else -> "${seconds / 86_400} d"
}

/** The agent timeline, one sheet: the last calls, newest first. */
@Composable
fun AgentTimeline(pulse: AgentPulse?, windowSeconds: Int) {
    val deck = LocalDeck.current
    if (pulse == null) {
        EmptyHint("Aktivität nicht messbar — das Journal liess sich nicht lesen.")
        return
    }
    if (pulse.recent.isEmpty()) {
        EmptyHint("Keine Tool-Calls in ${duration(windowSeconds)}.")
        return
    }
    Column(verticalArrangement = Arrangement.spacedBy(10.dp)) {
        pulse.recent.forEach { call ->
            Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.Top) {
                Text(
                    ago(call.secondsAgo),
                    style = tickerStyle,
                    color = deck.textFaint,
                    modifier = Modifier.width(72.dp),
                )
                Column(Modifier.weight(1f)) {
                    KindChip(call.kind)
                    Spacer(Modifier.height(4.dp))
                    Text(
                        call.label,
                        style = monoStyle,
                        color = deck.textSecondary,
                        maxLines = 2,
                        overflow = TextOverflow.Ellipsis,
                    )
                }
            }
        }
    }
}
