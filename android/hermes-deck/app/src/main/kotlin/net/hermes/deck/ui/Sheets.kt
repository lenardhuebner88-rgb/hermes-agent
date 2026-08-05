package net.hermes.deck.ui

import android.net.Uri
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyRow
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.BasicTextField
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Add
import androidx.compose.material.icons.filled.DateRange
import androidx.compose.material.icons.filled.Send
import androidx.compose.material.icons.filled.Share
import androidx.compose.material3.Icon
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import java.time.LocalDate
import kotlinx.coroutines.launch
import net.hermes.deck.DeckViewModel
import net.hermes.deck.data.displayNameOf
import net.hermes.deck.model.Attachment
import net.hermes.deck.model.DeckTask
import net.hermes.deck.model.DueDates
import net.hermes.deck.model.TaskPriority

@Composable
fun CaptureSheet(
    viewModel: DeckViewModel,
    onDismiss: () -> Unit,
    initialTitle: String = "",
    initialBody: String = "",
    initialAttachments: List<Attachment> = emptyList(),
) {
    val deck = LocalDeck.current
    val context = LocalContext.current
    val scope = rememberCoroutineScope()
    val snapshot by viewModel.snapshot.collectAsState()
    val filter by viewModel.filterChannel.collectAsState()

    var title by remember { mutableStateOf(initialTitle) }
    var body by remember { mutableStateOf(initialBody) }
    var priority by remember { mutableStateOf(TaskPriority.NORMAL) }
    var attachments by remember { mutableStateOf(initialAttachments) }
    var uploading by remember { mutableStateOf(false) }
    var submitting by remember { mutableStateOf(false) }
    val today = LocalDate.now()
    var due by remember { mutableStateOf<LocalDate?>(null) }
    var channelId by remember {
        mutableStateOf(
            filter
                ?: viewModel.settings.inboxChannelId.ifBlank { null }
                ?: snapshot.channels.firstOrNull()?.id
                ?: "",
        )
    }

    // The share activity builds its own view model, so at first composition the
    // channels are still loading from disk and the initial pick above lands on
    // "". Without this the sheet opens with no project selected and a dead
    // "Ablegen" button, and nothing ever says why.
    LaunchedEffect(snapshot.channels) {
        if (channelId.isBlank()) {
            channelId = viewModel.settings.inboxChannelId.ifBlank { null }
                ?: snapshot.channels.firstOrNull()?.id
                ?: ""
        }
    }

    val picker = rememberLauncherForActivityResult(ActivityResultContracts.GetContent()) { uri: Uri? ->
        if (uri == null) return@rememberLauncherForActivityResult
        uploading = true
        scope.launch {
            val resolver = context.contentResolver
            val mime = resolver.getType(uri) ?: "application/octet-stream"
            val bytes = runCatching {
                resolver.openInputStream(uri)?.use { it.readBytes() }
            }.getOrNull()
            if (bytes == null) {
                uploading = false
                viewModel.say("Datei konnte nicht gelesen werden.")
                return@launch
            }
            viewModel.uploadAttachment(bytes, mime, resolver.displayNameOf(uri))
                .onSuccess { attachments = attachments + it }
                .onFailure { viewModel.say(it.message ?: "Upload fehlgeschlagen") }
            uploading = false
        }
    }

    SheetScaffold(title = "Neu erfassen", onDismiss = onDismiss) {
        Column(Modifier.verticalScroll(rememberScrollState())) {
            MultilineField(
                placeholder = "Worum geht es?",
                value = title,
                minHeight = 24,
                large = true,
            ) { title = it }
            Spacer(Modifier.height(DeckMetrics.gap))
            MultilineField(placeholder = "Details, Plan, Kontext …", value = body, minHeight = 90) { body = it }

            Spacer(Modifier.height(DeckMetrics.gap + 4.dp))
            Text("PROJEKT", color = deck.textFaint, fontSize = 10.sp, fontWeight = FontWeight.SemiBold)
            Spacer(Modifier.height(8.dp))
            LazyRow(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                items(snapshot.channels, key = { it.id }) { channel ->
                    val selected = channel.id == channelId
                    Box(
                        Modifier
                            .clip(RoundedCornerShape(DeckMetrics.chipRadius))
                            .background(if (selected) deck.accentSoft else deck.surfaceRaised)
                            .border(
                                1.dp,
                                if (selected) deck.accent.copy(alpha = 0.6f) else deck.outline,
                                RoundedCornerShape(DeckMetrics.chipRadius),
                            )
                            .clickable { channelId = channel.id }
                            .padding(horizontal = 12.dp, vertical = 7.dp),
                    ) {
                        Text(
                            channel.displayName,
                            color = if (selected) deck.textPrimary else deck.textSecondary,
                            fontSize = 12.sp,
                        )
                    }
                }
            }

            Spacer(Modifier.height(DeckMetrics.gap + 4.dp))
            Text("PRIORITÄT", color = deck.textFaint, fontSize = 10.sp, fontWeight = FontWeight.SemiBold)
            Spacer(Modifier.height(8.dp))
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                TaskPriority.entries.forEach { option ->
                    val selected = option == priority
                    Box(
                        Modifier
                            .clip(RoundedCornerShape(DeckMetrics.chipRadius))
                            .background(if (selected) deck.accentSoft else deck.surfaceRaised)
                            .clickable { priority = option }
                            .padding(horizontal = 12.dp, vertical = 7.dp),
                    ) {
                        Text(
                            option.label,
                            color = if (selected) deck.accent else deck.textFaint,
                            fontSize = 12.sp,
                        )
                    }
                }
            }

            Spacer(Modifier.height(DeckMetrics.gap + 4.dp))
            Text("FÄLLIG", color = deck.textFaint, fontSize = 10.sp, fontWeight = FontWeight.SemiBold)
            Spacer(Modifier.height(8.dp))
            DueChips(selected = due, today = today) { due = it }

            Spacer(Modifier.height(DeckMetrics.gap + 4.dp))
            AttachmentRow(attachments, uploading) { picker.launch("*/*") }

            Spacer(Modifier.height(DeckMetrics.gap + 8.dp))
            PrimaryButton(
                text = when {
                    submitting -> "Wird abgelegt …"
                    uploading -> "Anhang lädt …"
                    else -> "Ablegen"
                },
                enabled = title.isNotBlank() && channelId.isNotBlank() && !uploading && !submitting,
            ) {
                // Dismiss only once the capture has signed, stored and pushed.
                // The share path finishes its activity in onDismiss, and that
                // cancels the viewModelScope: dismissing first threw the note
                // away before it was ever written — silently, because the
                // sheet closed exactly as it does on success.
                submitting = true
                viewModel.capture(
                    channelId = channelId,
                    title = title,
                    body = body,
                    priority = priority,
                    attachments = attachments,
                    due = due?.let { DueDates.format(it) },
                ) {
                    submitting = false
                    onDismiss()
                }
            }
            if (channelId.isBlank()) {
                Spacer(Modifier.height(8.dp))
                Text(
                    "Noch kein Projekt geladen — erst abgleichen.",
                    color = deck.warning,
                    fontSize = 11.sp,
                )
            }
            Spacer(Modifier.height(20.dp))
        }
    }
}

@Composable
fun TaskDetailSheet(viewModel: DeckViewModel, task: DeckTask, onDismiss: () -> Unit) {
    val deck = LocalDeck.current
    val snapshot by viewModel.snapshot.collectAsState()
    val channel = snapshot.channels.firstOrNull { it.id == task.channelId }
    val thread by viewModel.thread.collectAsState()
    val threadLoading by viewModel.threadLoading.collectAsState()
    val me = viewModel.settings.pubkeyHex
    var note by remember { mutableStateOf("") }
    var answer by remember { mutableStateOf("") }

    LaunchedEffect(task.id) { viewModel.loadThread(task) }

    SheetScaffold(title = channel?.displayName ?: "Aufgabe", onDismiss = onDismiss) {
        Column(Modifier.verticalScroll(rememberScrollState())) {
            Text(task.title, color = deck.textPrimary, fontSize = 19.sp, fontWeight = FontWeight.Bold)
            if (task.body.isNotBlank()) {
                Spacer(Modifier.height(8.dp))
                Text(task.body, color = deck.textSecondary, fontSize = 14.sp)
            }

            DueDates.label(task.due, LocalDate.now())?.let { label ->
                Spacer(Modifier.height(10.dp))
                val late = !task.status.isClosed && DueDates.isOverdue(task.due, LocalDate.now())
                MetricLine(
                    Icons.Filled.DateRange,
                    if (late) "Überfällig — war $label fällig" else "Fällig $label",
                    tint = if (late) deck.danger else deck.accent,
                )
            }

            Spacer(Modifier.height(DeckMetrics.gap + 4.dp))
            StatusChooser(task.status) { viewModel.setStatus(task, it) }

            Spacer(Modifier.height(DeckMetrics.gap))
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                TaskPriority.entries.forEach { option ->
                    val selected = option == task.priority
                    Box(
                        Modifier
                            .clip(RoundedCornerShape(DeckMetrics.chipRadius))
                            .background(if (selected) deck.accentSoft else deck.surfaceRaised)
                            .clickable { viewModel.setPriority(task, option) }
                            .padding(horizontal = 10.dp, vertical = 6.dp),
                    ) {
                        Text(
                            option.label,
                            color = if (selected) deck.accent else deck.textFaint,
                            fontSize = 11.sp,
                        )
                    }
                }
            }

            Spacer(Modifier.height(DeckMetrics.gap))
            DueChips(
                selected = DueDates.parse(task.due),
                today = LocalDate.now(),
            ) { viewModel.setDue(task, it) }

            if (task.attachments.isNotEmpty()) {
                Spacer(Modifier.height(DeckMetrics.gap + 4.dp))
                AttachmentRow(task.attachments, uploading = false, onAdd = null)
            }

            Spacer(Modifier.height(DeckMetrics.gap + 8.dp))
            Text(
                if (thread.isEmpty()) "VERLAUF" else "VERLAUF (${thread.size})",
                color = deck.textFaint,
                fontSize = 10.sp,
                fontWeight = FontWeight.SemiBold,
            )
            Spacer(Modifier.height(8.dp))
            when {
                threadLoading -> Text("Lädt aus Buzz …", color = deck.textFaint, fontSize = 12.sp)
                thread.isEmpty() -> Text("Noch keine Antworten.", color = deck.textFaint, fontSize = 12.sp)
                else -> thread.forEach { event ->
                    val mine = event.pubkey == me
                    Box(
                        Modifier
                            .fillMaxWidth()
                            .padding(bottom = 8.dp)
                            .clip(RoundedCornerShape(14.dp))
                            .background(if (mine) deck.accentSoft else deck.surfaceRaised)
                            .padding(horizontal = 12.dp, vertical = 10.dp),
                    ) {
                        Column {
                            Text(
                                if (mine) "Du" else event.pubkey.take(8),
                                color = if (mine) deck.accent else deck.edgeFor(event.pubkey),
                                fontSize = 10.sp,
                                fontWeight = FontWeight.SemiBold,
                            )
                            Spacer(Modifier.height(4.dp))
                            Text(event.content, color = deck.textSecondary, fontSize = 13.sp)
                        }
                    }
                }
            }
            Spacer(Modifier.height(8.dp))
            MultilineField(placeholder = "Antworten …", value = answer, minHeight = 44) { answer = it }
            Spacer(Modifier.height(8.dp))
            SecondaryButton("Antwort in den Thread") {
                viewModel.replyTo(task, answer)
                answer = ""
            }

            Spacer(Modifier.height(DeckMetrics.gap + 8.dp))
            Text("AN EINEN AGENTEN GEBEN", color = deck.textFaint, fontSize = 10.sp, fontWeight = FontWeight.SemiBold)
            Spacer(Modifier.height(6.dp))
            Text(
                "Postet einen Auftrag in den Thread dieser Aufgabe und erwähnt den Agenten.",
                color = deck.textFaint,
                fontSize = 11.sp,
            )
            Spacer(Modifier.height(8.dp))
            MultilineField(placeholder = "Auftrag (optional)", value = note, minHeight = 56) { note = it }
            Spacer(Modifier.height(8.dp))

            val members = channel?.memberPubkeys.orEmpty()
                .filter { it != viewModel.settings.pubkeyHex }
            if (members.isEmpty()) {
                Text(
                    "Keine anderen Mitglieder in diesem Kanal bekannt.",
                    color = deck.textFaint,
                    fontSize = 11.sp,
                )
            } else {
                LazyRow(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    items(members, key = { it }) { pubkey ->
                        // Buzz stores display names in kind:0 profiles, which the
                        // deck does not sync yet; a short key is honest, a guessed
                        // name would not be. Tracked as a follow-up.
                        val name = pubkey.take(8)
                        Box(
                            Modifier
                                .clip(RoundedCornerShape(DeckMetrics.chipRadius))
                                .background(deck.surfaceRaised)
                                .border(1.dp, deck.outline, RoundedCornerShape(DeckMetrics.chipRadius))
                                .clickable { viewModel.handOver(task, pubkey, name, note) }
                                .padding(horizontal = 12.dp, vertical = 8.dp),
                        ) {
                            Row(verticalAlignment = Alignment.CenterVertically) {
                                Icon(
                                    Icons.Filled.Send,
                                    contentDescription = null,
                                    tint = deck.accent,
                                    modifier = Modifier.size(14.dp),
                                )
                                Spacer(Modifier.width(6.dp))
                                Text(name, color = deck.textSecondary, fontSize = 12.sp)
                            }
                        }
                    }
                }
            }

            Spacer(Modifier.height(DeckMetrics.gap + 8.dp))
            Text(
                "Thread-ID ${task.id.take(12)}… · ${task.replyCount} Antworten",
                color = deck.textFaint,
                fontSize = 11.sp,
            )
            Spacer(Modifier.height(24.dp))
        }
    }
}

@Composable
private fun AttachmentRow(attachments: List<Attachment>, uploading: Boolean, onAdd: (() -> Unit)?) {
    val deck = LocalDeck.current
    Column {
        Text("ANHÄNGE", color = deck.textFaint, fontSize = 10.sp, fontWeight = FontWeight.SemiBold)
        Spacer(Modifier.height(8.dp))
        LazyRow(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            if (onAdd != null) {
                items(listOf("add")) {
                    Box(
                        Modifier
                            .size(64.dp)
                            .clip(RoundedCornerShape(16.dp))
                            .background(deck.surfaceRaised)
                            .border(1.dp, deck.outline, RoundedCornerShape(16.dp))
                            .clickable(enabled = !uploading, onClick = onAdd),
                        contentAlignment = Alignment.Center,
                    ) {
                        Icon(
                            Icons.Filled.Add,
                            contentDescription = "Anhang hinzufügen",
                            tint = if (uploading) deck.textFaint else deck.accent,
                            modifier = Modifier.size(20.dp),
                        )
                    }
                }
            }
            items(attachments, key = { it.sha256 + it.url }) { attachment ->
                Column(
                    Modifier.width(64.dp),
                    horizontalAlignment = Alignment.CenterHorizontally,
                ) {
                    AttachmentTile(attachment)
                    Spacer(Modifier.height(4.dp))
                    Text(
                        attachment.name,
                        color = deck.textFaint,
                        fontSize = 9.sp,
                        maxLines = 1,
                        overflow = TextOverflow.Ellipsis,
                    )
                }
            }
        }
    }
}

@Composable
private fun MultilineField(
    placeholder: String,
    value: String,
    minHeight: Int,
    large: Boolean = false,
    onChange: (String) -> Unit,
) {
    val deck = LocalDeck.current
    val shape = RoundedCornerShape(16.dp)
    Box(
        Modifier
            .fillMaxWidth()
            .clip(shape)
            .background(deck.surface)
            .border(1.dp, deck.outline, shape)
            .padding(horizontal = 14.dp, vertical = 12.dp),
    ) {
        if (value.isEmpty()) {
            Text(placeholder, color = deck.textFaint, fontSize = if (large) 17.sp else 14.sp)
        }
        BasicTextField(
            value = value,
            onValueChange = onChange,
            textStyle = TextStyle(
                color = deck.textPrimary,
                fontSize = if (large) 17.sp else 14.sp,
                fontWeight = if (large) FontWeight.SemiBold else FontWeight.Normal,
            ),
            cursorBrush = deck.accentBrush,
            modifier = Modifier.fillMaxWidth().heightIn(min = minHeight.dp),
        )
    }
}
