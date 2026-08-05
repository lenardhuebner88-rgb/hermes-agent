package net.hermes.deck.ui

import androidx.compose.animation.core.RepeatMode
import androidx.compose.animation.core.animateFloat
import androidx.compose.animation.core.infiniteRepeatable
import androidx.compose.animation.core.rememberInfiniteTransition
import androidx.compose.animation.core.tween
import androidx.compose.foundation.background
import androidx.compose.foundation.text.BasicTextField
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.WindowInsets
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.statusBars
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.layout.windowInsetsPadding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.alpha
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.SolidColor
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import net.hermes.deck.model.AccountUsage
import net.hermes.deck.model.ActionStack
import net.hermes.deck.model.AgentPulse
import net.hermes.deck.model.AgentRow
import net.hermes.deck.model.DeckPulse
import net.hermes.deck.model.FocusPick
import net.hermes.deck.model.Freshness
import net.hermes.deck.model.SessionFacts
import net.hermes.deck.model.ToolCall
import net.hermes.deck.model.TurnChain
import net.hermes.deck.model.WorkerRun

/**
 * The entry screen — a control stand, not a table.
 *
 * Three layers, and the middle one is the point:
 *
 *  1. **The fleet rail** — every agent on 62 dp. Peripheral vision, not reading.
 *  2. **One focus card** — whatever most needs the operator, chosen by
 *     [FocusPick] rather than by a tab. It carries the context gauge, the
 *     session line and the *shape of the turn*.
 *  3. **The stream** — what is happening, newest first.
 *
 * There are no tabs and no switch: the screen changes because the system
 * changed. The one thing it must never do is move under a finger, which is why
 * a pinned agent outranks even an alarm — the dock keeps shouting either way.
 *
 * `accent` is nearly absent by design; the semantic colours carry the states.
 */
@Composable
fun PulseScreen(
    pulse: DeckPulse?,
    freshness: Freshness,
    actions: List<ActionStack.Item>,
    usage: List<AccountUsage>,
    loading: Boolean,
    error: String?,
    pinnedStem: String?,
    onRefresh: () -> Unit,
    onPin: (String?) -> Unit,
    onAgent: (AgentRow) -> Unit,
    onRun: (WorkerRun) -> Unit,
    onOpenAgents: () -> Unit = {},
    onOpenAction: (ActionStack.Item) -> Unit = {},
    onCallOut: (AgentRow, String) -> Unit = { _, _ -> },
) {
    val deck = LocalDeck.current
    val rows = pulse?.agentRows.orEmpty()
    val sessions = rows.mapNotNull { row -> row.session?.let { row.agent.stem to it } }.toMap()
    val choice = FocusPick.of(rows, usage, sessions, pinnedStem)
    val focusRow = (choice.subject as? FocusPick.Subject.Agent)
        ?.let { subject -> rows.firstOrNull { it.agent.stem == subject.stem } }

    LazyColumn(
        Modifier
            .fillMaxSize()
            .background(deck.background)
            // Without this the title sat *under* the clock and the battery
            // icon — a screenshot caught it; no test could have.
            .windowInsetsPadding(WindowInsets.statusBars),
        contentPadding = PaddingValues(
            start = DeckMetrics.screenPadding,
            end = DeckMetrics.screenPadding,
            top = 12.dp,
            bottom = 104.dp,
        ),
        verticalArrangement = Arrangement.spacedBy(10.dp),
    ) {
        item { LeitstandHeader(freshness, loading, onRefresh) }

        // The draft carries this in all three states, and the data was already
        // being computed and thrown away: PulseScreen took `actions` and never
        // rendered them.
        actions.firstOrNull()?.let { top ->
            item { NowDueDock(top, actions.size, onOpenAction) }
        }

        if (rows.isNotEmpty()) {
            item { FleetRail(rows, pinnedStem) { stem -> onPin(if (stem == pinnedStem) null else stem) } }
            item { BurnBand(rows, usage) }
        }

        if (error != null) item { AlertBand(error, deck.danger) }
        pulse?.brokenSections?.takeIf { it.isNotEmpty() }?.let { broken ->
            item { AlertBand("Nicht abrufbar: ${broken.joinToString(", ")}", deck.warning) }
        }
        // A journal whose format moved reads exactly like a fleet at rest. It is
        // this feature's one silent death, so it gets a band of its own.
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

        item {
            when {
                choice.subject == FocusPick.Subject.Budget -> BudgetFocusCard(choice, usage, rows)
                focusRow != null ->
                    AgentFocusCard(choice, focusRow, onCallOut) { onAgent(focusRow) }
                else -> RestingCard(pulse, rows.size)
            }
        }

        item {
            Spacer(Modifier.height(4.dp))
            Row(
                Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.SpaceBetween,
                verticalAlignment = Alignment.Bottom,
            ) {
                Text("STROM", style = labelStyle, color = deck.textFaint)
                Text(
                    pulse?.let { "${it.recentEvents.size} Ereignisse" }.orEmpty(),
                    style = tickerStyle,
                    color = deck.textFaint,
                )
            }
            Spacer(Modifier.height(8.dp))
        }

        val stream = buildStream(pulse)
        if (stream.isEmpty()) {
            item { EmptyHint("Noch nichts passiert — oder der Abgleich läuft.") }
        }
        items(stream, key = { it.key }) { entry -> StreamRow(entry) }

        item {
            Spacer(Modifier.height(14.dp))
            Text(
                "Modelle & Verbrauch",
                color = deck.accent,
                fontSize = 13.sp,
                modifier = Modifier
                    .fillMaxWidth()
                    .clickable(onClick = onOpenAgents)
                    .padding(vertical = 8.dp),
            )
        }
    }
}

// ------------------------------------------------------------------ header

@Composable
private fun LeitstandHeader(freshness: Freshness, loading: Boolean, onRefresh: () -> Unit) {
    val deck = LocalDeck.current
    Row(
        Modifier.fillMaxWidth().clickable(onClick = onRefresh).padding(vertical = 2.dp),
        horizontalArrangement = Arrangement.SpaceBetween,
        verticalAlignment = Alignment.Bottom,
    ) {
        Text("Leitstand", color = deck.textPrimary, fontSize = 22.sp, fontWeight = FontWeight.Bold)
        Text(
            if (loading) "wird geholt …" else freshness.headerLabel,
            style = tickerStyle,
            color = if (freshness.isStale) deck.warning else deck.textFaint,
        )
    }
}

// --------------------------------------------------------------- now due

/**
 * "Jetzt dran" — the single loudest item, with the count of what is queued
 * behind it.
 *
 * The stack already ranks severity before age ([ActionStack]); this only shows
 * its head. Listing all of them would recreate the panel-of-everything the
 * stack exists to replace, and a number is enough to say "there is more".
 */
@Composable
private fun NowDueDock(top: ActionStack.Item, total: Int, onOpen: (ActionStack.Item) -> Unit) {
    val deck = LocalDeck.current
    val tint = when (top.severity) {
        ActionStack.Severity.CRITICAL -> deck.danger
        ActionStack.Severity.WARNING -> deck.warning
        ActionStack.Severity.INFO -> deck.accent
    }
    Row(
        Modifier
            .fillMaxWidth()
            .clip(RoundedCornerShape(DeckMetrics.chipRadius))
            .background(deck.surfaceRaised)
            .clickable { onOpen(top) }
            .padding(horizontal = 12.dp, vertical = 10.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Box(
            Modifier.size(22.dp).clip(CircleShape).background(tint.copy(alpha = 0.18f)),
            contentAlignment = Alignment.Center,
        ) {
            Text("$total", color = tint, fontSize = 11.sp, fontWeight = FontWeight.Bold)
        }
        Spacer(Modifier.width(11.dp))
        Column(Modifier.weight(1f)) {
            Text(
                if (total > 1) "Jetzt dran — $total für dich" else "Jetzt dran",
                color = deck.textPrimary,
                fontSize = 13.sp,
                fontWeight = FontWeight.SemiBold,
            )
            Text(
                listOfNotNull(
                    top.title.takeIf { it.isNotBlank() },
                    top.detail,
                    top.ageSeconds?.let { "seit ${duration(it)}" },
                ).joinToString(" · "),
                color = deck.textSecondary,
                fontSize = 11.sp,
                maxLines = 2,
                overflow = TextOverflow.Ellipsis,
            )
        }
        Spacer(Modifier.width(8.dp))
        Text("Öffnen →", color = deck.accent, fontSize = 12.sp)
    }
}

// -------------------------------------------------------------- fleet rail

/**
 * Every agent on one line: initials, context micro-bar, state dot.
 *
 * The rail is the escape hatch from the automatic focus — tapping a capsule
 * pins that agent, tapping it again releases it. Without it the operator would
 * be at the mercy of the ranking.
 */
@Composable
private fun FleetRail(rows: List<AgentRow>, pinned: String?, onPick: (String) -> Unit) {
    val deck = LocalDeck.current
    Row(horizontalArrangement = Arrangement.spacedBy(5.dp), modifier = Modifier.fillMaxWidth()) {
        rows.forEach { row ->
            val working = row.pulse?.looksOpen == true
            val stalled = FocusPick.isStalled(row)
            val tint = when {
                row.agent.isFailed -> deck.danger
                stalled || row.session?.isTight == true -> deck.warning
                working -> deck.success
                else -> deck.textFaint
            }
            Column(
                Modifier
                    .weight(1f)
                    .clip(RoundedCornerShape(12.dp))
                    .background(if (row.agent.stem == pinned) deck.accentSoft else deck.surface)
                    .clickable { onPick(row.agent.stem) }
                    .padding(vertical = 6.dp),
                horizontalAlignment = Alignment.CenterHorizontally,
                verticalArrangement = Arrangement.spacedBy(4.dp),
            ) {
                Text(
                    initials(row.agent.displayName),
                    color = if (working) deck.textPrimary else deck.textSecondary,
                    fontSize = 10.sp,
                    fontWeight = FontWeight.Bold,
                )
                ContextMicroBar(row.session)
                Box(
                    Modifier
                        .size(5.dp)
                        .then(if (working && !stalled) Modifier.alpha(breath()) else Modifier)
                        .clip(CircleShape)
                        .background(tint),
                )
            }
        }
    }
}

/** Two letters, so "Claude" and "Codex" do not both read as "C". */
internal fun initials(name: String): String {
    val trimmed = name.trim()
    if (trimmed.isEmpty()) return "?"
    return trimmed.take(2).replaceFirstChar { it.uppercase() }
}

/**
 * The context as three pixels. A dashed track means "this agent keeps no
 * session book" — never an empty bar, which would read as zero.
 */
@Composable
private fun ContextMicroBar(session: SessionFacts?) {
    val deck = LocalDeck.current
    val fraction = session?.fraction
    Box(
        Modifier
            .width(20.dp)
            .height(3.dp)
            .clip(RoundedCornerShape(2.dp))
            .background(
                if (fraction == null) {
                    Brush.horizontalGradient(
                        0f to deck.outline,
                        0.45f to Color.Transparent,
                        0.55f to deck.outline,
                        1f to Color.Transparent,
                    )
                } else {
                    Brush.horizontalGradient(listOf(deck.outline, deck.outline))
                },
            ),
    ) {
        if (fraction != null) {
            Box(
                Modifier
                    .fillMaxWidth(fraction.toFloat())
                    .height(3.dp)
                    .clip(RoundedCornerShape(2.dp))
                    .background(meterColor(session)),
            )
        }
    }
}

@Composable
private fun meterColor(session: SessionFacts?): Color {
    val deck = LocalDeck.current
    return when {
        session?.isCritical == true -> deck.danger
        session?.isTight == true -> deck.warning
        else -> deck.accent
    }
}

// --------------------------------------------------------------- burn band

/**
 * What the fleet is consuming, plus the tightest budget.
 *
 * Deliberately *not* a tokens-per-hour figure: the app cannot measure a rate
 * yet, and inventing one would be exactly the dishonesty the rest of this
 * screen avoids. What it can prove is tool-call load per agent and the provider
 * budget — so that is what it shows, labelled as what it is.
 */
@Composable
private fun BurnBand(rows: List<AgentRow>, usage: List<AccountUsage>) {
    val deck = LocalDeck.current
    val loads = rows.map { it.pulse?.callsInWindow ?: 0 }
    val total = loads.sum()
    val tightest = usage.mapNotNull { entry -> entry.peakPercent?.let { entry to it } }
        .maxByOrNull { it.second }
    val alarm = (tightest?.second ?: 0.0) >= FocusPick.BUDGET_ALARM_PERCENT

    Row(
        Modifier
            .fillMaxWidth()
            .clip(RoundedCornerShape(13.dp))
            .background(if (alarm) deck.danger.copy(alpha = 0.07f) else deck.surface)
            .padding(horizontal = 11.dp, vertical = 8.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Row(
            Modifier.weight(1f).height(16.dp),
            horizontalArrangement = Arrangement.spacedBy(1.5.dp),
            verticalAlignment = Alignment.Bottom,
        ) {
            val peak = (loads.maxOrNull() ?: 1).coerceAtLeast(1)
            loads.forEach { load ->
                Box(
                    Modifier
                        .weight(1f)
                        .height((3 + 13f * load / peak).dp)
                        .clip(RoundedCornerShape(1.dp))
                        .background(if (load * 2 > peak) deck.accent else deck.accentSoft),
                )
            }
        }
        Spacer(Modifier.width(10.dp))
        Column(horizontalAlignment = Alignment.End) {
            Text(
                "$total Tool-Calls",
                style = tickerStyle,
                color = if (alarm) deck.danger else deck.textSecondary,
                fontWeight = FontWeight.SemiBold,
            )
            Text(
                tightest?.let { (entry, percent) -> "${entry.displayName} ${percent.toInt()} %" }
                    ?: "Budget unbekannt",
                style = tickerStyle,
                color = deck.textFaint,
            )
        }
    }
}

// -------------------------------------------------------------- focus card

private val ALARM_BRUSH = Brush.linearGradient(listOf(Color(0xFF3B1220), Color(0xFF7A1D3A)))

@Composable
private fun AgentFocusCard(
    choice: FocusPick.Choice,
    row: AgentRow,
    onCallOut: (AgentRow, String) -> Unit,
    onOpen: () -> Unit,
) {
    val deck = LocalDeck.current
    val alarm = choice.rank == FocusPick.Rank.ALARM
    val session = row.session
    val chain = TurnChain.of(row.pulse)

    Column(
        Modifier
            .fillMaxWidth()
            .clip(RoundedCornerShape(DeckMetrics.heroRadius))
            .background(if (alarm) ALARM_BRUSH else deck.heroBrush)
            .clickable(onClick = onOpen)
            .padding(17.dp),
    ) {
        Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
            Text(
                choice.reason.uppercase(),
                style = labelStyle,
                color = Color.White.copy(alpha = 0.58f),
            )
            Text(
                when (session?.steerable) {
                    true -> "LENKBAR"
                    false -> "STELLT AN"
                    null -> ""
                },
                style = labelStyle,
                color = Color.White.copy(alpha = 0.58f),
            )
        }

        Spacer(Modifier.height(10.dp))
        Row(verticalAlignment = Alignment.CenterVertically) {
            ContextGauge(session)
            Spacer(Modifier.width(13.dp))
            Column(Modifier.weight(1f)) {
                Text(
                    row.agent.displayName,
                    color = Color.White,
                    fontSize = 19.sp,
                    fontWeight = FontWeight.Bold,
                    maxLines = 1,
                    overflow = TextOverflow.Ellipsis,
                )
                Text(sessionLine(session), color = Color.White.copy(alpha = 0.7f), fontSize = 11.sp)
                Text(turnLine(row), color = Color.White.copy(alpha = 0.7f), fontSize = 11.sp)
            }
        }

        if (!chain.isEmpty) {
            Spacer(Modifier.height(12.dp))
            TurnChainRow(chain)
        }
        row.pulse?.latest?.let { call ->
            Spacer(Modifier.height(9.dp))
            Text(
                call.label,
                style = monoStyle,
                color = Color.White.copy(alpha = 0.9f),
                maxLines = 1,
                overflow = TextOverflow.Ellipsis,
            )
        }
        if (chain.summary.isNotBlank()) {
            Spacer(Modifier.height(6.dp))
            Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
                Text("Gestalt des Turns", fontSize = 10.sp, color = Color.White.copy(alpha = 0.55f))
                Text(chain.summary, fontSize = 10.sp, color = Color.White.copy(alpha = 0.55f))
            }
        }

        Spacer(Modifier.height(12.dp))
        ZurufBar(row.agent.displayName, session?.reachability) { text -> onCallOut(row, text) }
    }
}

/**
 * Say something into the running turn, without ending it.
 *
 * The button says **"geht raus"**, not "lenkt sofort", and that is the whole
 * point. What is provable is delivery: a known pubkey plus a `p` tag reaches
 * the agent. Whether the agent then interrupts its turn is decided by
 * `buzz-acp`'s own rules, which this app cannot read — so it is not promised.
 * Without an identity there is no field at all: an input box that cannot send
 * is worse than an honest absence.
 */
@Composable
private fun ZurufBar(
    agentName: String,
    reachability: SessionFacts.Reachability?,
    onSend: (String) -> Unit,
) {
    if (reachability != SessionFacts.Reachability.ADDRESSABLE) {
        Text(
            "Zuruf nicht möglich — für $agentName ist keine Buzz-Kennung lesbar.",
            fontSize = 10.sp,
            color = Color.White.copy(alpha = 0.5f),
        )
        return
    }
    var text by remember(agentName) { mutableStateOf("") }
    Row(
        Modifier
            .fillMaxWidth()
            .clip(RoundedCornerShape(22.dp))
            .background(Color.Black.copy(alpha = 0.28f))
            .padding(start = 14.dp, end = 5.dp, top = 5.dp, bottom = 5.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        BasicTextField(
            value = text,
            onValueChange = { text = it },
            singleLine = true,
            textStyle = monoStyle.copy(color = Color.White, fontSize = 12.sp),
            cursorBrush = SolidColor(Color.White),
            modifier = Modifier.weight(1f),
            decorationBox = { inner ->
                if (text.isEmpty()) {
                    Text(
                        "Zuruf an $agentName …",
                        style = monoStyle,
                        fontSize = 12.sp,
                        color = Color.White.copy(alpha = 0.45f),
                    )
                }
                inner()
            },
        )
        Spacer(Modifier.width(8.dp))
        val armed = text.isNotBlank()
        Text(
            "GEHT RAUS",
            style = labelStyle,
            color = if (armed) Color(0xFF07120C) else Color.White.copy(alpha = 0.45f),
            modifier = Modifier
                .clip(RoundedCornerShape(18.dp))
                .background(if (armed) Color(0xFF34D399) else Color.White.copy(alpha = 0.10f))
                .clickable(enabled = armed) {
                    onSend(text.trim())
                    text = ""
                }
                .padding(horizontal = 12.dp, vertical = 8.dp),
        )
    }
}

/** "Session 8,8 h · 531k von 1M · 0 Compacts" — or why none of that is known. */
internal fun sessionLine(session: SessionFacts?): String {
    if (session == null) return "Session unbekannt"
    session.absenceReason?.let { return it }
    val parts = buildList {
        session.ageSeconds?.let { add("Session ${duration(it)}") }
        val used = session.promptTokens
        if (used != null) {
            add(
                session.contextWindow?.let { "${short(used)} von ${short(it)}" }
                    ?: "${short(used)} Token",
            )
        }
        session.compacts?.let { add("$it ${if (it == 1) "Compact" else "Compacts"}") }
    }
    return parts.joinToString(" · ")
}

/** "Turn seit 6 min · Signal vor 4 s" — the two numbers that together prove work. */
internal fun turnLine(row: AgentRow): String {
    val pulse = row.pulse ?: return "Aktivität nicht messbar"
    val latest = pulse.latest ?: return "keine Tool-Calls im Fenster"
    val head = if (pulse.looksOpen) "Turn seit ${duration(latest.secondsAgo)}"
    else "zuletzt ${ago(latest.secondsAgo)}"
    val signal = pulse.lastSignalSecondsAgo
    val tail = when {
        signal == null -> "kein Signal gemessen"
        FocusPick.isStalled(row) -> "steht seit ${duration(signal)}"
        else -> "Signal ${ago(signal)}"
    }
    return "$head · $tail"
}

internal fun short(tokens: Int): String = when {
    tokens >= 1_000_000 -> {
        val tenths = tokens / 100_000
        if (tenths % 10 == 0) "${tenths / 10}M" else "${tenths / 10},${tenths % 10}M"
    }
    tokens >= 1_000 -> "${tokens / 1000}k"
    else -> tokens.toString()
}

/** The signature graphic: a ring whose fill is the session's context. */
@Composable
private fun ContextGauge(session: SessionFacts?) {
    val fraction = session?.fraction
    val tint = meterColor(session)
    Box(Modifier.size(70.dp), contentAlignment = Alignment.Center) {
        Box(
            Modifier
                .fillMaxSize()
                .clip(CircleShape)
                .background(
                    if (fraction == null) {
                        Brush.sweepGradient(
                            listOf(
                                Color.White.copy(alpha = 0.18f),
                                Color.Transparent,
                                Color.White.copy(alpha = 0.18f),
                            ),
                        )
                    } else {
                        val stop = fraction.toFloat().coerceIn(0.02f, 0.999f)
                        Brush.sweepGradient(
                            0f to tint,
                            stop to tint,
                            (stop + 0.001f) to Color.White.copy(alpha = 0.14f),
                            1f to Color.White.copy(alpha = 0.14f),
                        )
                    },
                ),
        )
        Box(
            Modifier.size(56.dp).clip(CircleShape).background(Color(0xFF2A1148)),
            contentAlignment = Alignment.Center,
        ) {
            Column(horizontalAlignment = Alignment.CenterHorizontally) {
                Text(
                    session?.percent?.toString() ?: "—",
                    color = Color.White,
                    fontSize = 18.sp,
                    fontWeight = FontWeight.Bold,
                )
                Text(
                    if (session?.percent != null) "PROZENT" else "KONTEXT",
                    style = labelStyle.copy(fontSize = 8.sp),
                    color = Color.White.copy(alpha = 0.6f),
                )
            }
        }
    }
}

/** The shape of the turn: seven glyphs, oldest left, the open one breathing. */
@Composable
private fun TurnChainRow(chain: TurnChain.Chain) {
    Row(
        Modifier
            .fillMaxWidth()
            .clip(RoundedCornerShape(12.dp))
            .background(Color.Black.copy(alpha = 0.24f))
            .padding(horizontal = 10.dp, vertical = 9.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        chain.links.forEachIndexed { index, link ->
            if (index > 0) {
                Box(Modifier.weight(1f).height(1.dp).background(Color.White.copy(alpha = 0.18f)))
            }
            Box(
                Modifier
                    .size(15.dp)
                    .then(if (link.open) Modifier.alpha(breath()) else Modifier)
                    .clip(RoundedCornerShape(5.dp))
                    .background(if (link.open) Color(0xFF34D399) else Color.White.copy(alpha = 0.16f)),
                contentAlignment = Alignment.Center,
            ) {
                Text(
                    link.glyph,
                    fontSize = 8.sp,
                    fontWeight = FontWeight.Bold,
                    color = if (link.open) Color(0xFF07120C) else Color.White.copy(alpha = 0.85f),
                )
            }
        }
        if (chain.more > 0) {
            Spacer(Modifier.width(6.dp))
            Text("+${chain.more}", fontSize = 9.sp, color = Color.White.copy(alpha = 0.5f))
        }
    }
}

@Composable
private fun BudgetFocusCard(
    choice: FocusPick.Choice,
    usage: List<AccountUsage>,
    rows: List<AgentRow>,
) {
    val tightest = usage.mapNotNull { entry -> entry.peakPercent?.let { entry to it } }
        .maxByOrNull { it.second }
    Column(
        Modifier
            .fillMaxWidth()
            .clip(RoundedCornerShape(DeckMetrics.heroRadius))
            .background(ALARM_BRUSH)
            .padding(17.dp),
    ) {
        Text(choice.reason.uppercase(), style = labelStyle, color = Color.White.copy(alpha = 0.58f))
        Spacer(Modifier.height(10.dp))
        Row(verticalAlignment = Alignment.CenterVertically) {
            Box(Modifier.size(70.dp), contentAlignment = Alignment.Center) {
                Text(
                    "${tightest?.second?.toInt() ?: 0}",
                    color = Color.White,
                    fontSize = 26.sp,
                    fontWeight = FontWeight.Bold,
                )
            }
            Spacer(Modifier.width(13.dp))
            Column(Modifier.weight(1f)) {
                Text(
                    tightest?.first?.displayName ?: "Budget",
                    color = Color.White,
                    fontSize = 19.sp,
                    fontWeight = FontWeight.Bold,
                )
                Text(
                    "${rows.count { it.pulse?.looksOpen == true }} Agenten arbeiten gerade",
                    color = Color.White.copy(alpha = 0.7f),
                    fontSize = 11.sp,
                )
                if (tightest?.first?.isStale == true) {
                    Text(
                        "Zwischenspeicher — kein frischer Messwert",
                        color = Color.White.copy(alpha = 0.7f),
                        fontSize = 11.sp,
                    )
                }
            }
        }
    }
}

@Composable
private fun RestingCard(pulse: DeckPulse?, fleetSize: Int) {
    val deck = LocalDeck.current
    Column(
        Modifier
            .fillMaxWidth()
            .clip(RoundedCornerShape(DeckMetrics.heroRadius))
            .background(deck.surface)
            .padding(24.dp),
        horizontalAlignment = Alignment.CenterHorizontally,
    ) {
        Text(
            if (pulse == null) "Noch nichts abgerufen" else "Niemand arbeitet gerade",
            color = deck.textPrimary,
            fontSize = 15.sp,
            fontWeight = FontWeight.SemiBold,
        )
        Spacer(Modifier.height(6.dp))
        Text(
            if (pulse == null) {
                "Oben antippen, um abzugleichen."
            } else {
                "$fleetSize Units laufen, keiner hat gerade etwas zu tun. " +
                    "Das ist ein Zustand, kein Fehler."
            },
            color = deck.textSecondary,
            fontSize = 12.sp,
            lineHeight = 18.sp,
        )
    }
}

// ------------------------------------------------------------------ stream

internal data class StreamEntry(
    val key: String,
    val who: String,
    val kind: String,
    val what: String,
    val ageSeconds: Int?,
    val tone: Tone,
    val mono: Boolean,
) {
    enum class Tone { LIVE, GOOD, BAD, PLAIN }
}

/**
 * The stream is the rail's own data re-sorted by time — no second fetch.
 * Agents contribute their newest call, the board contributes its events.
 */
internal fun buildStream(pulse: DeckPulse?, limit: Int = 12): List<StreamEntry> {
    if (pulse == null) return emptyList()
    val fromAgents = pulse.agentRows.mapNotNull { row ->
        val call = row.pulse?.latest ?: return@mapNotNull null
        StreamEntry(
            key = "agent-${row.agent.stem}",
            who = row.agent.displayName,
            kind = call.kind.label,
            what = call.label,
            ageSeconds = call.secondsAgo,
            tone = if (row.pulse.looksOpen) StreamEntry.Tone.LIVE else StreamEntry.Tone.PLAIN,
            mono = true,
        )
    }
    val fromBoard = pulse.recentEvents.map { event ->
        StreamEntry(
            key = "event-${event.id}",
            who = event.profile ?: "Kanban",
            kind = event.label,
            what = event.note ?: event.title,
            // Both stamps come off the server clock, so the subtraction is
            // sound. An event from a payload without a clock keeps no age at
            // all rather than borrowing the phone's.
            ageSeconds = (pulse.serverNow - event.at)
                .takeIf { pulse.serverNow > 0L && event.at > 0L && it >= 0L }
                ?.toInt(),
            tone = if (event.isFailure) StreamEntry.Tone.BAD else StreamEntry.Tone.GOOD,
            mono = false,
        )
    }
    // One stream, one order: everything that has a measured age sorts by it,
    // and anything unmeasurable sinks to the bottom instead of interleaving on
    // a guess.
    return (fromAgents + fromBoard).sortedBy { it.ageSeconds ?: Int.MAX_VALUE }.take(limit)
}

@Composable
private fun StreamRow(entry: StreamEntry) {
    val deck = LocalDeck.current
    val tint = when (entry.tone) {
        StreamEntry.Tone.LIVE, StreamEntry.Tone.GOOD -> deck.success
        StreamEntry.Tone.BAD -> deck.danger
        StreamEntry.Tone.PLAIN -> deck.textFaint
    }
    Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.Top) {
        Text(
            entry.ageSeconds?.let { ago(it) }.orEmpty(),
            style = tickerStyle,
            color = deck.textFaint,
            modifier = Modifier.width(62.dp),
        )
        Box(
            Modifier
                .padding(top = 5.dp)
                .size(7.dp)
                .then(if (entry.tone == StreamEntry.Tone.LIVE) Modifier.alpha(breath()) else Modifier)
                .clip(CircleShape)
                .background(tint),
        )
        Spacer(Modifier.width(11.dp))
        Column(Modifier.weight(1f).padding(bottom = 11.dp)) {
            Row(verticalAlignment = Alignment.Bottom) {
                Text(
                    entry.who,
                    color = deck.textPrimary,
                    fontSize = 12.sp,
                    fontWeight = FontWeight.SemiBold,
                )
                Spacer(Modifier.width(6.dp))
                Text(entry.kind, color = deck.textFaint, fontSize = 11.sp)
            }
            Text(
                entry.what,
                style = if (entry.mono) monoStyle else monoStyle.copy(fontFamily = null),
                color = deck.textSecondary,
                maxLines = 2,
                overflow = TextOverflow.Ellipsis,
            )
        }
    }
}

// ------------------------------------------------------------------ shared

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

/**
 * The one animation this app allows, in one place.
 *
 * Callers apply it only where a life sign is fresh, so it is bound to evidence
 * rather than to a heuristic. A pulse that always pulses is decoration, and
 * decoration on a monitoring screen is a lie with a heartbeat.
 */
@Composable
private fun breath(): Float {
    val transition = rememberInfiniteTransition(label = "puls")
    val alpha by transition.animateFloat(
        initialValue = 1f,
        targetValue = 0.32f,
        animationSpec = infiniteRepeatable(tween(1100), RepeatMode.Reverse),
        label = "atem",
    )
    return alpha
}

@Composable
internal fun KindChip(kind: ToolCall.Kind) {
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

/** "vor 12 s" — relative, always, never an absolute stamp on a card. */
internal fun ago(seconds: Int): String = when {
    seconds < 5 -> "gerade eben"
    seconds < 60 -> "vor $seconds s"
    seconds < 3600 -> "vor ${seconds / 60} min"
    seconds < 86_400 -> "vor ${seconds / 3600} h"
    else -> "vor ${seconds / 86_400} d"
}

/** "8 min" / "8,8 h" — a span, without the "vor". */
internal fun duration(seconds: Int): String = when {
    seconds < 60 -> "$seconds s"
    seconds < 3600 -> "${seconds / 60} min"
    seconds < 86_400 -> "${seconds / 3600},${(seconds % 3600) / 360} h"
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
