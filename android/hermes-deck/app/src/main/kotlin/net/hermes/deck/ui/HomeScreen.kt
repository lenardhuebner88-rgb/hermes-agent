package net.hermes.deck.ui

import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
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
import androidx.compose.foundation.lazy.LazyRow
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.BasicTextField
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Check
import androidx.compose.material.icons.filled.DateRange
import androidx.compose.material.icons.filled.Refresh
import androidx.compose.material.icons.filled.Search
import androidx.compose.material.icons.filled.Settings
import androidx.compose.material.icons.filled.Share
import androidx.compose.material3.Icon
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.text.SpanStyle
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import java.util.concurrent.TimeUnit
import net.hermes.deck.DeckViewModel
import net.hermes.deck.model.Channel
import net.hermes.deck.model.DeckTask
import net.hermes.deck.model.TaskStatus

@Composable
fun HomeScreen(
    viewModel: DeckViewModel,
    onOpenTask: (DeckTask) -> Unit,
    onOpenSettings: () -> Unit,
) {
    val deck = LocalDeck.current
    val snapshot by viewModel.snapshot.collectAsState()
    val syncState by viewModel.syncState.collectAsState()
    val search by viewModel.search.collectAsState()
    val filter by viewModel.filterChannel.collectAsState()
    val decisions by viewModel.decisions.collectAsState()

    // Decisions live in ordinary channel chatter, so they can only be found by
    // scanning; do it once per screen entry rather than on every recomposition.
    LaunchedEffect(snapshot.channels.size) {
        if (snapshot.channels.isNotEmpty()) viewModel.loadDecisions()
    }

    val channels = snapshot.channels
    val byId = channels.associateBy { it.id }
    val visible = viewModel.visibleTasks(snapshot.tasks, filter, search)
    val open = snapshot.tasks.count { !it.status.isClosed }
    val urgent = snapshot.tasks.count { !it.status.isClosed && it.priority.ordinal >= 2 }
    val doneToday = snapshot.tasks.count { it.status == TaskStatus.DONE }

    LazyColumn(
        Modifier
            .fillMaxSize()
            .windowInsetsPadding(WindowInsets.statusBars)
            .padding(horizontal = DeckMetrics.screenPadding),
    ) {
        item {
            Spacer(Modifier.height(8.dp))
            Header(
                name = viewModel.settings.pubkeyHex?.take(8) ?: "Deck",
                syncing = syncState is net.hermes.deck.data.DeckRepository.SyncState.Running,
                pending = snapshot.pendingCount,
                onSync = { viewModel.sync() },
                onSettings = onOpenSettings,
            )
            Spacer(Modifier.height(DeckMetrics.gap + 4.dp))
            SearchField(search) { viewModel.setSearch(it) }
            Spacer(Modifier.height(DeckMetrics.gap + 8.dp))
        }

        item {
            val failure = (syncState as? net.hermes.deck.data.DeckRepository.SyncState.Failed)?.reason
            HeroPanel(
                title = when (open) {
                    0 -> "Nichts offen"
                    1 -> "1 offener Punkt"
                    else -> "$open offene Punkte"
                },
                // A failed sync has to stay on screen. As a transient message it
                // vanished after four seconds and left a screen that looked
                // exactly like "nothing captured yet" — measured on the device.
                subtitle = when {
                    failure != null -> "Abgleich fehlgeschlagen: $failure"
                    syncState is net.hermes.deck.data.DeckRepository.SyncState.Running ->
                        "Gleicht mit Buzz ab …"
                    snapshot.pendingCount > 0 ->
                        "${snapshot.pendingCount} wartet auf Verbindung zu Buzz"
                    snapshot.lastSyncAt == 0L -> "Noch nicht abgeglichen"
                    else -> "Abgeglichen ${relativeTime(snapshot.lastSyncAt)}"
                },
                warn = failure != null,
            ) {
                Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                    StatPill("Offen", open.toString(), highlighted = true) {
                        viewModel.setFilterChannel(null)
                    }
                    StatPill("Dringend", urgent.toString(), highlighted = false)
                    StatPill("Erledigt", doneToday.toString(), highlighted = false)
                    StatPill("Projekte", channels.size.toString(), highlighted = false)
                }
            }
            Spacer(Modifier.height(DeckMetrics.gap + 12.dp))
        }

        // Hidden while searching or filtering: the decisions block is tall, and
        // it pushed the actual search result off the screen — seen on device.
        val showDecisions = decisions.isNotEmpty() && search.isBlank() && filter == null
        if (showDecisions) {
            item {
                SectionHeader(title = "Warten auf dich", action = "${decisions.size}")
            }
            items(decisions.take(MAX_DECISIONS), key = { it.eventId }) { decision ->
                GlassCard(Modifier.fillMaxWidth()) {
                    Column {
                        Row(verticalAlignment = Alignment.CenterVertically) {
                            Box(
                                Modifier
                                    .width(3.dp)
                                    .height(20.dp)
                                    .clip(RoundedCornerShape(2.dp))
                                    .background(deck.warning),
                            )
                            Spacer(Modifier.width(10.dp))
                            Text(
                                decision.channelName,
                                color = deck.textFaint,
                                fontSize = 11.sp,
                                fontWeight = FontWeight.SemiBold,
                            )
                        }
                        Spacer(Modifier.height(8.dp))
                        Text(
                            decision.text,
                            color = deck.textPrimary,
                            fontSize = 14.sp,
                            maxLines = 4,
                            overflow = TextOverflow.Ellipsis,
                        )
                        Spacer(Modifier.height(10.dp))
                        Row(verticalAlignment = Alignment.CenterVertically) {
                            Box(
                                Modifier
                                    .clip(RoundedCornerShape(DeckMetrics.chipRadius))
                                    .background(deck.success.copy(alpha = 0.16f))
                                    .clickable { viewModel.approve(decision) }
                                    .padding(horizontal = 14.dp, vertical = 7.dp),
                            ) {
                                Text(
                                    "Freigeben",
                                    color = deck.success,
                                    fontSize = 12.sp,
                                    fontWeight = FontWeight.SemiBold,
                                )
                            }
                            Spacer(Modifier.width(10.dp))
                            Text(relativeTime(decision.askedAt), color = deck.textFaint, fontSize = 11.sp)
                        }
                    }
                }
                Spacer(Modifier.height(DeckMetrics.gap - 2.dp))
            }
            if (decisions.size > MAX_DECISIONS) {
                item {
                    Text(
                        "und ${decisions.size - MAX_DECISIONS} weitere",
                        color = deck.textFaint,
                        fontSize = 12.sp,
                        modifier = Modifier.padding(bottom = DeckMetrics.gap),
                    )
                }
            }
            item { Spacer(Modifier.height(DeckMetrics.gap)) }
        }

        if (channels.isNotEmpty()) {
            item {
                SectionHeader(
                    title = "Projekte",
                    action = if (filter == null) null else "Filter aufheben",
                    onAction = { viewModel.setFilterChannel(null) },
                )
            }
            item {
                LazyRow(horizontalArrangement = Arrangement.spacedBy(DeckMetrics.gap)) {
                    items(channels, key = { it.id }) { channel ->
                        ProjectCard(
                            channel = channel,
                            openCount = snapshot.tasks.count {
                                it.channelId == channel.id && !it.status.isClosed
                            },
                            selected = filter == channel.id,
                            onClick = {
                                viewModel.setFilterChannel(if (filter == channel.id) null else channel.id)
                            },
                        )
                    }
                }
                Spacer(Modifier.height(DeckMetrics.gap + 12.dp))
            }
        }

        item {
            SectionHeader(
                title = filter?.let { byId[it]?.displayName } ?: "Alle Aufgaben",
                action = "${visible.size}",
            )
        }

        if (visible.isEmpty()) {
            item {
                EmptyHint(
                    if (snapshot.tasks.isEmpty()) {
                        "Noch nichts erfasst — tipp auf +"
                    } else {
                        "Nichts gefunden"
                    },
                )
            }
        }

        items(visible, key = { it.id }) { task ->
            TaskRow(
                task = task,
                channel = byId[task.channelId],
                onClick = { onOpenTask(task) },
                onToggleDone = {
                    viewModel.setStatus(
                        task,
                        if (task.status.isClosed) TaskStatus.OPEN else TaskStatus.DONE,
                    )
                },
            )
            Spacer(Modifier.height(DeckMetrics.gap - 2.dp))
        }

        item { Spacer(Modifier.height(bottomBarSpace)) }
    }
}

@Composable
private fun Header(
    name: String,
    syncing: Boolean,
    pending: Int,
    onSync: () -> Unit,
    onSettings: () -> Unit,
) {
    val deck = LocalDeck.current
    Row(
        Modifier.fillMaxWidth(),
        horizontalArrangement = Arrangement.SpaceBetween,
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            InitialBadge(label = "P", key = name, size = 42)
            Spacer(Modifier.width(10.dp))
            Column {
                Text(greeting(), color = deck.textSecondary, fontSize = 12.sp)
                Text("Hermes Deck", color = deck.textPrimary, fontSize = 18.sp, fontWeight = FontWeight.Bold)
            }
        }
        Row(verticalAlignment = Alignment.CenterVertically) {
            IconBubble(Icons.Filled.Refresh, if (syncing) "Gleicht ab" else "Abgleichen", onSync, active = syncing)
            Spacer(Modifier.width(8.dp))
            IconBubble(Icons.Filled.Settings, "Einstellungen", onSettings, badged = pending > 0)
        }
    }
}

@Composable
private fun IconBubble(
    icon: androidx.compose.ui.graphics.vector.ImageVector,
    description: String,
    onClick: () -> Unit,
    active: Boolean = false,
    badged: Boolean = false,
) {
    val deck = LocalDeck.current
    Box {
        Box(
            Modifier
                .size(40.dp)
                .clip(CircleShape)
                .background(deck.surface)
                .border(1.dp, deck.outline, CircleShape)
                .clickable(onClick = onClick),
            contentAlignment = Alignment.Center,
        ) {
            Icon(
                icon,
                contentDescription = description,
                tint = if (active) deck.accent else deck.textSecondary,
                modifier = Modifier.size(18.dp),
            )
        }
        if (badged) {
            Box(
                Modifier
                    .align(Alignment.TopEnd)
                    .size(9.dp)
                    .clip(CircleShape)
                    .background(deck.warning),
            )
        }
    }
}

@Composable
private fun SearchField(value: String, onChange: (String) -> Unit) {
    val deck = LocalDeck.current
    val shape = RoundedCornerShape(18.dp)
    Row(
        Modifier
            .fillMaxWidth()
            .clip(shape)
            .background(deck.surface)
            .border(1.dp, deck.outline, shape)
            .padding(horizontal = 14.dp, vertical = 12.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Icon(Icons.Filled.Search, contentDescription = null, tint = deck.textFaint, modifier = Modifier.size(18.dp))
        Spacer(Modifier.width(10.dp))
        Box(Modifier.weight(1f)) {
            if (value.isEmpty()) {
                Text("Aufgabe suchen …", color = deck.textFaint, fontSize = 14.sp)
            }
            BasicTextField(
                value = value,
                onValueChange = onChange,
                singleLine = true,
                textStyle = TextStyle(color = deck.textPrimary, fontSize = 14.sp),
                cursorBrush = deck.accentBrush,
                modifier = Modifier.fillMaxWidth(),
            )
        }
    }
}

@Composable
private fun ProjectCard(channel: Channel, openCount: Int, selected: Boolean, onClick: () -> Unit) {
    val deck = LocalDeck.current
    val edge = deck.edgeFor(channel.id)
    val shape = RoundedCornerShape(DeckMetrics.cardRadius)
    Box(
        Modifier
            .width(190.dp)
            .clip(shape)
            .background(if (selected) deck.surfaceRaised else deck.surface)
            .border(1.dp, if (selected) edge.copy(alpha = 0.55f) else deck.outline, shape)
            .clickable(onClick = onClick)
            .padding(DeckMetrics.cardPadding),
    ) {
        Column {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Box(
                    Modifier
                        .width(3.dp)
                        .height(26.dp)
                        .clip(RoundedCornerShape(2.dp))
                        .background(edge),
                )
                Spacer(Modifier.width(10.dp))
                Text(
                    channel.displayName,
                    color = deck.textPrimary,
                    fontSize = 15.sp,
                    fontWeight = FontWeight.SemiBold,
                    maxLines = 2,
                    overflow = TextOverflow.Ellipsis,
                )
            }
            Spacer(Modifier.height(10.dp))
            Text(
                channel.topic ?: channel.about ?: "Kein Thema hinterlegt",
                color = deck.textFaint,
                fontSize = 11.sp,
                maxLines = 2,
                overflow = TextOverflow.Ellipsis,
            )
            Spacer(Modifier.height(12.dp))
            MetricLine(
                icon = Icons.Filled.Check,
                text = if (openCount == 0) "nichts offen" else "$openCount offen",
                tint = if (openCount == 0) null else edge,
            )
        }
    }
}

@Composable
fun TaskRow(
    task: DeckTask,
    channel: Channel?,
    onClick: () -> Unit,
    onToggleDone: () -> Unit,
) {
    val deck = LocalDeck.current
    val edge = deck.edgeFor(task.channelId)
    GlassCard(modifier = Modifier.fillMaxWidth(), onClick = onClick) {
        Row(verticalAlignment = Alignment.Top) {
            Box(
                Modifier
                    .width(3.dp)
                    .height(38.dp)
                    .clip(RoundedCornerShape(2.dp))
                    .background(edge),
            )
            Spacer(Modifier.width(12.dp))
            Column(Modifier.weight(1f)) {
                Text(
                    task.title,
                    color = if (task.status.isClosed) deck.textFaint else deck.textPrimary,
                    fontSize = 15.sp,
                    fontWeight = FontWeight.SemiBold,
                    maxLines = 2,
                    overflow = TextOverflow.Ellipsis,
                )
                Spacer(Modifier.height(8.dp))
                Row(
                    verticalAlignment = Alignment.CenterVertically,
                    horizontalArrangement = Arrangement.spacedBy(8.dp),
                ) {
                    StatusPill(task.status)
                    PriorityMark(task.priority)
                    if (channel != null) {
                        Text(channel.displayName, color = deck.textFaint, fontSize = 11.sp)
                    }
                }
                if (task.replyCount > 0 || task.hasAttachments || task.due != null) {
                    Spacer(Modifier.height(8.dp))
                    Row(horizontalArrangement = Arrangement.spacedBy(14.dp)) {
                        if (task.replyCount > 0) {
                            MetricLine(Icons.Filled.Share, "${task.replyCount} Antworten")
                        }
                        if (task.hasAttachments) {
                            MetricLine(Icons.Filled.Share, "${task.attachments.size} Anhänge")
                        }
                        task.due?.let { MetricLine(Icons.Filled.DateRange, it) }
                    }
                }
            }
            Spacer(Modifier.width(8.dp))
            Box(
                Modifier
                    .size(30.dp)
                    .clip(CircleShape)
                    .background(if (task.status.isClosed) deck.success.copy(alpha = 0.18f) else deck.surfaceRaised)
                    .border(
                        1.dp,
                        if (task.status.isClosed) deck.success.copy(alpha = 0.5f) else deck.outline,
                        CircleShape,
                    )
                    .clickable(onClick = onToggleDone),
                contentAlignment = Alignment.Center,
            ) {
                Icon(
                    Icons.Filled.Check,
                    contentDescription = if (task.status.isClosed) "Wieder öffnen" else "Erledigt",
                    tint = if (task.status.isClosed) deck.success else deck.textFaint,
                    modifier = Modifier.size(16.dp),
                )
            }
        }
    }
}

private fun greeting(): String {
    val hour = java.util.Calendar.getInstance().get(java.util.Calendar.HOUR_OF_DAY)
    return when {
        hour < 5 -> "Noch wach 🌙"
        hour < 11 -> "Guten Morgen 👋"
        hour < 18 -> "Guten Tag 👋"
        else -> "Guten Abend 👋"
    }
}

fun relativeTime(epochSeconds: Long): String {
    if (epochSeconds <= 0) return "nie"
    val delta = System.currentTimeMillis() / 1000 - epochSeconds
    return when {
        delta < 60 -> "gerade eben"
        delta < TimeUnit.HOURS.toSeconds(1) -> "vor ${delta / 60} Min."
        delta < TimeUnit.DAYS.toSeconds(1) -> "vor ${delta / 3600} Std."
        else -> "vor ${delta / 86400} Tagen"
    }
}

/** Keeps the deck readable: the rest stay one tap away behind the count. */
private const val MAX_DECISIONS = 3
