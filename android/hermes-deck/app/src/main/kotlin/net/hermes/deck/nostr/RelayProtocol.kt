package net.hermes.deck.nostr

import org.json.JSONArray
import org.json.JSONObject

/**
 * The frames Buzz's relay speaks, and the events we sign to get in.
 *
 * Kept free of Android and OkHttp so the whole wire format is covered by plain
 * JVM tests — the transport in `RelayClient` only moves strings.
 */
object RelayProtocol {

    /** A parsed inbound frame. Anything unrecognised becomes [Unknown]. */
    sealed interface Frame {
        /** Relay is asking us to prove who we are (NIP-42). */
        data class Auth(val challenge: String) : Frame

        data class Event(val subscriptionId: String, val event: NostrEvent) : Frame

        /** End of stored events for a subscription; live updates follow. */
        data class EndOfStored(val subscriptionId: String) : Frame

        data class Ok(val eventId: String, val accepted: Boolean, val message: String) : Frame

        data class Closed(val subscriptionId: String, val message: String) : Frame

        data class Notice(val message: String) : Frame

        data class Unknown(val raw: String) : Frame
    }

    fun parseFrame(raw: String): Frame {
        val array = runCatching { JSONArray(raw) }.getOrNull()
            ?: return Frame.Unknown(raw)
        if (array.length() == 0) return Frame.Unknown(raw)
        return when (array.optString(0)) {
            "AUTH" -> Frame.Auth(array.optString(1))
            "EVENT" -> {
                val obj = array.optJSONObject(2) ?: return Frame.Unknown(raw)
                runCatching { Frame.Event(array.optString(1), NostrEvent.fromJson(obj)) }
                    .getOrElse { Frame.Unknown(raw) }
            }
            "EOSE" -> Frame.EndOfStored(array.optString(1))
            "OK" -> Frame.Ok(
                eventId = array.optString(1),
                accepted = array.optBoolean(2, false),
                message = array.optString(3, ""),
            )
            "CLOSED" -> Frame.Closed(array.optString(1), array.optString(2, ""))
            "NOTICE" -> Frame.Notice(array.optString(1, ""))
            else -> Frame.Unknown(raw)
        }
    }

    fun reqFrame(subscriptionId: String, filters: List<JSONObject>): String {
        val array = JSONArray()
        array.put("REQ")
        array.put(subscriptionId)
        filters.forEach { array.put(it) }
        return array.toString()
    }

    fun closeFrame(subscriptionId: String): String =
        JSONArray().put("CLOSE").put(subscriptionId).toString()

    fun eventFrame(event: NostrEvent): String =
        JSONArray().put("EVENT").put(event.toJson()).toString()

    fun authFrame(event: NostrEvent): String =
        JSONArray().put("AUTH").put(event.toJson()).toString()

    /**
     * The NIP-42 response. The `relay` tag must carry the URL we actually dialled
     * — the relay compares it, and a normalised or trailing-slash-stripped
     * variant is rejected without an explanatory message.
     */
    fun authEvent(secretKey: ByteArray, relayUrl: String, challenge: String, now: Long): NostrEvent =
        NostrEvent.signed(
            secretKey = secretKey,
            kind = Kind.AUTH,
            content = "",
            tags = listOf(
                listOf("relay", relayUrl),
                listOf("challenge", challenge),
            ),
            createdAt = now,
        )

    /**
     * Blossom BUD-02 upload authorisation. Sent base64-encoded in
     * `Authorization: Nostr …`; the `x` tag must equal the body's sha256, which
     * the relay cross-checks against the `X-SHA-256` header.
     */
    fun blossomUploadAuth(
        secretKey: ByteArray,
        sha256Hex: String,
        expiresAt: Long,
        serverHost: String,
        now: Long,
    ): NostrEvent = NostrEvent.signed(
        secretKey = secretKey,
        kind = Kind.BLOSSOM_AUTH,
        content = "Upload",
        tags = listOf(
            listOf("t", "upload"),
            listOf("x", sha256Hex),
            listOf("expiration", expiresAt.toString()),
            listOf("server", serverHost),
        ),
        createdAt = now,
    )

    /** Filter for "the channels I am a member of". */
    fun myChannelsFilter(pubkey: String): JSONObject = JSONObject().apply {
        put("kinds", JSONArray().put(Kind.CHANNEL_MEMBERS))
        put("#p", JSONArray().put(pubkey))
        put("limit", 500)
    }

    fun channelMetadataFilter(channelIds: Collection<String>): JSONObject = JSONObject().apply {
        put("kinds", JSONArray().put(Kind.CHANNEL_METADATA))
        put("#d", JSONArray().apply { channelIds.forEach { put(it) } })
        put("limit", 500)
    }

    /**
     * Task roots in one channel.
     *
     * `#t` is the only handle we get: the relay's filter deserialiser keeps
     * single-letter tag keys and *silently drops* anything longer, so a
     * `"#status"` filter would look like it worked while matching everything.
     */
    fun deckTaskFilter(channelId: String, limit: Int = 200, until: Long? = null): JSONObject =
        JSONObject().apply {
            put("kinds", JSONArray().put(Kind.CHANNEL_MESSAGE))
            put("#h", JSONArray().put(channelId))
            put("#t", JSONArray().put(DECK_TASK_MARKER))
            put("limit", limit)
            until?.let { put("until", it) }
        }

    /** Edits, reactions and deletions that may change a task we already hold. */
    fun taskOverlayFilter(channelId: String, limit: Int = 500): JSONObject = JSONObject().apply {
        put("kinds", JSONArray().put(Kind.MESSAGE_EDIT).put(Kind.REACTION).put(Kind.DELETE))
        put("#h", JSONArray().put(channelId))
        put("limit", limit)
    }

    fun threadFilter(rootEventId: String, limit: Int = 200): JSONObject = JSONObject().apply {
        put("kinds", JSONArray().put(Kind.CHANNEL_MESSAGE).put(Kind.MESSAGE_EDIT))
        put("#e", JSONArray().put(rootEventId))
        put("limit", limit)
    }

    /** Marks an event as one of ours. Single-letter `t` so the relay can filter it. */
    const val DECK_TASK_MARKER = "deck-task"
}
