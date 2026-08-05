package net.hermes.deck.ui

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableLongStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.LifecycleEventObserver
import androidx.lifecycle.compose.LocalLifecycleOwner
import java.time.LocalDate
import kotlinx.coroutines.delay
import net.hermes.deck.DeckViewModel
import net.hermes.deck.model.ActionStack
import net.hermes.deck.model.AgentRow
import net.hermes.deck.model.Freshness
import net.hermes.deck.model.TaskFilter
import net.hermes.deck.model.WorkerRun

/**
 * Connects [PulseScreen] to the view model, and owns the two things a live
 * screen has that a static one does not: a poll loop and a ticking clock.
 *
 * Both are bound to the lifecycle. A loop that keeps running once the app is in
 * the pocket spends the battery on a `journalctl` query across eight units
 * every eight seconds, for a screen nobody is looking at.
 */
@Composable
fun PulseTab(
    viewModel: DeckViewModel,
    onOpenTask: (String) -> Unit,
    onOpenAgents: () -> Unit,
) {
    val state by viewModel.pulse.collectAsState()
    val agentsState by viewModel.agents.collectAsState()
    val snapshot by viewModel.snapshot.collectAsState()
    val decisions by viewModel.decisions.collectAsState()

    var openAgent by remember { mutableStateOf<AgentRow?>(null) }
    var openRun by remember { mutableStateOf<WorkerRun?>(null) }
    // The operator's pin. It outranks the automatic focus so the screen never
    // moves under a finger; tapping the same capsule again releases it.
    var pinnedStem by remember { mutableStateOf<String?>(null) }

    // The clock the ages are rendered against. Without it the header would say
    // "gerade eben" until the next payload lands, which is the same lie the
    // whole screen exists to stop telling.
    var now by remember { mutableLongStateOf(System.currentTimeMillis()) }
    LaunchedEffect(Unit) {
        while (true) {
            delay(1000)
            now = System.currentTimeMillis()
        }
    }

    val owner = LocalLifecycleOwner.current
    DisposableEffect(owner) {
        val observer = LifecycleEventObserver { _, event ->
            when (event) {
                Lifecycle.Event.ON_START -> viewModel.startPulsePolling()
                Lifecycle.Event.ON_STOP -> viewModel.stopPulsePolling()
                else -> Unit
            }
        }
        owner.lifecycle.addObserver(observer)
        viewModel.startPulsePolling()
        onDispose {
            owner.lifecycle.removeObserver(observer)
            viewModel.stopPulsePolling()
        }
    }

    val today = LocalDate.now()
    val actions = ActionStack.of(
        decisions = decisions.map {
            ActionStack.PendingDecision(
                taskId = it.eventId,
                title = it.text.lineSequence().first().take(80),
                ageSeconds = ((now / 1000L) - it.askedAt).coerceAtLeast(0L).toInt(),
            )
        },
        holds = state.pulse?.heldTasks.orEmpty(),
        runs = state.pulse?.workerRuns.orEmpty(),
        agents = state.pulse?.agentRows.orEmpty(),
        usage = agentsState.usage,
        overdue = TaskFilter.overdue(snapshot.tasks, today),
    )

    PulseScreen(
        pulse = state.pulse,
        freshness = Freshness.of(state.pulse?.receivedAt ?: 0L, now),
        actions = actions,
        usage = agentsState.usage,
        loading = state.loading,
        error = state.error,
        pinnedStem = pinnedStem,
        onRefresh = { viewModel.refreshPulseNow() },
        onPin = { pinnedStem = it },
        onAgent = { openAgent = it },
        onRun = { openRun = it },
        onOpenAgents = onOpenAgents,
        onCallOut = { row, text ->
            val pubkey = row.session?.pubkey
            if (pubkey == null) {
                // The bar is not drawn without an identity; this is the guard
                // for the case where the payload changed under an open screen.
                viewModel.report("Für ${row.agent.displayName} ist keine Buzz-Kennung bekannt.")
            } else {
                viewModel.callOut(pubkey, row.agent.displayName, text)
            }
        },
        onOpenAction = { item ->
            when (val target = item.target) {
                is ActionStack.Target.Run ->
                    openRun = state.pulse?.workerRuns?.firstOrNull { it.runId == target.runId }
                is ActionStack.Target.Task -> onOpenTask(target.taskId)
                is ActionStack.Target.Agent ->
                    openAgent = state.pulse?.agentRows?.firstOrNull { it.agent.stem == target.stem }
                ActionStack.Target.Budget -> onOpenAgents()
            }
        },
    )

    openAgent?.let { row ->
        AgentSheet(
            row = row,
            windowSeconds = state.pulse?.agents?.data?.activityWindowSeconds ?: 1800,
            onDismiss = { openAgent = null },
        )
    }
    openRun?.let { run ->
        RunSheet(
            run = run,
            busy = state.actionBusy,
            onTerminate = { viewModel.terminateRun(run.runId); openRun = null },
            onCancelChain = { viewModel.cancelChain(run.taskId); openRun = null },
            onRelease = { viewModel.releaseGate(run.taskId); openRun = null },
            onDismiss = { openRun = null },
        )
    }
}

/** One agent, its last thirty tool calls, and what state it is really in. */
@Composable
private fun AgentSheet(row: AgentRow, windowSeconds: Int, onDismiss: () -> Unit) {
    val deck = LocalDeck.current
    SheetScaffold(title = row.agent.displayName, onDismiss = onDismiss) {
        Column {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Text(
                    row.pulse?.statusWord?.replaceFirstChar { it.uppercase() } ?: "Unbekannt",
                    color = if (row.pulse?.looksOpen == true) deck.success else deck.textSecondary,
                    fontSize = 14.sp,
                    fontWeight = FontWeight.SemiBold,
                )
                Spacer(Modifier.width(10.dp))
                Text(row.agent.model, style = monoStyle, color = deck.textFaint)
            }
            Spacer(Modifier.height(4.dp))
            Text(
                "Unit ${row.agent.activeState} · Start ${row.agent.lastStart.ifBlank { "unbekannt" }}",
                style = tickerStyle,
                color = deck.textFaint,
            )
            Spacer(Modifier.height(16.dp))
            Text("LETZTE TOOL-CALLS", style = labelStyle, color = deck.textFaint)
            Spacer(Modifier.height(10.dp))
            AgentTimeline(row.pulse, windowSeconds)
        }
    }
}

/**
 * The intervention sheet.
 *
 * Three buttons that stop real work, so each one states its target and each one
 * needs a second tap. The confirmation names the run — "Beenden?" on its own is
 * how the wrong run gets killed at a traffic light.
 */
@Composable
private fun RunSheet(
    run: WorkerRun,
    busy: Boolean,
    onTerminate: () -> Unit,
    onCancelChain: () -> Unit,
    onRelease: () -> Unit,
    onDismiss: () -> Unit,
) {
    val deck = LocalDeck.current
    var confirming by remember { mutableStateOf<String?>(null) }

    SheetScaffold(title = run.title, onDismiss = onDismiss) {
        Column {
            Text(
                "${run.profile} · läuft ${duration(run.runtimeSeconds)}" +
                    if (run.isOverBudget) " · über Budget" else "",
                style = tickerStyle,
                color = if (run.isOverBudget) deck.danger else deck.textFaint,
            )
            run.heartbeatNote?.let {
                Spacer(Modifier.height(10.dp))
                Text(it, color = deck.textSecondary, fontSize = 13.sp, lineHeight = 18.sp)
                run.noteAgeSeconds?.let { age ->
                    Text("gemeldet ${ago(age)}", style = tickerStyle, color = deck.textFaint)
                }
            }
            run.blockReason?.let {
                Spacer(Modifier.height(10.dp))
                Text(it, color = deck.warning, fontSize = 13.sp)
            }
            run.totalTokens?.let {
                Spacer(Modifier.height(10.dp))
                Text("$it Tokens verbraucht", style = tickerStyle, color = deck.textFaint)
            }

            Spacer(Modifier.height(20.dp))
            Text("EINGRIFF", style = labelStyle, color = deck.textFaint)
            Spacer(Modifier.height(10.dp))

            if (confirming == null) {
                DangerButton("Lauf beenden", busy) { confirming = "run" }
                Spacer(Modifier.height(8.dp))
                DangerButton("Kette abbrechen", busy) { confirming = "chain" }
                Spacer(Modifier.height(8.dp))
                TextButton(onClick = onRelease, enabled = !busy) {
                    Text("Freigabe lösen", color = deck.accent, fontSize = 14.sp)
                }
            } else {
                Text(
                    when (confirming) {
                        "chain" -> "Die ganze Kette um \u201E${run.title}\u201C abbrechen?"
                        else -> "Lauf \u201E${run.title}\u201C wirklich beenden?"
                    },
                    color = deck.textPrimary,
                    fontSize = 14.sp,
                    lineHeight = 20.sp,
                )
                Spacer(Modifier.height(12.dp))
                Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    DangerButton(
                        text = if (busy) "Wird gemeldet …" else "Ja, ausführen",
                        busy = busy,
                        modifier = Modifier.weight(1f),
                    ) {
                        if (confirming == "chain") onCancelChain() else onTerminate()
                    }
                    TextButton(onClick = { confirming = null }, enabled = !busy) {
                        Text("Abbrechen", color = deck.textSecondary, fontSize = 14.sp)
                    }
                }
            }
        }
    }
}

/**
 * The one place `danger` fills a surface rather than marking a state — a button
 * that stops work has earned it.
 */
@Composable
private fun DangerButton(
    text: String,
    busy: Boolean,
    modifier: Modifier = Modifier,
    onClick: () -> Unit,
) {
    val deck = LocalDeck.current
    Button(
        onClick = onClick,
        enabled = !busy,
        modifier = modifier.fillMaxWidth(),
        colors = ButtonDefaults.buttonColors(
            containerColor = deck.danger,
            contentColor = deck.background,
        ),
    ) {
        Text(text, fontSize = 14.sp, fontWeight = FontWeight.SemiBold)
    }
}
