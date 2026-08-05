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
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.imePadding
import androidx.compose.foundation.layout.navigationBarsPadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.statusBars
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.layout.windowInsetsPadding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.BasicTextField
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Check
import androidx.compose.material.icons.filled.Close
import androidx.compose.material.icons.filled.Refresh
import androidx.compose.material3.Icon
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import net.hermes.deck.DeckViewModel
import net.hermes.deck.model.BuzzAgent
import net.hermes.deck.model.DeckTask
import net.hermes.deck.model.TaskStatus

@Composable
fun ProjectsScreen(viewModel: DeckViewModel, onOpenTask: (DeckTask) -> Unit) {
    val deck = LocalDeck.current
    val snapshot by viewModel.snapshot.collectAsState()

    LazyColumn(
        Modifier
            .fillMaxSize()
            .windowInsetsPadding(WindowInsets.statusBars)
            .padding(horizontal = DeckMetrics.screenPadding),
    ) {
        item {
            Spacer(Modifier.height(12.dp))
            Text("Projekte", color = deck.textPrimary, fontSize = 24.sp, fontWeight = FontWeight.Bold)
            Text(
                "Jeder Kanal aus Buzz, in dem du Mitglied bist",
                color = deck.textSecondary,
                fontSize = 13.sp,
            )
            Spacer(Modifier.height(DeckMetrics.gap + 8.dp))
        }

        if (snapshot.channels.isEmpty()) {
            item { EmptyHint("Noch keine Kanäle geladen — oben abgleichen") }
        }

        items(snapshot.channels, key = { it.id }) { channel ->
            val tasks = snapshot.tasks.filter { it.channelId == channel.id }
            val open = tasks.count { !it.status.isClosed }
            GlassCard(
                modifier = Modifier.fillMaxWidth(),
                onClick = { viewModel.setFilterChannel(channel.id) },
            ) {
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Box(
                        Modifier
                            .width(3.dp)
                            .height(36.dp)
                            .clip(RoundedCornerShape(2.dp))
                            .background(deck.edgeFor(channel.id)),
                    )
                    Spacer(Modifier.width(12.dp))
                    Column(Modifier.weight(1f)) {
                        Text(
                            channel.displayName,
                            color = deck.textPrimary,
                            fontSize = 16.sp,
                            fontWeight = FontWeight.SemiBold,
                        )
                        channel.topic?.let {
                            Spacer(Modifier.height(3.dp))
                            Text(
                                it,
                                color = deck.textFaint,
                                fontSize = 11.sp,
                                maxLines = 2,
                                overflow = TextOverflow.Ellipsis,
                            )
                        }
                    }
                    Text(
                        if (open == 0) "—" else open.toString(),
                        color = if (open == 0) deck.textFaint else deck.accent,
                        fontSize = 18.sp,
                        fontWeight = FontWeight.Bold,
                    )
                }
            }
            Spacer(Modifier.height(DeckMetrics.gap - 2.dp))
        }

        item { Spacer(Modifier.height(bottomBarSpace)) }
    }
}

@Composable
fun AgentsScreen(viewModel: DeckViewModel) {
    val deck = LocalDeck.current
    val state by viewModel.agents.collectAsState()
    var expanded by remember { mutableStateOf<String?>(null) }

    LaunchedEffect(Unit) { viewModel.loadAgents() }

    LazyColumn(
        Modifier
            .fillMaxSize()
            .windowInsetsPadding(WindowInsets.statusBars)
            .padding(horizontal = DeckMetrics.screenPadding),
    ) {
        item {
            Spacer(Modifier.height(12.dp))
            Row(
                Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.SpaceBetween,
                verticalAlignment = Alignment.CenterVertically,
            ) {
                Column {
                    Text("Agenten", color = deck.textPrimary, fontSize = 24.sp, fontWeight = FontWeight.Bold)
                    Text("Modell setzen und Zustand sehen", color = deck.textSecondary, fontSize = 13.sp)
                }
                Box(
                    Modifier
                        .size(40.dp)
                        .clip(CircleShape)
                        .background(deck.surface)
                        .border(1.dp, deck.outline, CircleShape)
                        .clickable { viewModel.loadAgents() },
                    contentAlignment = Alignment.Center,
                ) {
                    Icon(
                        Icons.Filled.Refresh,
                        contentDescription = "Neu laden",
                        tint = if (state.loading) deck.accent else deck.textSecondary,
                        modifier = Modifier.size(18.dp),
                    )
                }
            }
            Spacer(Modifier.height(DeckMetrics.gap + 8.dp))
        }

        state.error?.let { error ->
            item {
                GlassCard(Modifier.fillMaxWidth()) {
                    Text(error, color = deck.warning, fontSize = 13.sp)
                }
                Spacer(Modifier.height(DeckMetrics.gap))
            }
        }

        if (state.agents.isEmpty() && state.error == null) {
            item { EmptyHint(if (state.loading) "Lädt …" else "Keine Agenten gemeldet") }
        }

        items(state.agents, key = { it.stem }) { agent ->
            AgentCard(
                agent = agent,
                expanded = expanded == agent.stem,
                models = if (expanded == agent.stem) state.models else emptyList(),
                modelsError = if (expanded == agent.stem) state.modelsError else null,
                modelsLoading = expanded == agent.stem && state.modelsLoading,
                busy = state.busyStem == agent.stem,
                onToggle = {
                    expanded = if (expanded == agent.stem) null else agent.stem
                    if (expanded == agent.stem) viewModel.loadModels(agent.stem)
                },
                onPick = { viewModel.setModel(agent.stem, it) },
            )
            Spacer(Modifier.height(DeckMetrics.gap - 2.dp))
        }

        item { Spacer(Modifier.height(bottomBarSpace)) }
    }
}

@Composable
private fun AgentCard(
    agent: BuzzAgent,
    expanded: Boolean,
    models: List<String>,
    modelsError: String?,
    modelsLoading: Boolean,
    busy: Boolean,
    onToggle: () -> Unit,
    onPick: (String) -> Unit,
) {
    val deck = LocalDeck.current
    val stateTint = when {
        agent.isRunning -> deck.success
        agent.isFailed -> deck.danger
        else -> deck.textFaint
    }
    GlassCard(Modifier.fillMaxWidth(), onClick = onToggle) {
        Column {
            Row(verticalAlignment = Alignment.CenterVertically) {
                InitialBadge(agent.displayName, agent.stem, size = 34)
                Spacer(Modifier.width(12.dp))
                Column(Modifier.weight(1f)) {
                    Text(
                        agent.displayName,
                        color = deck.textPrimary,
                        fontSize = 16.sp,
                        fontWeight = FontWeight.SemiBold,
                    )
                    Spacer(Modifier.height(2.dp))
                    Text(agent.model, color = deck.accent, fontSize = 12.sp)
                }
                Box(
                    Modifier
                        .size(9.dp)
                        .clip(CircleShape)
                        .background(stateTint),
                )
            }
            Spacer(Modifier.height(8.dp))
            Text(
                // The start time is the honest part: a changed model only takes
                // effect from the next start, so both are shown together.
                "${agent.agentCommand} · seit ${agent.lastStart.ifBlank { "unbekannt" }}",
                color = deck.textFaint,
                fontSize = 11.sp,
            )

            if (expanded) {
                Spacer(Modifier.height(DeckMetrics.gap))
                if (busy) {
                    Text("Wird gesetzt und neu gestartet …", color = deck.warning, fontSize = 12.sp)
                } else if (modelsLoading) {
                    // The probe starts a real agent to ask it — several seconds
                    // in which the card would otherwise look like a dud tap.
                    Text("Modelle werden abgefragt …", color = deck.textSecondary, fontSize = 12.sp)
                } else {
                    modelsError?.let {
                        Text(it, color = deck.warning, fontSize = 11.sp)
                        Spacer(Modifier.height(8.dp))
                    }
                    models.forEach { model ->
                        val active = model == agent.model
                        Row(
                            Modifier
                                .fillMaxWidth()
                                .clip(RoundedCornerShape(12.dp))
                                .background(if (active) deck.accentSoft else Color.Transparent)
                                .clickable(enabled = !active) { onPick(model) }
                                .padding(horizontal = 10.dp, vertical = 9.dp),
                            verticalAlignment = Alignment.CenterVertically,
                        ) {
                            Text(
                                model,
                                color = if (active) deck.textPrimary else deck.textSecondary,
                                fontSize = 13.sp,
                                modifier = Modifier.weight(1f),
                            )
                            if (active) {
                                Icon(
                                    Icons.Filled.Check,
                                    contentDescription = "aktiv",
                                    tint = deck.accent,
                                    modifier = Modifier.size(16.dp),
                                )
                            }
                        }
                    }
                }
            }
        }
    }
}

@Composable
fun SettingsScreen(viewModel: DeckViewModel) {
    val deck = LocalDeck.current
    val settings = viewModel.settings
    val snapshot by viewModel.snapshot.collectAsState()

    var relay by remember { mutableStateOf(settings.relayBaseUrl) }
    var hermes by remember { mutableStateOf(settings.hermesBaseUrl) }
    var user by remember { mutableStateOf(settings.hermesUsername) }
    var password by remember { mutableStateOf(settings.hermesPassword) }

    Column(
        Modifier
            .fillMaxSize()
            .windowInsetsPadding(WindowInsets.statusBars)
            .verticalScroll(rememberScrollState())
            .padding(horizontal = DeckMetrics.screenPadding),
    ) {
        Spacer(Modifier.height(12.dp))
        Text("Einstellungen", color = deck.textPrimary, fontSize = 24.sp, fontWeight = FontWeight.Bold)
        Spacer(Modifier.height(DeckMetrics.gap + 8.dp))

        GlassCard(Modifier.fillMaxWidth()) {
            Column {
                Text("Buzz", color = deck.textPrimary, fontSize = 15.sp, fontWeight = FontWeight.SemiBold)
                Spacer(Modifier.height(10.dp))
                LabelledField("Relay", relay) { relay = it }
                Spacer(Modifier.height(10.dp))
                Text(
                    "Identität: ${settings.pubkeyHex?.take(16) ?: "keine"}…",
                    color = deck.textFaint,
                    fontSize = 11.sp,
                )
                Spacer(Modifier.height(4.dp))
                Text(
                    if (snapshot.pendingCount > 0) {
                        "${snapshot.pendingCount} Einträge warten auf Übertragung"
                    } else {
                        "Alles übertragen · zuletzt ${relativeTime(snapshot.lastSyncAt)}"
                    },
                    color = if (snapshot.pendingCount > 0) deck.warning else deck.textFaint,
                    fontSize = 11.sp,
                )
            }
        }
        Spacer(Modifier.height(DeckMetrics.gap))

        GlassCard(Modifier.fillMaxWidth()) {
            Column {
                Text("Hermes-Dashboard", color = deck.textPrimary, fontSize = 15.sp, fontWeight = FontWeight.SemiBold)
                Spacer(Modifier.height(4.dp))
                Text("Nur nötig, um Agenten-Modelle zu ändern", color = deck.textFaint, fontSize = 11.sp)
                Spacer(Modifier.height(10.dp))
                LabelledField("Adresse", hermes) { hermes = it }
                Spacer(Modifier.height(8.dp))
                LabelledField("Benutzer", user) { user = it }
                Spacer(Modifier.height(8.dp))
                LabelledField("Passwort", password, secret = true) { password = it }
            }
        }
        Spacer(Modifier.height(DeckMetrics.gap))

        PrimaryButton("Sichern") {
            settings.relayBaseUrl = relay
            settings.hermesBaseUrl = hermes
            settings.hermesUsername = user
            settings.hermesPassword = password
            viewModel.say("Gesichert.")
            viewModel.sync()
        }
        Spacer(Modifier.height(DeckMetrics.gap))
        SecondaryButton("Schlüssel vom Gerät entfernen") {
            viewModel.forgetIdentity()
            viewModel.say("Schlüssel entfernt.")
        }
        Spacer(Modifier.height(bottomBarSpace))
    }
}

@Composable
fun OnboardingScreen(viewModel: DeckViewModel) {
    val deck = LocalDeck.current
    var key by remember { mutableStateOf("") }
    var relay by remember { mutableStateOf(viewModel.settings.relayBaseUrl) }
    var error by remember { mutableStateOf<String?>(null) }

    Column(
        Modifier
            .fillMaxSize()
            .background(deck.background)
            .windowInsetsPadding(WindowInsets.statusBars)
            .verticalScroll(rememberScrollState())
            .padding(DeckMetrics.screenPadding),
    ) {
        Spacer(Modifier.height(40.dp))
        Text("Hermes Deck", color = deck.textPrimary, fontSize = 30.sp, fontWeight = FontWeight.Bold)
        Spacer(Modifier.height(8.dp))
        Text(
            "Ideen und Aufgaben landen als Buzz-Threads — sichtbar in deiner Buzz-App, adressierbar für die Agenten.",
            color = deck.textSecondary,
            fontSize = 14.sp,
        )
        Spacer(Modifier.height(28.dp))

        GlassCard(Modifier.fillMaxWidth()) {
            Column {
                LabelledField("Relay", relay) { relay = it }
                Spacer(Modifier.height(12.dp))
                LabelledField("Schlüssel (nsec1… oder 64 Hex)", key, secret = true) {
                    key = it
                    error = null
                }
                error?.let {
                    Spacer(Modifier.height(8.dp))
                    Text(it, color = deck.danger, fontSize = 12.sp)
                }
                Spacer(Modifier.height(6.dp))
                Text(
                    "Der Schlüssel bleibt verschlüsselt auf dem Gerät und verlässt es nie.",
                    color = deck.textFaint,
                    fontSize = 11.sp,
                )
            }
        }

        Spacer(Modifier.height(DeckMetrics.gap + 8.dp))
        PrimaryButton("Verbinden", enabled = key.isNotBlank()) {
            runCatching { viewModel.importKey(relay, key) }
                .onFailure { error = it.message ?: "Schlüssel nicht lesbar" }
        }
    }
}

@Composable
fun LabelledField(
    label: String,
    value: String,
    secret: Boolean = false,
    onChange: (String) -> Unit,
) {
    val deck = LocalDeck.current
    val shape = RoundedCornerShape(14.dp)
    Column {
        Text(label.uppercase(), color = deck.textFaint, fontSize = 10.sp, fontWeight = FontWeight.SemiBold)
        Spacer(Modifier.height(6.dp))
        Box(
            Modifier
                .fillMaxWidth()
                .clip(shape)
                .background(deck.surfaceRaised)
                .border(1.dp, deck.outline, shape)
                .padding(horizontal = 12.dp, vertical = 11.dp),
        ) {
            BasicTextField(
                value = value,
                onValueChange = onChange,
                singleLine = true,
                textStyle = TextStyle(color = deck.textPrimary, fontSize = 14.sp),
                cursorBrush = deck.accentBrush,
                visualTransformation = if (secret) {
                    androidx.compose.ui.text.input.PasswordVisualTransformation()
                } else {
                    androidx.compose.ui.text.input.VisualTransformation.None
                },
                modifier = Modifier.fillMaxWidth(),
            )
        }
    }
}

@Composable
fun PrimaryButton(text: String, enabled: Boolean = true, onClick: () -> Unit) {
    val deck = LocalDeck.current
    val shape = RoundedCornerShape(18.dp)
    Box(
        Modifier
            .fillMaxWidth()
            .clip(shape)
            .background(if (enabled) deck.accentBrush else androidx.compose.ui.graphics.SolidColor(deck.surfaceRaised))
            .clickable(enabled = enabled, onClick = onClick)
            .padding(vertical = 15.dp),
        contentAlignment = Alignment.Center,
    ) {
        Text(
            text,
            color = if (enabled) Color.White else deck.textFaint,
            fontSize = 15.sp,
            fontWeight = FontWeight.SemiBold,
        )
    }
}

@Composable
fun SecondaryButton(text: String, onClick: () -> Unit) {
    val deck = LocalDeck.current
    val shape = RoundedCornerShape(18.dp)
    Box(
        Modifier
            .fillMaxWidth()
            .clip(shape)
            .background(deck.surface)
            .border(1.dp, deck.outline, shape)
            .clickable(onClick = onClick)
            .padding(vertical = 14.dp),
        contentAlignment = Alignment.Center,
    ) {
        Text(text, color = deck.textSecondary, fontSize = 14.sp)
    }
}

@Composable
fun SheetScaffold(title: String, onDismiss: () -> Unit, content: @Composable () -> Unit) {
    val deck = LocalDeck.current
    Box(
        Modifier
            .fillMaxSize()
            .background(Color.Black.copy(alpha = 0.55f))
            .clickable(onClick = onDismiss),
    ) {
        Box(
            Modifier
                .align(Alignment.BottomCenter)
                .fillMaxWidth()
                // Without these the sheet keeps its full height behind the
                // keyboard: project, priority, attachments and the submit
                // button all sit underneath it and scrolling does not help,
                // because there is nothing to scroll — the content fits the
                // sheet, the sheet just is not on screen.
                .navigationBarsPadding()
                .imePadding()
                .heightIn(max = 620.dp)
                .clip(RoundedCornerShape(topStart = 28.dp, topEnd = 28.dp))
                .background(deck.background)
                .border(
                    1.dp,
                    deck.outline,
                    RoundedCornerShape(topStart = 28.dp, topEnd = 28.dp),
                )
                // Swallow taps so they do not reach the dismissing scrim below.
                .clickable(enabled = false) {}
                .padding(DeckMetrics.screenPadding),
        ) {
            Column {
                Row(
                    Modifier.fillMaxWidth(),
                    horizontalArrangement = Arrangement.SpaceBetween,
                    verticalAlignment = Alignment.CenterVertically,
                ) {
                    Text(title, color = deck.textPrimary, fontSize = 19.sp, fontWeight = FontWeight.Bold)
                    Icon(
                        Icons.Filled.Close,
                        contentDescription = "Schließen",
                        tint = deck.textSecondary,
                        modifier = Modifier.size(22.dp).clickable(onClick = onDismiss),
                    )
                }
                Spacer(Modifier.height(DeckMetrics.gap + 4.dp))
                content()
            }
        }
    }
}

/** Status chooser shared by the capture sheet and the detail sheet. */
@Composable
fun StatusChooser(current: TaskStatus, onPick: (TaskStatus) -> Unit) {
    Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
        TaskStatus.entries.forEach { status ->
            Box(
                Modifier
                    .clip(RoundedCornerShape(DeckMetrics.chipRadius))
                    .background(
                        if (status == current) {
                            LocalDeck.current.accentSoft
                        } else {
                            LocalDeck.current.surfaceRaised
                        },
                    )
                    .clickable { onPick(status) }
                    .padding(horizontal = 10.dp, vertical = 6.dp),
            ) {
                Text(
                    status.label,
                    color = if (status == current) LocalDeck.current.accent else LocalDeck.current.textFaint,
                    fontSize = 11.sp,
                    fontWeight = FontWeight.SemiBold,
                )
            }
        }
    }
}
