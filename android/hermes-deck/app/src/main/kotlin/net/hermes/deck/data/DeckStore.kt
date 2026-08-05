package net.hermes.deck.data

import java.io.File
import net.hermes.deck.model.Channel
import net.hermes.deck.nostr.NostrEvent
import org.json.JSONArray
import org.json.JSONObject

/**
 * The deck's offline copy: the channels and task events last seen, plus an
 * outbox of events that are signed but not yet accepted by the relay.
 *
 * Captured ideas are signed the moment they are written, so an entry made in a
 * dead spot keeps its real creation time and its final event id — publishing
 * later is a pure retry, never a re-authoring. That also means the outbox is
 * safe to replay: the relay deduplicates by event id.
 */
class DeckStore(private val file: File) {

    data class Snapshot(
        val channels: List<Channel> = emptyList(),
        /** Raw task roots and their edits, folded into tasks at read time. */
        val events: List<NostrEvent> = emptyList(),
        val outbox: List<NostrEvent> = emptyList(),
        val lastSyncAt: Long = 0,
    )

    fun load(): Snapshot {
        if (!file.exists()) return Snapshot()
        val text = runCatching { file.readText() }.getOrNull() ?: return Snapshot()
        val json = runCatching { JSONObject(text) }.getOrNull()
        // A truncated write (battery death mid-save) must not brick the app; an
        // unreadable cache is simply an empty one, the relay refills it.
            ?: return Snapshot()
        return Snapshot(
            channels = readChannels(json.optJSONArray("channels")),
            events = readEvents(json.optJSONArray("events")),
            outbox = readEvents(json.optJSONArray("outbox")),
            lastSyncAt = json.optLong("lastSyncAt", 0),
        )
    }

    fun save(snapshot: Snapshot) {
        val json = JSONObject().apply {
            put("version", FORMAT_VERSION)
            put("lastSyncAt", snapshot.lastSyncAt)
            put("channels", JSONArray().apply { snapshot.channels.forEach { put(writeChannel(it)) } })
            put("events", JSONArray().apply { snapshot.events.forEach { put(it.toJson()) } })
            put("outbox", JSONArray().apply { snapshot.outbox.forEach { put(it.toJson()) } })
        }
        // Write-then-rename: a crash mid-write leaves the previous good file.
        val temp = File(file.parentFile, file.name + ".tmp")
        temp.parentFile?.mkdirs()
        temp.writeText(json.toString())
        if (!temp.renameTo(file)) {
            file.writeText(json.toString())
            temp.delete()
        }
    }

    private fun readEvents(array: JSONArray?): List<NostrEvent> {
        if (array == null) return emptyList()
        return (0 until array.length()).mapNotNull { i ->
            runCatching { NostrEvent.fromJson(array.getJSONObject(i)) }.getOrNull()
        }
    }

    private fun readChannels(array: JSONArray?): List<Channel> {
        if (array == null) return emptyList()
        return (0 until array.length()).mapNotNull { i ->
            runCatching {
                val o = array.getJSONObject(i)
                Channel(
                    id = o.getString("id"),
                    name = o.optString("name", ""),
                    type = o.optString("type", "stream"),
                    topic = o.optString("topic", "").ifBlank { null },
                    about = o.optString("about", "").ifBlank { null },
                    purpose = o.optString("purpose", "").ifBlank { null },
                    archived = o.optBoolean("archived", false),
                    memberPubkeys = o.optJSONArray("members")?.let { m ->
                        (0 until m.length()).map { m.getString(it) }
                    } ?: emptyList(),
                )
            }.getOrNull()
        }
    }

    private fun writeChannel(channel: Channel): JSONObject = JSONObject().apply {
        put("id", channel.id)
        put("name", channel.name)
        put("type", channel.type)
        channel.topic?.let { put("topic", it) }
        channel.about?.let { put("about", it) }
        channel.purpose?.let { put("purpose", it) }
        put("archived", channel.archived)
        put("members", JSONArray().apply { channel.memberPubkeys.forEach { put(it) } })
    }

    companion object {
        const val FORMAT_VERSION = 1
        const val FILE_NAME = "deck-cache.json"
    }
}
