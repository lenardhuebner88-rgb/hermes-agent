package net.hermes.deck.net

import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicLong
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asSharedFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.filterIsInstance
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.flow.onSubscription
import kotlinx.coroutines.flow.takeWhile
import kotlinx.coroutines.withTimeoutOrNull
import net.hermes.deck.nostr.NostrEvent
import net.hermes.deck.nostr.RelayProtocol
import net.hermes.deck.nostr.RelayProtocol.Frame
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import org.json.JSONObject

/**
 * A Nostr client for Buzz's relay.
 *
 * Two properties of this relay shape everything here:
 *
 *  1. It resolves the community from the *connection host*, so the URL is the
 *     tenant — there is no community parameter to pass, and a host it does not
 *     know is answered with a bare HTTP 404 at the WebSocket upgrade.
 *  2. It refuses every EVENT and REQ until NIP-42 AUTH has completed. Sending
 *     early does not queue; it is answered with `auth-required` and dropped. So
 *     [connect] does not report success before the AUTH round-trip is confirmed.
 */
class RelayClient(
    private val relayUrl: String,
    private val secretKey: ByteArray,
    private val http: OkHttpClient = defaultHttpClient(),
    private val clock: () -> Long = { System.currentTimeMillis() / 1000 },
) {

    sealed interface State {
        data object Idle : State
        data object Connecting : State
        /** Socket is up; the AUTH round-trip has not finished. */
        data object Authenticating : State
        data object Ready : State
        data class Failed(val reason: String) : State
    }

    data class PublishResult(val accepted: Boolean, val message: String)

    /** Distinguishes "the channel really is empty" from "we never heard back". */
    data class QueryResult(val events: List<NostrEvent>, val complete: Boolean)

    private val _state = MutableStateFlow<State>(State.Idle)
    val state: StateFlow<State> = _state.asStateFlow()

    private val _frames = MutableSharedFlow<Frame>(replay = 0, extraBufferCapacity = 512)
    val frames: Flow<Frame> = _frames.asSharedFlow()

    private var socket: WebSocket? = null
    private val subscriptionCounter = AtomicLong(0)
    private var authGate: CompletableDeferred<Boolean>? = null

    @Volatile
    private var pendingAuthEventId: String? = null

    /** Connects and completes NIP-42. Returns null on success, else the reason. */
    suspend fun connect(timeoutMillis: Long = 20_000): String? {
        if (_state.value == State.Ready) return null
        _state.value = State.Connecting
        val gate = CompletableDeferred<Boolean>()
        authGate = gate

        socket = http.newWebSocket(Request.Builder().url(relayUrl).build(), Listener())

        val authenticated = withTimeoutOrNull(timeoutMillis) { gate.await() }
        return when {
            authenticated == null -> {
                val reason = "Zeitüberschreitung beim Verbinden mit $relayUrl"
                _state.value = State.Failed(reason)
                close()
                reason
            }
            authenticated -> null
            else -> (_state.value as? State.Failed)?.reason ?: "Anmeldung am Relay abgelehnt"
        }
    }

    fun close() {
        socket?.close(1000, null)
        socket = null
        if (_state.value !is State.Failed) _state.value = State.Idle
    }

    /**
     * Runs a stored-event query and stops at EOSE.
     *
     * Deliberately not a long-lived subscription: the deck syncs in bursts, and
     * a REQ per screen would leak subscriptions on a phone that suspends
     * mid-scroll.
     */
    suspend fun query(filters: List<JSONObject>, timeoutMillis: Long = 25_000): QueryResult {
        check(_state.value == State.Ready) { "query before the relay is ready" }
        val subId = "deck-${subscriptionCounter.incrementAndGet()}"
        val collected = ArrayList<NostrEvent>()

        // onSubscription sends REQ only once this collector is registered.
        // Sending first would race: EOSE for a small channel can arrive before
        // collection starts, and the flow has no replay to catch up from.
        val finished = withTimeoutOrNull(timeoutMillis) {
            _frames
                .onSubscription { send(RelayProtocol.reqFrame(subId, filters)) }
                .takeWhile { frame -> !isEndOf(frame, subId) }
                .filterIsInstance<Frame.Event>()
                .collect { if (it.subscriptionId == subId) collected.add(it.event) }
            true
        } ?: false

        runCatching { send(RelayProtocol.closeFrame(subId)) }
        return QueryResult(collected.toList(), finished)
    }

    private fun isEndOf(frame: Frame, subId: String): Boolean =
        (frame is Frame.EndOfStored && frame.subscriptionId == subId) ||
            (frame is Frame.Closed && frame.subscriptionId == subId)

    /** Publishes an event and waits for the relay's OK. */
    suspend fun publish(event: NostrEvent, timeoutMillis: Long = 20_000): PublishResult {
        check(_state.value == State.Ready) { "publish before the relay is ready" }
        val ok = withTimeoutOrNull(timeoutMillis) {
            _frames
                .onSubscription { send(RelayProtocol.eventFrame(event)) }
                .filterIsInstance<Frame.Ok>()
                .first { it.eventId == event.id }
        }
        return when {
            ok == null -> PublishResult(false, "Keine Antwort vom Relay")
            ok.accepted -> PublishResult(true, ok.message)
            else -> PublishResult(false, ok.message.ifBlank { "Vom Relay abgelehnt" })
        }
    }

    private fun send(payload: String) {
        val ws = socket ?: error("not connected")
        ws.send(payload)
    }

    private inner class Listener : WebSocketListener() {
        override fun onOpen(webSocket: WebSocket, response: Response) {
            _state.value = State.Authenticating
        }

        override fun onMessage(webSocket: WebSocket, text: String) {
            when (val frame = RelayProtocol.parseFrame(text)) {
                is Frame.Auth -> {
                    val event = RelayProtocol.authEvent(secretKey, relayUrl, frame.challenge, clock())
                    pendingAuthEventId = event.id
                    webSocket.send(RelayProtocol.authFrame(event))
                }
                is Frame.Ok -> {
                    if (frame.eventId == pendingAuthEventId) {
                        pendingAuthEventId = null
                        if (frame.accepted) {
                            _state.value = State.Ready
                            authGate?.complete(true)
                        } else {
                            _state.value = State.Failed(
                                frame.message.ifBlank { "Relay hat die Anmeldung abgelehnt" },
                            )
                            authGate?.complete(false)
                        }
                    } else {
                        _frames.tryEmit(frame)
                    }
                }
                else -> _frames.tryEmit(frame)
            }
        }

        override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
            val reason = when (response?.code) {
                // Not a missing page: the relay answers the upgrade with 404 when
                // the host is not a registered community.
                404 -> "Relay kennt diesen Host nicht (HTTP 404) — stimmt die Adresse?"
                null -> t.message ?: "Verbindung fehlgeschlagen"
                else -> "HTTP ${response.code}"
            }
            _state.value = State.Failed(reason)
            authGate?.complete(false)
        }

        override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
            if (_state.value !is State.Failed) _state.value = State.Idle
            authGate?.complete(false)
        }
    }

    companion object {
        fun defaultHttpClient(): OkHttpClient = OkHttpClient.Builder()
            .connectTimeout(15, TimeUnit.SECONDS)
            // A WebSocket is idle by nature; a read timeout would kill it.
            .readTimeout(0, TimeUnit.MILLISECONDS)
            .pingInterval(25, TimeUnit.SECONDS)
            .build()

        /** `https://host:port` → `wss://host:port`, the form the relay expects. */
        fun webSocketUrl(baseUrl: String): String = baseUrl.trim().trimEnd('/')
            .replaceFirst("https://", "wss://")
            .replaceFirst("http://", "ws://")
    }
}
