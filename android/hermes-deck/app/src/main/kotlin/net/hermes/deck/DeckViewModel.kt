package net.hermes.deck

import android.app.Application
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import java.io.File
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import net.hermes.deck.data.DeckRepository
import net.hermes.deck.data.DeckSettings
import net.hermes.deck.data.DeckStore
import net.hermes.deck.model.Attachment
import net.hermes.deck.model.BuzzAgent
import net.hermes.deck.model.Channel
import net.hermes.deck.model.DeckTask
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
        val busyStem: String? = null,
    )

    private val _agents = MutableStateFlow(AgentsState())
    val agents: StateFlow<AgentsState> = _agents.asStateFlow()

    private val _filterChannel = MutableStateFlow<String?>(null)
    val filterChannel: StateFlow<String?> = _filterChannel.asStateFlow()

    private val _search = MutableStateFlow("")
    val search: StateFlow<String> = _search.asStateFlow()

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

    fun dismissMessage() {
        _message.value = null
    }

    fun say(text: String) {
        _message.value = text
    }

    fun sync() = viewModelScope.launch {
        repository.sync()?.let { _message.value = it }
    }

    /** The list the deck screen shows, after the channel filter and search. */
    fun visibleTasks(tasks: List<DeckTask>): List<DeckTask> {
        val channel = _filterChannel.value
        val needle = _search.value.trim().lowercase()
        return tasks.asSequence()
            .filter { channel == null || it.channelId == channel }
            .filter {
                needle.isEmpty() ||
                    it.title.lowercase().contains(needle) ||
                    it.body.lowercase().contains(needle)
            }
            .toList()
    }

    fun capture(
        channelId: String,
        title: String,
        body: String,
        priority: TaskPriority,
        attachments: List<Attachment>,
        onDone: (DeckTask) -> Unit = {},
    ) = viewModelScope.launch {
        runCatching {
            repository.capture(
                channelId = channelId,
                title = title,
                body = body,
                priority = priority,
                attachments = attachments,
            )
        }.onSuccess { task ->
            onDone(task)
            val pending = repository.pushPending()
            _message.value = if (pending == 0) {
                "In Buzz abgelegt."
            } else {
                "Lokal gesichert — $pending wartet auf Verbindung."
            }
        }.onFailure { _message.value = it.message ?: "Konnte nicht gespeichert werden." }
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
        val result = withContext(Dispatchers.IO) { runCatching { client.agents() } }
        _agents.value = result.fold(
            onSuccess = { AgentsState(agents = it, loading = false) },
            onFailure = { _agents.value.copy(loading = false, error = it.message ?: "Abruf fehlgeschlagen") },
        )
    }

    fun loadModels(stem: String) = viewModelScope.launch {
        _agents.value = _agents.value.copy(modelsFor = stem, models = emptyList(), modelsError = null)
        val result = withContext(Dispatchers.IO) { runCatching { hermes().availableModels(stem) } }
        _agents.value = result.fold(
            onSuccess = { choices ->
                _agents.value.copy(
                    models = choices.models,
                    // An empty list with no error would look like "this agent has
                    // no models"; say plainly that the probe came back empty.
                    modelsError = choices.error
                        ?: if (choices.models.isEmpty()) "Keine Liste erhalten — Modell lässt sich trotzdem setzen." else null,
                )
            },
            onFailure = { _agents.value.copy(modelsError = it.message ?: "Modelle nicht abrufbar") },
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
