package net.hermes.deck.data

import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.withContext
import net.hermes.deck.model.Attachment
import net.hermes.deck.model.Channel
import net.hermes.deck.model.DeckTask
import net.hermes.deck.model.Profile
import net.hermes.deck.model.TaskAssembler
import net.hermes.deck.model.TaskCodec
import net.hermes.deck.model.TaskPriority
import net.hermes.deck.model.TaskStatus
import net.hermes.deck.net.MediaUploader
import net.hermes.deck.net.RelayClient
import net.hermes.deck.nostr.Kind
import net.hermes.deck.nostr.NostrEvent
import net.hermes.deck.nostr.RelayProtocol

/**
 * Everything the screens read from and write to.
 *
 * Local first: a capture is signed, stored and shown immediately, and only then
 * pushed. Nothing the user does depends on the relay being reachable, and a
 * failed push leaves the item in the outbox rather than losing it or silently
 * pretending it landed.
 */
class DeckRepository(
    private val settings: DeckSettings,
    private val store: DeckStore,
    private val clock: () -> Long = { System.currentTimeMillis() / 1000 },
    private val relayFactory: (String, ByteArray) -> RelayClient = { url, key -> RelayClient(url, key) },
) {

    data class Snapshot(
        val channels: List<Channel> = emptyList(),
        val tasks: List<DeckTask> = emptyList(),
        val pendingCount: Int = 0,
        val lastSyncAt: Long = 0,
        /**
         * Recent plain channel messages, held for this session only.
         *
         * Deliberately not persisted: 120 messages per channel across thirteen
         * channels would grow the single-file store without bound, and "what is
         * being said right now" is a question the next sync answers in a second
         * anyway. Task roots and their edits keep being persisted as before.
         */
        val chatter: List<NostrEvent> = emptyList(),
        val profiles: Map<String, Profile> = emptyMap(),
    )

    sealed interface SyncState {
        data object Idle : SyncState
        data object Running : SyncState
        data class Failed(val reason: String) : SyncState
        data class Done(val at: Long, val taskCount: Int) : SyncState
    }

    private val _snapshot = MutableStateFlow(Snapshot())
    val snapshot: StateFlow<Snapshot> = _snapshot.asStateFlow()

    private val _syncState = MutableStateFlow<SyncState>(SyncState.Idle)
    val syncState: StateFlow<SyncState> = _syncState.asStateFlow()

    private var cached: DeckStore.Snapshot = DeckStore.Snapshot()

    /** Session-only; see [Snapshot.chatter] for why this is not persisted. */
    private var sessionChatter: List<NostrEvent> = emptyList()

    /** Reads the offline copy. Cheap and safe to call before any network exists. */
    suspend fun loadLocal() = withContext(Dispatchers.IO) {
        cached = store.load()
        publishSnapshot()
    }

    suspend fun sync(): String? = withContext(Dispatchers.IO) {
        val key = settings.secretKey ?: return@withContext "Kein Schlüssel hinterlegt."
        _syncState.value = SyncState.Running
        val relay = relayFactory(RelayClient.webSocketUrl(settings.relayBaseUrl), key)
        try {
            relay.connect()?.let { reason ->
                _syncState.value = SyncState.Failed(reason)
                return@withContext reason
            }
            val pubkey = settings.pubkeyHex ?: return@withContext "Schlüssel unlesbar."

            // Push first: a task created offline should exist on the relay before
            // we pull, otherwise this sync reports a state that already lags.
            flushOutbox(relay)

            val memberships = relay.query(listOf(RelayProtocol.myChannelsFilter(pubkey)))
            val channelIds = memberships.events.mapNotNull { Channel.membershipChannelId(it) }.distinct()
            val membersById = memberships.events.mapNotNull { event ->
                Channel.membershipChannelId(event)?.let { it to Channel.membersOf(event) }
            }.toMap()

            val channels = if (channelIds.isEmpty()) {
                emptyList()
            } else {
                relay.query(listOf(RelayProtocol.channelMetadataFilter(channelIds)))
                    .events
                    .mapNotNull { Channel.fromMetadata(it, membersById[it.tag("d")] ?: emptyList()) }
            }

            val projects = channels.filter { it.usableAsProject }
            val events = ArrayList<NostrEvent>()
            val chatter = ArrayList<NostrEvent>()
            for (channel in projects) {
                val result = relay.query(
                    listOf(
                        RelayProtocol.deckTaskFilter(channel.id),
                        RelayProtocol.taskOverlayFilter(channel.id),
                        // Replies carry no `deck-task` marker — they are plain
                        // channel messages pointing at the root — so the task
                        // filter above can never see them. Without this pull the
                        // reply count on every card is structurally zero, which
                        // is exactly what it was.
                        RelayProtocol.recentMessagesFilter(channel.id),
                    ),
                )
                for (event in result.events) {
                    if (TaskCodec.isTaskRoot(event) || event.kind != Kind.CHANNEL_MESSAGE) {
                        events.add(event)
                    } else {
                        chatter.add(event)
                    }
                }
            }

            val profiles = fetchProfiles(relay, events + chatter, channels)

            cached = cached.copy(
                channels = channels,
                events = mergeEvents(cached.events, events),
                profiles = mergeEvents(cached.profiles, profiles),
                lastSyncAt = clock(),
            )
            store.save(cached)
            sessionChatter = chatter
            publishSnapshot()
            _syncState.value = SyncState.Done(cached.lastSyncAt, _snapshot.value.tasks.size)
            null
        } catch (t: Throwable) {
            val reason = t.message ?: "Abgleich fehlgeschlagen"
            _syncState.value = SyncState.Failed(reason)
            reason
        } finally {
            relay.close()
        }
    }

    /**
     * Creates a task. Signed and stored before anything touches the network, so
     * the caller can show it at once; [pushPending] carries it out later.
     */
    suspend fun capture(
        channelId: String,
        title: String,
        body: String,
        priority: TaskPriority = TaskPriority.NORMAL,
        status: TaskStatus = TaskStatus.INBOX,
        due: String? = null,
        attachments: List<Attachment> = emptyList(),
        assignees: List<String> = emptyList(),
    ): DeckTask = withContext(Dispatchers.IO) {
        val key = requireNotNull(settings.secretKey) { "Kein Schlüssel hinterlegt." }
        val event = NostrEvent.signed(
            secretKey = key,
            kind = Kind.CHANNEL_MESSAGE,
            content = TaskCodec.buildContent(title, body),
            tags = TaskCodec.tagsFor(channelId, status, priority, due, attachments, assignees),
            createdAt = clock(),
        )
        enqueue(event)
        checkNotNull(TaskCodec.fromEvent(event))
    }

    /** Publishes a changed task as a `kind:40003` edit. */
    suspend fun update(task: DeckTask): DeckTask = withContext(Dispatchers.IO) {
        val key = requireNotNull(settings.secretKey) { "Kein Schlüssel hinterlegt." }
        val edit = NostrEvent.signed(
            secretKey = key,
            kind = Kind.MESSAGE_EDIT,
            content = TaskCodec.buildContent(task.title, task.body),
            tags = TaskCodec.editEventTags(task),
            createdAt = clock(),
        )
        enqueue(edit)
        task.copy(updatedAt = edit.createdAt)
    }

    /** Posts a reply into the task's Buzz thread — this is how an agent is briefed. */
    suspend fun reply(task: DeckTask, text: String, mention: List<String> = emptyList()) =
        withContext(Dispatchers.IO) {
            val key = requireNotNull(settings.secretKey) { "Kein Schlüssel hinterlegt." }
            val event = NostrEvent.signed(
                secretKey = key,
                kind = Kind.CHANNEL_MESSAGE,
                content = text,
                tags = buildList {
                    add(listOf("h", task.channelId))
                    add(listOf("e", task.id, "", "root"))
                    add(listOf("e", task.id, "", "reply"))
                    mention.forEach { add(listOf("p", it)) }
                },
                createdAt = clock(),
            )
            enqueue(event)
        }

    /**
     * The Zuruf: a `p`-tagged line into a channel, with no task under it.
     *
     * [reply] cannot express this — it needs a [DeckTask] for its `e` tags, and
     * a call into a running turn is not about a card. What makes an agent read
     * it is the `p` tag alone; `@name` in the text would be decoration.
     *
     * Whether the agent then *wakes* is its own rule set (`buzz-acp`'s
     * `filter.rs` plus `<stem>-rules.toml`), which this app cannot see. So this
     * returns once the event is enqueued and promises delivery, never a reaction.
     */
    suspend fun callOut(channelId: String, text: String, agentPubkey: String) =
        withContext(Dispatchers.IO) {
            val key = requireNotNull(settings.secretKey) { "Kein Schlüssel hinterlegt." }
            val event = NostrEvent.signed(
                secretKey = key,
                kind = Kind.CHANNEL_MESSAGE,
                content = text,
                tags = listOf(
                    listOf("h", channelId),
                    listOf("p", agentPubkey),
                ),
                createdAt = clock(),
            )
            enqueue(event)
        }

    suspend fun uploadAttachment(bytes: ByteArray, mime: String, name: String): Attachment =
        withContext(Dispatchers.IO) {
            val key = requireNotNull(settings.secretKey) { "Kein Schlüssel hinterlegt." }
            MediaUploader(settings.relayBaseUrl, key).upload(bytes, mime, name)
        }

    /**
     * Loads the replies under a task, newest last.
     *
     * Read straight from the relay rather than the cache: the whole point of
     * opening a task is to see what an agent answered since the last sync.
     */
    suspend fun thread(task: DeckTask): List<NostrEvent> = withContext(Dispatchers.IO) {
        val key = settings.secretKey ?: return@withContext emptyList()
        val relay = relayFactory(RelayClient.webSocketUrl(settings.relayBaseUrl), key)
        try {
            if (relay.connect() != null) return@withContext emptyList()
            relay.query(listOf(RelayProtocol.threadFilter(task.id)))
                .events
                .filter { it.kind == Kind.CHANNEL_MESSAGE && it.id != task.id }
                .sortedBy { it.createdAt }
        } catch (t: Throwable) {
            emptyList()
        } finally {
            relay.close()
        }
    }

    /**
     * Open decisions waiting on the operator.
     *
     * Piet's own convention: a message that needs his call starts with the stop
     * sign, and a check-mark reaction from him closes it. Both halves are read
     * here so a decision he already ticked off stops showing up.
     */
    suspend fun openDecisions(): List<Decision> = withContext(Dispatchers.IO) {
        val key = settings.secretKey ?: return@withContext emptyList()
        val me = settings.pubkeyHex ?: return@withContext emptyList()
        val relay = relayFactory(RelayClient.webSocketUrl(settings.relayBaseUrl), key)
        try {
            if (relay.connect() != null) return@withContext emptyList()
            val channels = cached.channels.filter { it.usableAsProject }
            val found = ArrayList<Decision>()
            for (channel in channels) {
                val result = relay.query(
                    listOf(
                        RelayProtocol.recentMessagesFilter(channel.id),
                        RelayProtocol.taskOverlayFilter(channel.id, limit = 200),
                    ),
                )
                val asked = result.events.filter {
                    it.kind == Kind.CHANNEL_MESSAGE && it.content.trimStart().startsWith(DECISION_MARK)
                }
                if (asked.isEmpty()) continue
                val settled = result.events
                    .filter { it.kind == Kind.REACTION && it.pubkey == me && it.content.contains(DONE_MARK) }
                    .flatMap { it.tagValues("e") }
                    .toSet()
                asked.filterNot { it.id in settled }.forEach { event ->
                    found.add(
                        Decision(
                            eventId = event.id,
                            channelId = channel.id,
                            channelName = channel.displayName,
                            text = event.content.removePrefix(DECISION_MARK).trim(),
                            askedAt = event.createdAt,
                            authorPubkey = event.pubkey,
                        ),
                    )
                }
            }
            found.sortedByDescending { it.askedAt }
        } catch (t: Throwable) {
            emptyList()
        } finally {
            relay.close()
        }
    }

    /** Ticks a decision off with the reaction Piet's workflows already listen for. */
    suspend fun approve(decision: Decision) = withContext(Dispatchers.IO) {
        val key = requireNotNull(settings.secretKey) { "Kein Schlüssel hinterlegt." }
        val event = NostrEvent.signed(
            secretKey = key,
            kind = Kind.REACTION,
            content = DONE_MARK,
            tags = listOf(
                listOf("e", decision.eventId),
                listOf("h", decision.channelId),
                listOf("p", decision.authorPubkey),
            ),
            createdAt = clock(),
        )
        enqueue(event)
    }

    data class Decision(
        val eventId: String,
        val channelId: String,
        val channelName: String,
        val text: String,
        val askedAt: Long,
        val authorPubkey: String,
    )

    /** Tries to hand the outbox to the relay. Returns how many are still waiting. */
    suspend fun pushPending(): Int = withContext(Dispatchers.IO) {
        if (cached.outbox.isEmpty()) return@withContext 0
        val key = settings.secretKey ?: return@withContext cached.outbox.size
        val relay = relayFactory(RelayClient.webSocketUrl(settings.relayBaseUrl), key)
        try {
            if (relay.connect() != null) return@withContext cached.outbox.size
            flushOutbox(relay)
            cached.outbox.size
        } catch (t: Throwable) {
            cached.outbox.size
        } finally {
            relay.close()
        }
    }

    private suspend fun flushOutbox(relay: RelayClient) {
        if (cached.outbox.isEmpty()) return
        val remaining = ArrayList<NostrEvent>()
        val accepted = ArrayList<NostrEvent>()
        for (event in cached.outbox) {
            val result = runCatching { relay.publish(event) }.getOrNull()
            if (result?.accepted == true) accepted.add(event) else remaining.add(event)
        }
        cached = cached.copy(
            outbox = remaining,
            events = mergeEvents(cached.events, accepted),
        )
        store.save(cached)
        publishSnapshot()
    }

    private fun enqueue(event: NostrEvent) {
        // Into events as well as the outbox: the UI must show it immediately,
        // and the relay deduplicates by id when it is finally delivered.
        cached = cached.copy(
            outbox = cached.outbox + event,
            events = mergeEvents(cached.events, listOf(event)),
        )
        store.save(cached)
        publishSnapshot()
    }

    private fun mergeEvents(existing: List<NostrEvent>, incoming: List<NostrEvent>): List<NostrEvent> {
        if (incoming.isEmpty()) return existing
        val byId = LinkedHashMap<String, NostrEvent>(existing.size + incoming.size)
        existing.forEach { byId[it.id] = it }
        incoming.forEach { byId[it.id] = it }
        return byId.values.toList()
    }

    companion object {
        /** Piet's convention for "this needs a decision from the operator". */
        const val DECISION_MARK = "\u26D4"
        /** And the reaction that closes one. */
        const val DONE_MARK = "\u2705"
    }

    /**
     * Profiles for everyone who appears on screen: task authors, assignees,
     * whoever last spoke in a thread, and every channel member.
     *
     * One query for all of them — `authors` is a top-level filter field, so the
     * single-letter tag rule that constrains task filters does not apply. A
     * failure here is not worth failing the sync over: the screens fall back to
     * short pubkeys, which is what they showed before profiles existed.
     */
    private suspend fun fetchProfiles(
        relay: RelayClient,
        events: Collection<NostrEvent>,
        channels: Collection<Channel>,
    ): List<NostrEvent> {
        val wanted = buildSet {
            events.forEach { event ->
                add(event.pubkey)
                addAll(event.tagValues("p"))
            }
            channels.forEach { addAll(it.memberPubkeys) }
        }.filter { it.length == 64 }
        if (wanted.isEmpty()) return emptyList()
        return runCatching {
            relay.query(listOf(RelayProtocol.profileFilter(wanted))).events
        }.getOrDefault(emptyList())
    }

    private fun publishSnapshot() {
        _snapshot.value = Snapshot(
            channels = cached.channels.filter { it.usableAsProject },
            // Chatter is folded in here too, so a reply raises the reply count
            // on the card it belongs to instead of only inside the open thread.
            tasks = TaskAssembler.assemble(cached.events + sessionChatter),
            pendingCount = cached.outbox.size,
            lastSyncAt = cached.lastSyncAt,
            chatter = sessionChatter,
            profiles = Profile.newestByPubkey(cached.profiles),
        )
    }
}
