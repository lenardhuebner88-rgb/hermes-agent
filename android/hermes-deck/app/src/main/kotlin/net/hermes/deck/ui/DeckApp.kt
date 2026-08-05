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
import androidx.compose.foundation.layout.navigationBars
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.layout.windowInsetsPadding
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Add
import androidx.compose.material.icons.filled.Home
import androidx.compose.material.icons.filled.List
import androidx.compose.material.icons.filled.Person
import androidx.compose.material.icons.filled.Settings
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Icon
import androidx.compose.material3.Snackbar
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
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
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import kotlinx.coroutines.delay
import net.hermes.deck.DeckViewModel
import net.hermes.deck.model.DeckTask

enum class DeckTab(val label: String, val icon: ImageVector) {
    DECK("Deck", Icons.Filled.Home),
    PROJECTS("Projekte", Icons.Filled.List),
    AGENTS("Agenten", Icons.Filled.Person),
    SETTINGS("Mehr", Icons.Filled.Settings),
}

@Composable
fun DeckApp(viewModel: DeckViewModel) {
    val deck = LocalDeck.current
    var tab by remember { mutableStateOf(DeckTab.DECK) }
    var capturing by remember { mutableStateOf(false) }
    var openTask by remember { mutableStateOf<DeckTask?>(null) }

    val snapshot by viewModel.snapshot.collectAsState()
    val message by viewModel.message.collectAsState()
    val hasIdentity by viewModel.hasIdentity.collectAsState()
    val pendingSetup by viewModel.pendingSetup.collectAsState()

    // Without an identity nothing else can work, so onboarding replaces the
    // whole surface rather than hiding behind a tab. The setup dialog has to
    // render on top of it — a link tapped on a fresh install is exactly the
    // case it exists for.
    if (!hasIdentity) {
        OnboardingScreen(viewModel)
        pendingSetup?.let { SetupConfirmDialog(it, false, viewModel::applyPendingSetup, viewModel::dismissSetup) }
        message?.let { text ->
            Box(Modifier.fillMaxSize().padding(16.dp), contentAlignment = Alignment.BottomCenter) {
                Snackbar(containerColor = deck.surfaceRaised, contentColor = deck.textPrimary) {
                    Text(text, fontSize = 13.sp)
                }
            }
            LaunchedEffect(text) {
                delay(6000)
                viewModel.dismissMessage()
            }
        }
        return
    }

    Box(Modifier.fillMaxSize().background(deck.background)) {
        when (tab) {
            DeckTab.DECK -> HomeScreen(
                viewModel = viewModel,
                onOpenTask = { openTask = it },
                onOpenSettings = { tab = DeckTab.SETTINGS },
            )
            DeckTab.PROJECTS -> ProjectsScreen(viewModel) { channelId ->
                viewModel.setFilterChannel(channelId)
                tab = DeckTab.DECK
            }
            DeckTab.AGENTS -> AgentsScreen(viewModel)
            DeckTab.SETTINGS -> SettingsScreen(viewModel)
        }

        BottomBar(
            current = tab,
            pendingCount = snapshot.pendingCount,
            onSelect = { tab = it },
            onCapture = { capturing = true },
            modifier = Modifier.align(Alignment.BottomCenter),
        )

        message?.let { text ->
            Box(Modifier.align(Alignment.BottomCenter).padding(bottom = 108.dp, start = 16.dp, end = 16.dp)) {
                Snackbar(containerColor = deck.surfaceRaised, contentColor = deck.textPrimary) {
                    Text(text, fontSize = 13.sp)
                }
            }
            LaunchedEffect(text) {
                delay(4000)
                viewModel.dismissMessage()
            }
        }
    }

    pendingSetup?.let {
        SetupConfirmDialog(it, replacesIdentity = true, viewModel::applyPendingSetup, viewModel::dismissSetup)
    }

    if (capturing) {
        CaptureSheet(
            viewModel = viewModel,
            onDismiss = { capturing = false },
        )
    }

    openTask?.let { task ->
        // Re-read from the snapshot so the sheet reflects edits made inside it.
        val live = snapshot.tasks.firstOrNull { it.id == task.id } ?: task
        TaskDetailSheet(
            viewModel = viewModel,
            task = live,
            onDismiss = { openTask = null },
        )
    }
}

/**
 * Confirmation for a `hermes-deck://setup?…` link.
 *
 * Shows the two facts that decide whether the link is trustworthy — which
 * relay the data will go to, and which identity it will be signed with —
 * because everything else in the link is invisible once applied.
 */
@Composable
private fun SetupConfirmDialog(
    payload: net.hermes.deck.data.SetupLink.SetupPayload,
    replacesIdentity: Boolean,
    onApply: () -> Unit,
    onDismiss: () -> Unit,
) {
    val deck = LocalDeck.current
    AlertDialog(
        onDismissRequest = onDismiss,
        containerColor = deck.surfaceRaised,
        titleContentColor = deck.textPrimary,
        textContentColor = deck.textSecondary,
        title = { Text("Deck einrichten?", fontSize = 17.sp) },
        text = {
            Column {
                Text(
                    "Dieser Link richtet die App ein. Prüfe, dass beides stimmt:",
                    fontSize = 13.sp,
                    color = deck.textSecondary,
                )
                Spacer(Modifier.height(12.dp))
                SetupFact("Relay", payload.relayHost ?: "bleibt wie eingestellt", deck.textPrimary)
                payload.npub?.let {
                    // Truncated in the middle: the tail is what distinguishes
                    // two npubs, so a plain `take(24)` would hide the part that
                    // matters.
                    SetupFact("Identität", it.take(14) + "…" + it.takeLast(6), deck.textPrimary)
                }
                if (payload.hermesUrl != null || payload.hermesUser != null) {
                    SetupFact("Dashboard", "Zugang wird gesetzt", deck.textSecondary)
                }
                // Only a warning when there is actually something to lose —
                // on a fresh install the same sentence would be a lie.
                if (payload.secretKeyHex != null && replacesIdentity) {
                    Spacer(Modifier.height(10.dp))
                    Text(
                        "Ersetzt die Identität, mit der die App bisher gearbeitet hat.",
                        fontSize = 11.sp,
                        color = deck.warning,
                    )
                }
            }
        },
        confirmButton = {
            TextButton(onClick = onApply) { Text("Übernehmen", color = deck.accent) }
        },
        dismissButton = {
            TextButton(onClick = onDismiss) { Text("Abbrechen", color = deck.textFaint) }
        },
    )
}

@Composable
private fun SetupFact(label: String, value: String, valueColor: Color) {
    val deck = LocalDeck.current
    Row(Modifier.fillMaxWidth().padding(vertical = 3.dp)) {
        Text(label, fontSize = 12.sp, color = deck.textFaint, modifier = Modifier.width(84.dp))
        Text(value, fontSize = 12.sp, color = valueColor)
    }
}

@Composable
private fun BottomBar(
    current: DeckTab,
    pendingCount: Int,
    onSelect: (DeckTab) -> Unit,
    onCapture: () -> Unit,
    modifier: Modifier = Modifier,
) {
    val deck = LocalDeck.current
    Box(modifier.fillMaxWidth().windowInsetsPadding(WindowInsets.navigationBars)) {
        Row(
            Modifier
                .fillMaxWidth()
                .padding(horizontal = 12.dp, vertical = 10.dp)
                .clip(RoundedCornerShape(28.dp))
                .background(deck.surface)
                .border(1.dp, deck.outline, RoundedCornerShape(28.dp))
                .padding(vertical = 10.dp),
            horizontalArrangement = Arrangement.SpaceEvenly,
            verticalAlignment = Alignment.CenterVertically,
        ) {
            NavIcon(DeckTab.DECK, current, onSelect)
            NavIcon(DeckTab.PROJECTS, current, onSelect)

            // The capture button sits in the bar rather than floating over the
            // list: one-thumb reach is the whole point of this app.
            Box(
                Modifier
                    .size(52.dp)
                    .clip(CircleShape)
                    .background(deck.accentBrush)
                    .clickable(onClick = onCapture),
                contentAlignment = Alignment.Center,
            ) {
                Icon(Icons.Filled.Add, contentDescription = "Neu erfassen", tint = Color.White)
            }

            NavIcon(DeckTab.AGENTS, current, onSelect)
            Box {
                NavIcon(DeckTab.SETTINGS, current, onSelect)
                if (pendingCount > 0) {
                    // Unsent items are the one piece of state that must never be
                    // silent — an idea that never left the phone looks identical
                    // to one that landed.
                    Box(
                        Modifier
                            .align(Alignment.TopEnd)
                            .size(8.dp)
                            .clip(CircleShape)
                            .background(deck.warning),
                    )
                }
            }
        }
    }
}

@Composable
private fun NavIcon(tab: DeckTab, current: DeckTab, onSelect: (DeckTab) -> Unit) {
    val deck = LocalDeck.current
    val selected = tab == current
    Column(
        horizontalAlignment = Alignment.CenterHorizontally,
        modifier = Modifier.clickable { onSelect(tab) }.padding(horizontal = 10.dp),
    ) {
        Icon(
            tab.icon,
            contentDescription = tab.label,
            tint = if (selected) deck.accent else deck.textFaint,
            modifier = Modifier.size(22.dp),
        )
        Spacer(Modifier.height(3.dp))
        Text(
            tab.label,
            color = if (selected) deck.accent else deck.textFaint,
            fontSize = 10.sp,
        )
    }
}

/** Height the scrollable screens must leave free for the bar. */
val bottomBarSpace = 110.dp
