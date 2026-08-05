package net.hermes.deck.ui

import androidx.compose.foundation.background
import androidx.compose.foundation.border
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
import androidx.compose.foundation.lazy.LazyRow
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Share
import androidx.compose.material3.Icon
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.graphics.vector.rememberVectorPainter
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import coil3.compose.AsyncImage
import java.time.LocalDate
import net.hermes.deck.model.Attachment
import net.hermes.deck.model.DueDates
import net.hermes.deck.model.TaskPriority
import net.hermes.deck.model.TaskStatus

/**
 * The deck's building blocks. One card treatment, one chip treatment, one
 * accent — repeated everywhere, so the app reads as a single surface rather
 * than a stack of Material defaults.
 */

/**
 * The week as tiles: the deck's calendar, and the only place a date is picked.
 *
 * A tile carries its own load — the number of open tasks due that day — so the
 * strip answers "when is it busy" before anything is tapped. Selecting a day is
 * a toggle; tapping the selected one again clears the filter.
 */
@Composable
fun DueStrip(
    days: List<LocalDate>,
    counts: Map<LocalDate, Int>,
    selected: LocalDate?,
    today: LocalDate,
    onSelect: (LocalDate) -> Unit,
) {
    val deck = LocalDeck.current
    LazyRow(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
        items(days, key = { it.toString() }) { day ->
            val isSelected = day == selected
            val isToday = day == today
            val load = counts[day] ?: 0
            val shape = RoundedCornerShape(18.dp)
            Column(
                Modifier
                    .width(54.dp)
                    .clip(shape)
                    .background(if (isSelected) deck.accent else deck.surface)
                    .border(
                        1.dp,
                        when {
                            isSelected -> Color.Transparent
                            isToday -> deck.accent.copy(alpha = 0.55f)
                            else -> deck.outline
                        },
                        shape,
                    )
                    .clickable { onSelect(day) }
                    .padding(vertical = 10.dp),
                horizontalAlignment = Alignment.CenterHorizontally,
            ) {
                Text(
                    DueDates.weekdayShort(day).uppercase(),
                    color = if (isSelected) Color.White.copy(alpha = 0.85f) else deck.textFaint,
                    fontSize = 10.sp,
                    fontWeight = FontWeight.SemiBold,
                )
                Spacer(Modifier.height(3.dp))
                Text(
                    day.dayOfMonth.toString(),
                    color = when {
                        isSelected -> Color.White
                        isToday -> deck.accent
                        else -> deck.textPrimary
                    },
                    fontSize = 17.sp,
                    fontWeight = FontWeight.Bold,
                )
                Spacer(Modifier.height(5.dp))
                // A load indicator, not a badge: the count matters up to a
                // glanceable few, beyond that only "a lot" does. The slot keeps
                // its height when empty — otherwise only the tiles that carry a
                // number grow, and the strip comes out ragged.
                Box(Modifier.height(14.dp), contentAlignment = Alignment.Center) {
                    if (load > 0) {
                        Text(
                            if (load > 9) "9+" else load.toString(),
                            color = if (isSelected) Color.White else deck.accent,
                            fontSize = 10.sp,
                            fontWeight = FontWeight.Bold,
                        )
                    }
                }
            }
        }
    }
}

/**
 * Picking a date without a calendar dialog.
 *
 * A deck task is due today, tomorrow, or later this week — almost never on a
 * date worth two taps and a modal. The four chips cover that, and clearing is
 * one of them rather than a hidden gesture.
 */
@Composable
fun DueChips(selected: LocalDate?, today: LocalDate, onPick: (LocalDate?) -> Unit) {
    val options = listOf<Pair<String, LocalDate?>>(
        "Heute" to today,
        "Morgen" to today.plusDays(1),
        "Diese Woche" to today.plusDays((7 - today.dayOfWeek.value).coerceAtLeast(1).toLong()),
        "Kein Termin" to null,
    )
    Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
        options.forEach { (label, date) ->
            val isSelected = selected == date
            Box(
                Modifier
                    .clip(RoundedCornerShape(DeckMetrics.chipRadius))
                    .background(if (isSelected) LocalDeck.current.accentSoft else LocalDeck.current.surfaceRaised)
                    .clickable { onPick(date) }
                    .padding(horizontal = 12.dp, vertical = 7.dp),
            ) {
                Text(
                    label,
                    color = if (isSelected) LocalDeck.current.accent else LocalDeck.current.textFaint,
                    fontSize = 12.sp,
                )
            }
        }
    }
}

/**
 * An attachment as it actually looks.
 *
 * Images carry their own preview; anything else keeps the icon. The URL points
 * at Buzz's Blossom store, which is only reachable inside the tailnet — a
 * failed load therefore has to degrade to the icon rather than to a broken
 * image, because "not on the network right now" is the normal case.
 */
@Composable
fun AttachmentTile(attachment: Attachment, size: Int = 64) {
    val deck = LocalDeck.current
    val shape = RoundedCornerShape(16.dp)
    Box(
        Modifier
            .size(size.dp)
            .clip(shape)
            .background(deck.surfaceRaised)
            .border(1.dp, deck.outline, shape),
        contentAlignment = Alignment.Center,
    ) {
        if (attachment.isImage) {
            AsyncImage(
                model = attachment.url,
                contentDescription = attachment.name,
                contentScale = ContentScale.Crop,
                modifier = Modifier.fillMaxSize(),
                error = rememberVectorPainter(Icons.Filled.Share),
                placeholder = rememberVectorPainter(Icons.Filled.Share),
            )
        } else {
            Icon(
                Icons.Filled.Share,
                contentDescription = null,
                tint = deck.textSecondary,
                modifier = Modifier.size(18.dp),
            )
        }
    }
}

/** A raised panel: slightly lighter than the ground, hairline edge, big radius. */
@Composable
fun GlassCard(
    modifier: Modifier = Modifier,
    accent: Color? = null,
    onClick: (() -> Unit)? = null,
    content: @Composable () -> Unit,
) {
    val deck = LocalDeck.current
    val shape = RoundedCornerShape(DeckMetrics.cardRadius)
    Box(
        modifier = modifier
            .clip(shape)
            .background(deck.surface)
            .border(1.dp, deck.outline, shape)
            .let { if (onClick != null) it.clickable(onClick = onClick) else it },
    ) {
        // The accent is a bar, not a border: a full outline in the project colour
        // would compete with the card's own edge and muddy the palette.
        if (accent != null) {
            Box(
                Modifier
                    .width(4.dp)
                    .height(56.dp)
                    .padding(top = 0.dp)
                    .background(accent, RoundedCornerShape(topStart = 4.dp, bottomStart = 4.dp))
                    .align(Alignment.TopStart),
            )
        }
        Box(Modifier.padding(DeckMetrics.cardPadding)) { content() }
    }
}

@Composable
fun SectionHeader(title: String, action: String? = null, onAction: (() -> Unit)? = null) {
    val deck = LocalDeck.current
    Row(
        Modifier.fillMaxWidth().padding(bottom = DeckMetrics.gap),
        horizontalArrangement = Arrangement.SpaceBetween,
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Text(title, color = deck.textPrimary, fontSize = 18.sp, fontWeight = FontWeight.Bold)
        if (action != null) {
            Text(
                action,
                color = deck.textSecondary,
                fontSize = 13.sp,
                modifier = Modifier.let { if (onAction != null) it.clickable(onClick = onAction) else it },
            )
        }
    }
}

@Composable
fun StatusPill(status: TaskStatus, modifier: Modifier = Modifier) {
    val deck = LocalDeck.current
    val tint = when (status) {
        TaskStatus.INBOX -> deck.textSecondary
        TaskStatus.OPEN -> deck.info
        TaskStatus.DOING -> deck.accent
        TaskStatus.WAITING -> deck.warning
        TaskStatus.DONE -> deck.success
    }
    Box(
        modifier
            .clip(RoundedCornerShape(DeckMetrics.chipRadius))
            .background(tint.copy(alpha = 0.16f))
            .padding(horizontal = 10.dp, vertical = 4.dp),
    ) {
        Text(status.label, color = tint, fontSize = 11.sp, fontWeight = FontWeight.SemiBold)
    }
}

@Composable
fun PriorityMark(priority: TaskPriority) {
    val deck = LocalDeck.current
    if (priority == TaskPriority.NORMAL) return
    val tint = if (priority == TaskPriority.URGENT) deck.danger else deck.warning
    Box(
        Modifier
            .clip(RoundedCornerShape(DeckMetrics.chipRadius))
            .background(tint.copy(alpha = 0.16f))
            .padding(horizontal = 8.dp, vertical = 3.dp),
    ) {
        Text(priority.label, color = tint, fontSize = 10.sp, fontWeight = FontWeight.Bold)
    }
}

/** A small labelled figure, used in the hero and the project cards. */
@Composable
fun MetricLine(icon: ImageVector, text: String, tint: Color? = null) {
    val deck = LocalDeck.current
    Row(verticalAlignment = Alignment.CenterVertically) {
        Icon(icon, contentDescription = null, tint = tint ?: deck.textFaint, modifier = Modifier.size(14.dp))
        Spacer(Modifier.width(6.dp))
        Text(text, color = tint ?: deck.textSecondary, fontSize = 12.sp)
    }
}

/**
 * The initial badge that stands in for an avatar. Buzz profiles have pictures,
 * but fetching them would mean an image request per agent on every render; the
 * colour is derived from the key so it is still recognisable at a glance.
 */
@Composable
fun InitialBadge(label: String, key: String, size: Int = 28) {
    val deck = LocalDeck.current
    val tint = deck.edgeFor(key)
    Box(
        Modifier
            .size(size.dp)
            .clip(CircleShape)
            .background(tint.copy(alpha = 0.22f))
            .border(1.dp, tint.copy(alpha = 0.5f), CircleShape),
        contentAlignment = Alignment.Center,
    ) {
        Text(
            label.take(1).uppercase(),
            color = tint,
            fontSize = (size * 0.42).sp,
            fontWeight = FontWeight.Bold,
        )
    }
}

/** The gradient panel at the top of the deck. */
@Composable
fun HeroPanel(
    title: String,
    subtitle: String,
    modifier: Modifier = Modifier,
    /** Tints the subtitle when it carries a failure rather than a status. */
    warn: Boolean = false,
    content: @Composable () -> Unit,
) {
    val deck = LocalDeck.current
    val shape = RoundedCornerShape(DeckMetrics.heroRadius)
    Box(
        modifier
            .fillMaxWidth()
            .clip(shape)
            .background(deck.heroBrush)
            .border(1.dp, Color.White.copy(alpha = 0.10f), shape)
            .padding(DeckMetrics.cardPadding + 4.dp),
    ) {
        Column {
            Text(title, color = Color.White, fontSize = 22.sp, fontWeight = FontWeight.Bold)
            Spacer(Modifier.height(4.dp))
            Text(
                subtitle,
                color = if (warn) Color(0xFFFFD9A0) else Color.White.copy(alpha = 0.72f),
                fontSize = 13.sp,
            )
            Spacer(Modifier.height(DeckMetrics.gap + 4.dp))
            content()
        }
    }
}

/** A pill in the hero's row of figures. [highlighted] gets the solid accent. */
@Composable
fun StatPill(caption: String, value: String, highlighted: Boolean, onClick: (() -> Unit)? = null) {
    val shape = RoundedCornerShape(18.dp)
    val background: Brush = if (highlighted) {
        LocalDeck.current.accentBrush
    } else {
        Brush.linearGradient(listOf(Color.White.copy(alpha = 0.07f), Color.White.copy(alpha = 0.07f)))
    }
    Column(
        modifier = Modifier
            .clip(shape)
            .background(background)
            .border(1.dp, Color.White.copy(alpha = if (highlighted) 0f else 0.12f), shape)
            .let { if (onClick != null) it.clickable(onClick = onClick) else it }
            .padding(horizontal = 14.dp, vertical = 10.dp),
        horizontalAlignment = Alignment.CenterHorizontally,
    ) {
        Text(
            caption.uppercase(),
            color = Color.White.copy(alpha = 0.65f),
            fontSize = 10.sp,
            fontWeight = FontWeight.SemiBold,
        )
        Spacer(Modifier.height(2.dp))
        Text(value, color = Color.White, fontSize = 18.sp, fontWeight = FontWeight.Bold)
    }
}

@Composable
fun EmptyHint(text: String) {
    val deck = LocalDeck.current
    Box(
        Modifier.fillMaxWidth().padding(vertical = 32.dp),
        contentAlignment = Alignment.Center,
    ) {
        Text(text, color = deck.textFaint, fontSize = 13.sp)
    }
}
