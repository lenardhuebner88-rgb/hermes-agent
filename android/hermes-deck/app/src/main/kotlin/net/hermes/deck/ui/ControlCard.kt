package net.hermes.deck.ui

import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Refresh
import androidx.compose.material3.Icon
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import kotlin.math.roundToInt
import net.hermes.deck.model.ControlSummary
import net.hermes.deck.model.ThreadActivity

/**
 * The first thing on the first screen: the state of the whole system, in four
 * bands, above the fold.
 *
 * Scope is set by what Buzz cannot do. The chat shows conversations one at a
 * time, so it can never say how much is open across thirteen channels, that an
 * agent is grinding through tool calls without writing a word, how close a
 * subscription is to its ceiling, or whether the thing just captured actually
 * arrived. Those four are exactly the bands here. Reading a conversation stays
 * in Buzz, which is better at it.
 *
 * Height is a constraint, not an outcome. Twice already this screen pushed its
 * own content below the fold — once with three unclamped approvals, once with a
 * decisions block that buried the search result — so every band is one line and
 * a band with nothing to say renders nothing at all.
 */
@Composable
fun ControlCard(
    summary: ControlSummary,
    onShowDecisions: () -> Unit,
    onShowOverdue: () -> Unit,
    onShowToday: () -> Unit,
    onSync: () -> Unit,
) {
    val deck = LocalDeck.current
    GlassCard(Modifier.fillMaxWidth()) {
        Column {
            PulseBand(summary.pulse)

            if (!summary.waiting.isEmpty) {
                Spacer(Modifier.height(12.dp))
                Divider()
                Spacer(Modifier.height(12.dp))
                WaitingBand(summary.waiting, onShowDecisions, onShowOverdue, onShowToday)
            }

            summary.capacity?.let { capacity ->
                Spacer(Modifier.height(12.dp))
                Divider()
                Spacer(Modifier.height(12.dp))
                CapacityBand(capacity)
            }

            Spacer(Modifier.height(12.dp))
            Divider()
            Spacer(Modifier.height(10.dp))
            DeliveryBand(summary.delivery, onSync)
        }
    }
}

@Composable
private fun Divider() {
    Box(
        Modifier
            .fillMaxWidth()
            .height(1.dp)
            .background(LocalDeck.current.outline),
    )
}

@Composable
private fun PulseBand(pulse: ControlSummary.Pulse) {
    val deck = LocalDeck.current
    Row(verticalAlignment = Alignment.CenterVertically) {
        Box(
            Modifier
                .size(8.dp)
                .clip(CircleShape)
                .background(if (pulse.working.isNotEmpty()) deck.success else deck.textFaint),
        )
        Spacer(Modifier.width(8.dp))
        Column(Modifier.weight(1f)) {
            Text(
                pulse.headline,
                color = deck.textPrimary,
                fontSize = 15.sp,
                fontWeight = FontWeight.SemiBold,
            )
            // Names, not just a count: "3 arbeiten" is a number, "Claude, Qwen,
            // Hermes arbeiten" is an answer.
            val detail = listOfNotNull(
                pulse.working.takeIf { it.isNotEmpty() }?.joinToString(", "),
                pulse.rest,
            ).joinToString(" · ")
            if (detail.isNotBlank()) {
                Spacer(Modifier.height(2.dp))
                Text(
                    detail,
                    color = deck.textFaint,
                    fontSize = 11.sp,
                    maxLines = 1,
                    overflow = TextOverflow.Ellipsis,
                )
            }
        }
        if (pulse.activeThreads > 0) {
            Spacer(Modifier.width(10.dp))
            Box(
                Modifier
                    .clip(RoundedCornerShape(DeckMetrics.chipRadius))
                    .background(deck.accentSoft)
                    .padding(horizontal = 10.dp, vertical = 5.dp),
            ) {
                Text(
                    "${pulse.activeThreads} aktiv",
                    color = deck.accent,
                    fontSize = 11.sp,
                    fontWeight = FontWeight.SemiBold,
                )
            }
        }
    }
}

@Composable
private fun WaitingBand(
    waiting: ControlSummary.Waiting,
    onShowDecisions: () -> Unit,
    onShowOverdue: () -> Unit,
    onShowToday: () -> Unit,
) {
    val deck = LocalDeck.current
    Column {
        Text(
            "Wartet auf dich",
            color = deck.textSecondary,
            fontSize = 11.sp,
            fontWeight = FontWeight.SemiBold,
        )
        Spacer(Modifier.height(8.dp))
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            if (waiting.decisions > 0) {
                WaitChip("Freigaben", waiting.decisions, deck.warning, onShowDecisions)
            }
            if (waiting.overdue > 0) {
                WaitChip("Überfällig", waiting.overdue, deck.danger, onShowOverdue)
            }
            if (waiting.dueToday > 0) {
                WaitChip("Heute", waiting.dueToday, deck.accent, onShowToday)
            }
            if (waiting.mentions > 0) {
                // No action: reading the mention is Buzz's job, and a chip that
                // opened a half-rendered conversation here would be worse.
                WaitChip("Erwähnt", waiting.mentions, deck.textSecondary, null)
            }
        }
    }
}

@Composable
private fun WaitChip(label: String, count: Int, tint: Color, onClick: (() -> Unit)?) {
    Row(
        Modifier
            .clip(RoundedCornerShape(DeckMetrics.chipRadius))
            .background(tint.copy(alpha = 0.14f))
            .let { if (onClick != null) it.clickable(onClick = onClick) else it }
            .padding(horizontal = 11.dp, vertical = 7.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Text("$count", color = tint, fontSize = 13.sp, fontWeight = FontWeight.Bold)
        Spacer(Modifier.width(5.dp))
        Text(label, color = tint, fontSize = 11.sp)
    }
}

@Composable
private fun CapacityBand(capacity: ControlSummary.Capacity) {
    val deck = LocalDeck.current
    val tint = when {
        capacity.percent >= 85 -> deck.danger
        capacity.percent >= 60 -> deck.warning
        else -> deck.success
    }
    Column {
        Row(verticalAlignment = Alignment.CenterVertically) {
            Text(
                // The tightest budget, named. Which subscription will bite
                // first is the only usage question worth a line up here.
                "Knappstes Budget · ${capacity.provider}",
                color = deck.textSecondary,
                fontSize = 11.sp,
                fontWeight = FontWeight.SemiBold,
                modifier = Modifier.weight(1f),
            )
            Text(
                "${capacity.percent.roundToInt()} %",
                color = tint,
                fontSize = 13.sp,
                fontWeight = FontWeight.Bold,
            )
        }
        Spacer(Modifier.height(6.dp))
        Box(
            Modifier
                .fillMaxWidth()
                .height(4.dp)
                .clip(RoundedCornerShape(2.dp))
                .background(deck.accentSoft),
        ) {
            Box(
                Modifier
                    .fillMaxWidth((capacity.percent / 100.0).toFloat().coerceIn(0f, 1f))
                    .height(4.dp)
                    .clip(RoundedCornerShape(2.dp))
                    .background(tint),
            )
        }
        val note = listOfNotNull(
            if (capacity.others > 0) "${capacity.others} weitere" else null,
            if (capacity.stale) "Zwischenspeicher" else null,
        ).joinToString(" · ")
        if (note.isNotBlank()) {
            Spacer(Modifier.height(4.dp))
            Text(note, color = deck.textFaint, fontSize = 10.sp)
        }
    }
}

@Composable
private fun DeliveryBand(delivery: ControlSummary.Delivery, onSync: () -> Unit) {
    val deck = LocalDeck.current
    // A failed sync outranks everything else here. Shown as a transient message
    // it vanished after four seconds and left a screen that looked exactly like
    // "nothing captured yet" — measured on the device.
    val (text, tint) = when {
        delivery.failure != null -> "Abgleich fehlgeschlagen: ${delivery.failure}" to deck.danger
        delivery.syncing -> "Gleicht mit Buzz ab …" to deck.textSecondary
        delivery.pending > 0 ->
            "${delivery.pending} wartet auf Verbindung zu Buzz" to deck.warning
        delivery.lastSyncAt == 0L -> "Noch nicht abgeglichen" to deck.warning
        else -> "Abgeglichen ${relativeTime(delivery.lastSyncAt)}" to deck.textFaint
    }
    Row(verticalAlignment = Alignment.CenterVertically) {
        Text(
            text,
            color = tint,
            fontSize = 11.sp,
            maxLines = 1,
            overflow = TextOverflow.Ellipsis,
            modifier = Modifier.weight(1f),
        )
        Spacer(Modifier.width(8.dp))
        Icon(
            Icons.Filled.Refresh,
            contentDescription = "Neu abgleichen",
            tint = deck.textSecondary,
            modifier = Modifier
                .size(16.dp)
                .clickable(onClick = onSync),
        )
    }
}

/**
 * Where words are being exchanged right now, across every channel at once.
 *
 * This is the list Buzz structurally cannot show: it renders one conversation
 * per screen, so "where is something happening" means opening each channel in
 * turn. The row deliberately carries only enough to decide whether to switch to
 * Buzz — who spoke, in which project, how long ago, and the first line.
 */
@Composable
fun ActiveThreadRow(entry: ThreadActivity.Entry, onClick: () -> Unit) {
    val deck = LocalDeck.current
    GlassCard(Modifier.fillMaxWidth(), onClick = onClick) {
        Column {
            Row(verticalAlignment = Alignment.CenterVertically) {
                InitialBadge(entry.lastAuthorLabel, entry.lastAuthorPubkey, size = 24)
                Spacer(Modifier.width(9.dp))
                Text(
                    entry.lastAuthorLabel,
                    color = deck.textPrimary,
                    fontSize = 12.sp,
                    fontWeight = FontWeight.SemiBold,
                )
                Spacer(Modifier.width(7.dp))
                Text(
                    "· ${entry.channelName}",
                    color = deck.textFaint,
                    fontSize = 11.sp,
                    maxLines = 1,
                    overflow = TextOverflow.Ellipsis,
                    modifier = Modifier.weight(1f),
                )
                Spacer(Modifier.width(7.dp))
                Text(relativeTime(entry.lastAt), color = deck.textFaint, fontSize = 11.sp)
            }
            Spacer(Modifier.height(6.dp))
            Text(
                entry.title,
                color = deck.textSecondary,
                fontSize = 12.sp,
                fontWeight = FontWeight.Medium,
                maxLines = 1,
                overflow = TextOverflow.Ellipsis,
            )
            if (entry.lastText.isNotBlank()) {
                Spacer(Modifier.height(3.dp))
                Text(
                    entry.lastText,
                    color = deck.textFaint,
                    fontSize = 11.sp,
                    lineHeight = 15.sp,
                    maxLines = 2,
                    overflow = TextOverflow.Ellipsis,
                )
            }
        }
    }
}
