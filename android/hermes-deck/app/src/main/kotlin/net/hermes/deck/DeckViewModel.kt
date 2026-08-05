package net.hermes.deck

import android.app.Application
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import java.io.File
import java.time.LocalDate
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.async
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import net.hermes.deck.data.DeckRepository
import net.hermes.deck.data.DeckSettings
import net.hermes.deck.data.DeckStore
import net.hermes.deck.data.SetupLink
import net.hermes.deck.model.AccountUsage
import net.hermes.deck.model.Attachment
import net.hermes.deck.model.BuzzAgent
import net.hermes.deck.model.Channel
import net.hermes.deck.model.DeckTask
import net.hermes.deck.model.DueDates
import net.hermes.deck.model.Mention
import net.hermes.deck.model.ThreadActivity
import net.hermes.deck.model.TaskFilter
import net.hermes.deck.model.TaskPriority
import net.hermes.deck.model.TaskStatus
import net.hermes.deck.net.HermesClient

class DeckViewModel(app: Application) : AndroidViewModel(app) {

    val settings = DeckSettings(app)
    private val store = DeckStore(File(app.filesDir, DeckStore.FILE_NAME))
    val repository = DeckRepository(settings, store)

    data class AgentsState(
        val agents: List<BuzzAgent> = emptyList(),
        val loading: Boolean = false,
        val error: String? = null,
        val modelsFor: String? = null,
        val models: List<String> = emptyList(),
        val modelsError: String? = null,
        /** The probe boots a real agent and takes seconds — say so meanwhile. */
        val modelsLoading: Boolean = false,
        val busyStem: String? = null,
        /** Windows the server measured over, so the labels quote its numbers. */
        val heartbeatWindowSeconds: Int = 3600,
        val heartbeatRecentSeconds: Int = 300,
        /** Set when the journal could not be read at all — never rendered as calm. */
        val heartbeatError: String? = null,
        val usage: List<AccountUsage> = emptyList(),
        /**
         * Usage failing must not blank the agents. The two come from separate
         * endpoints and one being down says nothing about the other, so this
         * error is held apart from [error] instead of replacing the screen.
         */
        val usageError: String? = null,
    )

    private val _agents = MutableStateFlow(AgentsState())
    val agents: StateFlow<AgentsState> = _agents.asStateFlow()

    private val _filterChannel = MutableStateFlow<String?>(null)
    val filterChannel: StateFlow<String?> = _filterChannel.asStateFlow()

    private val _search = MutableStateFlow("")
    val search: StateFlow<String> = _search.asStateFlow()

    private val _filterDue = MutableStateFlow<LocalDate?>(null)
    val filterDue: StateFlow<LocalDate?> = _filterDue.asStateFlow()

    private val _message = MutableStateFlow<String?>(null)
    val message: StateFlow<String?> = _message.asStateFlow()

    /**
     * Observable, unlike [DeckSettings.hasIdentity]: reading the setting during
     * composition captures its value once, so storing a key would leave the app
     * sitting on the onboarding screen forever.
     */
    private val _hasIdentity = MutableStateFlow(settings.hasIdentity)
    val hasIdentity: StateFlow<Boolean> = _hasIdentity.asStateFlow()

    /** Stores the key and lets the UI move on. Throws with a readable message. */
    fun importKey(relayUrl: String, key: String) {
        settings.relayBaseUrl = relayUrl
        settings.importKey(key)
        _hasIdentity.value = true
        sync()
    }

    fun forgetIdentity() {
        settings.forgetIdentity()
        _hasIdentity.value = false
    }

    // --- One-tap setup via `hermes-deck://setup?…` ------------------------

    private val _pendingSetup = MutableStateFlow<SetupLink.SetupPayload?>(null)
    val pendingSetup: StateFlow<SetupLink.SetupPayload?> = _pendingSetup.asStateFlow()

    /**
     * Holds a setup link for confirmation. Never applies it: a link is
     * attacker-supplied, and one that quietly repointed the relay would send
     * every future note to a stranger's server.
     */
    fun offerSetup(rawLink: String) {
        runCatching { SetupLink.parse(rawLink) }
            .onSuccess { _pendingSetup.value = it }
            .onFailure { _message.value = it.message ?: "Einrichtungs-Link nicht lesbar." }
    }

    fun dismissSetup() {
        _pendingSetup.value = null
    }

    fun applyPendingSetup() {
        val payload = _pendingSetup.value ?: return
        _pendingSetup.value = null
        runCatching {
            payload.relayUrl?.let { settings.relayBaseUrl = it }
            payload.hermesUrl?.let { settings.hermesBaseUrl = it }
            payload.hermesUser?.let { settings.hermesUsername = it }
            payload.hermesPassword?.let { settings.hermesPassword = it }
            payload.secretKeyHex?.let {
                settings.importKey(it)
                _hasIdentity.value = true
            }
        }.onSuccess {
            _message.value = "Eingerichtet — Abgleich läuft."
            sync()
        }.onFailure { _message.value = it.message ?: "Einrichtung fehlgeschlagen." }
    }

    val snapshot = repository.snapshot
    val syncState = repository.syncState

    init {
        viewModelScope.launch {
            repository.loadLocal()
            if (settings.hasIdentity) repository.sync()
        }
    }

    fun setFilterChannel(channelId: String?) {
        _filterChannel.value = channelId
    }

    fun setSearch(text: String) {
        _search.value = text
    }

    /** Tapping the selected day again clears it — a day tile is a toggle. */
    fun setFilterDue(date: LocalDate?) {
        _filterDue.value = if (_filterDue.value == date) null else date
    }

    fun dismissMessage() {
        _message.value = null
    }

    fun say(text: String) {
        _message.value = text
    }

    fun sync() = viewModelScope.launch {
        repository.sync()?.let { _message.value = it }
    }

    /**
     * The list the deck screen shows, after the channel filter and search.
     *
     * Filter and needle are parameters, not reads of [_filterChannel] and
     * [_search]. Reading the flows in here made the function invisible to
     * Compose: the screen recomposed — the approvals section correctly
     * disappeared as soon as the field held text — while this list kept its
     * pre-search contents. Typing looked like it did nothing, and no test
     * could see it, because called directly the function filtered fine.
     */
    fun visibleTasks(
        tasks: List<DeckTask>,
        channel: String?,
        search: String,
        due: LocalDate?,
    ): List<DeckTask> = TaskFilter.apply(tasks, channel, search, due)

    /**
     * Threads with recent words in them.
     *
     * Takes `now` rather than reading the clock so the window advances with the
     * screen instead of freezing at whatever second the last sync happened —
     * and so the whole thing is testable with a fixed instant.
     */
    fun activeThreads(
        snapshot: DeckRepository.Snapshot,
        now: Long,
    ): List<ThreadActivity.Entry> = ThreadActivity.of(
        events = snapshot.chatter,
        tasks = snapshot.tasks,
        channels = snapshot.channels,
        profiles = snapshot.profiles,
        me = settings.pubkeyHex,
        now = now,
    )

    /**
     * Mentions still waiting on an answer — those newer than my own last word
     * in that same channel.
     *
     * Counting every mention in the chatter window instead reported 63 against
     * the real workspace on 2026-08-05, reaching back 133 hours, and could only
     * ever grow: a chip under the heading "Wartet auf dich" that never falls is
     * not a claim about waiting, it is a claim about history. Speaking in the
     * channel clears it, which is the one signal available here without
     * inventing a read-state Buzz already owns.
     */
    fun mentions(snapshot: DeckRepository.Snapshot): List<Mention.Entry> =
        Mention.of(snapshot.chatter, snapshot.channels, snapshot.profiles, settings.pubkeyHex)

    fun capture(
        channelId: String,
        title: String,
        body: String,
        priority: TaskPriority,
        attachments: List<Attachment>,
        due: String? = null,
        /**
         * Runs once the note is signed, stored and pushed — success or not.
         * Callers that tear down their host (the share activity) must wait for
         * this before finishing, otherwise the cancelled scope drops the note.
         */
        onSettled: () -> Unit = {},
    ) = viewModelScope.launch {
        try {
            runCatching {
                repository.capture(
                    channelId = channelId,
                    title = title,
                    body = body,
                    priority = priority,
                    attachments = attachments,
                    due = due,
                )
            }.onSuccess {
                val pending = repository.pushPending()
                _message.value = if (pending == 0) {
                    "In Buzz abgelegt."
                } else {
                    "Lokal gesichert — $pending wartet auf Verbindung."
                }
            }.onFailure { _message.value = it.message ?: "Konnte nicht gespeichert werden." }
        } finally {
            onSettled()
        }
    }

    /** `null` clears the date; the edit goes out as a `kind:40003` like any other. */
    fun setDue(task: DeckTask, date: LocalDate?) = viewModelScope.launch {
        runCatching { repository.update(task.copy(due = date?.let { DueDates.format(it) })) }
            .onSuccess {
                repository.pushPending()
                _message.value = if (date == null) "Termin entfernt." else "Fällig ${DueDates.format(date)}."
            }
            .onFailure { _message.value = it.message ?: "Termin nicht gesetzt." }
    }

    fun setStatus(task: DeckTask, status: TaskStatus) = viewModelScope.launch {
        runCatching { repository.update(task.copy(status = status)) }
            .onSuccess {
                val pending = repository.pushPending()
                _message.value = if (pending == 0) {
                    "Auf ${status.label} gesetzt."
                } else {
                    "Gemerkt — Buzz nicht erreichbar."
                }
            }
            .onFailure { _message.value = it.message ?: "Änderung fehlgeschlagen." }
    }

    fun setPriority(task: DeckTask, priority: TaskPriority) = viewModelScope.launch {
        runCatching { repository.update(task.copy(priority = priority)) }
            .onSuccess { repository.pushPending() }
            .onFailure { _message.value = it.message }
    }

    fun handOver(task: DeckTask, agentPubkey: String, agentName: String, note: String) =
        viewModelScope.launch {
            runCatching {
                repository.reply(
                    task = task,
                    text = note.ifBlank { "Bitte übernehmen." },
                    mention = listOf(agentPubkey),
                )
                repository.update(task.copy(status = TaskStatus.DOING, assignees = listOf(agentPubkey)))
            }.onSuccess {
                val pending = repository.pushPending()
                _message.value = if (pending == 0) "An $agentName übergeben." else "Übergabe wartet auf Verbindung."
            }.onFailure { _message.value = it.message ?: "Übergabe fehlgeschlagen." }
        }

    suspend fun uploadAttachment(bytes: ByteArray, mime: String, name: String): Result<Attachment> =
        runCatching { repository.uploadAttachment(bytes, mime, name) }

    // --- Thread und Freigaben ---------------------------------------------

    private val _thread = MutableStateFlow<List<net.hermes.deck.nostr.NostrEvent>>(emptyList())
    val thread: StateFlow<List<net.hermes.deck.nostr.NostrEvent>> = _thread.asStateFlow()

    private val _threadLoading = MutableStateFlow(false)
    val threadLoading: StateFlow<Boolean> = _threadLoading.asStateFlow()

    fun loadThread(task: DeckTask) = viewModelScope.launch {
        _threadLoading.value = true
        _thread.value = emptyList()
        _thread.value = repository.thread(task)
        _threadLoading.value = false
    }

    fun replyTo(task: DeckTask, text: String) = viewModelScope.launch {
        if (text.isBlank()) return@launch
        runCatching { repository.reply(task, text) }
            .onSuccess {
                val pending = repository.pushPending()
                _message.value = if (pending == 0) "Antwort gesendet." else "Antwort wartet auf Verbindung."
                loadThread(task)
            }
            .onFailure { _message.value = it.message ?: "Antwort fehlgeschlagen." }
    }

    private val _decisions = MutableStateFlow<List<DeckRepository.Decision>>(emptyList())
    val decisions: StateFlow<List<DeckRepository.Decision>> = _decisions.asStateFlow()

    private val _decisionsLoading = MutableStateFlow(false)
    val decisionsLoading: StateFlow<Boolean> = _decisionsLoading.asStateFlow()

    fun loadDecisions() = viewModelScope.launch {
        _decisionsLoading.value = true
        _decisions.value = repository.openDecisions()
        _decisionsLoading.value = false
    }

    fun approve(decision: DeckRepository.Decision) = viewModelScope.launch {
        runCatching { repository.approve(decision) }
            .onSuccess {
                // Take it off the list at once; the relay round-trip only
                // confirms what the user already decided.
                _decisions.value = _decisions.value.filterNot { it.eventId == decision.eventId }
                val pending = repository.pushPending()
                _message.value = if (pending == 0) "Freigegeben." else "Freigabe wartet auf Verbindung."
            }
            .onFailure { _message.value = it.message ?: "Freigabe fehlgeschlagen." }
    }

    fun channelsById(channels: List<Channel>): Map<String, Channel> = channels.associateBy { it.id }

    // --- Buzz agents via the Hermes dashboard -----------------------------

    private fun hermes(): HermesClient =
        HermesClient(settings.hermesBaseUrl, settings.hermesUsername, settings.hermesPassword)

    fun loadAgents() = viewModelScope.launch {
        val client = hermes()
        if (!client.isConfigured) {
            _agents.value = _agents.value.copy(
                error = "Dashboard-Zugang fehlt — unter Einstellungen eintragen.",
            )
            return@launch
        }
        _agents.value = _agents.value.copy(loading = true, error = null)
        // Both calls go out at once, but the agents render as soon as they
        // land. `/api/account-usage` fans out to five provider APIs on a cache
        // miss, and awaiting it first would hold the whole tab hostage to the
        // slowest subscription endpoint.
        val usageCall = async(Dispatchers.IO) { runCatching { client.accountUsage() } }
        val result = withContext(Dispatchers.IO) { runCatching { client.agents() } }
        _agents.value = result.fold(
            onSuccess = {
                AgentsState(
                    agents = it.agents,
                    loading = false,
                    heartbeatWindowSeconds = it.windowSeconds,
                    heartbeatRecentSeconds = it.recentSeconds,
                    heartbeatError = it.heartbeatError,
                )
            },
            onFailure = { _agents.value.copy(loading = false, error = it.message ?: "Abruf fehlgeschlagen") },
        )

        val usage = usageCall.await()
        // Copied onto whatever the agents left behind, so a usage failure never
        // blanks the agent list and an agent failure never hides the budgets.
        _agents.value = _agents.value.copy(
            usage = usage.getOrDefault(emptyList()),
            usageError = usage.exceptionOrNull()?.let { it.message ?: "Verbrauch nicht abrufbar" },
        )
    }

    fun loadModels(stem: String) = viewModelScope.launch {
        _agents.value = _agents.value.copy(
            modelsFor = stem,
            models = emptyList(),
            modelsError = null,
            modelsLoading = true,
        )
        val result = withContext(Dispatchers.IO) { runCatching { hermes().availableModels(stem) } }
        _agents.value = result.fold(
            onSuccess = { choices ->
                _agents.value.copy(
                    models = choices.models,
                    modelsLoading = false,
                    // An empty list with no error would look like "this agent has
                    // no models"; say plainly that the probe came back empty.
                    modelsError = choices.error
                        ?: if (choices.models.isEmpty()) "Keine Liste erhalten — Modell lässt sich trotzdem setzen." else null,
                )
            },
            onFailure = {
                _agents.value.copy(
                    modelsLoading = false,
                    modelsError = it.message ?: "Modelle nicht abrufbar",
                )
            },
        )
    }

    fun setModel(stem: String, model: String) = viewModelScope.launch {
        _agents.value = _agents.value.copy(busyStem = stem)
        val result = withContext(Dispatchers.IO) { runCatching { hermes().setModel(stem, model) } }
        _agents.value = _agents.value.copy(busyStem = null)
        result.fold(
            onSuccess = { change ->
                _message.value = buildString {
                    append("$stem → ${change.current}")
                    if (!change.restarted) append(" — Neustart fehlgeschlagen!")
                    else if (!change.ready) append(" — neu gestartet, Bereitschaft nicht bestätigt")
                    if (change.unverified) append(" (Modell-ID ungeprüft)")
                }
                loadAgents()
            },
            onFailure = { _message.value = it.message ?: "Modellwechsel fehlgeschlagen" },
        )
    }
}
