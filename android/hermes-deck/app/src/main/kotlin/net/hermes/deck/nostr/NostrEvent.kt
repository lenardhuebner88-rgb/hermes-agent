package net.hermes.deck.nostr

import java.security.MessageDigest
import org.json.JSONArray
import org.json.JSONObject

/**
 * A NIP-01 event.
 *
 * [id] is sha256 over the canonical serialization `[0,pubkey,created_at,kind,tags,content]`,
 * and [sig] is a BIP-340 signature over that id. Both are reproduced from the
 * other fields, never trusted from the wire — [verify] is what tells us a relay
 * payload is genuine.
 */
data class NostrEvent(
    val id: String,
    val pubkey: String,
    val createdAt: Long,
    val kind: Int,
    val tags: List<List<String>>,
    val content: String,
    val sig: String,
) {
    /** First value of the first tag with this name, or null. */
    fun tag(name: String): String? =
        tags.firstOrNull { it.size >= 2 && it[0] == name }?.get(1)

    /** All values of every tag with this name. */
    fun tagValues(name: String): List<String> =
        tags.filter { it.size >= 2 && it[0] == name }.map { it[1] }

    /** The channel this event belongs to (NIP-29 `h` tag). */
    val channelId: String? get() = tag("h")

    /**
     * The event this one replies to, and the thread root.
     * Buzz marks them with the NIP-10 marker in position 3.
     */
    val replyTo: String? get() = markedEventTag("reply")
    val threadRoot: String? get() = markedEventTag("root")

    private fun markedEventTag(marker: String): String? =
        tags.firstOrNull { it.size >= 4 && it[0] == "e" && it[3] == marker }?.get(1)

    fun toJson(): JSONObject = JSONObject().apply {
        put("id", id)
        put("pubkey", pubkey)
        put("created_at", createdAt)
        put("kind", kind)
        put("tags", tagsToJson(tags))
        put("content", content)
        put("sig", sig)
    }

    fun verify(): Boolean {
        if (computeId(pubkey, createdAt, kind, tags, content) != id) return false
        return runCatching {
            Schnorr.verify(Hex.decode(id), Hex.decode(pubkey), Hex.decode(sig))
        }.getOrDefault(false)
    }

    companion object {

        /**
         * The NIP-01 canonical form. Hand-rolled on purpose: `org.json` escapes
         * `/` after `<` and turns assorted Unicode ranges into `\uXXXX`, both of
         * which change the hash. The relay would then reject — or worse, other
         * clients would compute a different id for the same event.
         */
        fun serializeForId(
            pubkey: String,
            createdAt: Long,
            kind: Int,
            tags: List<List<String>>,
            content: String,
        ): String {
            val sb = StringBuilder()
            sb.append("[0,\"").append(pubkey).append("\",")
            sb.append(createdAt).append(',').append(kind).append(",[")
            tags.forEachIndexed { i, tag ->
                if (i > 0) sb.append(',')
                sb.append('[')
                tag.forEachIndexed { j, value ->
                    if (j > 0) sb.append(',')
                    escapeJsonString(value, sb)
                }
                sb.append(']')
            }
            sb.append("],")
            escapeJsonString(content, sb)
            sb.append(']')
            return sb.toString()
        }

        /** NIP-01 escaping: only these six sequences, plus \u00XX for other control chars. */
        private fun escapeJsonString(value: String, sb: StringBuilder) {
            sb.append('"')
            for (c in value) {
                when (c) {
                    '"' -> sb.append("\\\"")
                    '\\' -> sb.append("\\\\")
                    '\n' -> sb.append("\\n")
                    '\r' -> sb.append("\\r")
                    '\t' -> sb.append("\\t")
                    '\b' -> sb.append("\\b")
                    // Kotlin has no '\f' escape sequence; spell formfeed out.
                    '\u000C' -> sb.append("\\f")
                    else -> if (c < ' ') {
                        sb.append(String.format("\\u%04x", c.code))
                    } else {
                        sb.append(c)
                    }
                }
            }
            sb.append('"')
        }

        fun computeId(
            pubkey: String,
            createdAt: Long,
            kind: Int,
            tags: List<List<String>>,
            content: String,
        ): String {
            val canonical = serializeForId(pubkey, createdAt, kind, tags, content)
            val digest = MessageDigest.getInstance("SHA-256")
            return Hex.encode(digest.digest(canonical.toByteArray(Charsets.UTF_8)))
        }

        /** Builds, ids and signs an event in one step. */
        fun signed(
            secretKey: ByteArray,
            kind: Int,
            content: String,
            tags: List<List<String>>,
            createdAt: Long = System.currentTimeMillis() / 1000,
        ): NostrEvent {
            val pubkey = Hex.encode(Schnorr.publicKey(secretKey))
            val id = computeId(pubkey, createdAt, kind, tags, content)
            val sig = Hex.encode(Schnorr.sign(Hex.decode(id), secretKey))
            return NostrEvent(id, pubkey, createdAt, kind, tags, content, sig)
        }

        fun fromJson(json: JSONObject): NostrEvent = NostrEvent(
            id = json.getString("id"),
            pubkey = json.getString("pubkey"),
            createdAt = json.getLong("created_at"),
            kind = json.getInt("kind"),
            tags = tagsFromJson(json.optJSONArray("tags")),
            content = json.optString("content", ""),
            sig = json.optString("sig", ""),
        )

        fun tagsFromJson(array: JSONArray?): List<List<String>> {
            if (array == null) return emptyList()
            return (0 until array.length()).map { i ->
                val tag = array.getJSONArray(i)
                (0 until tag.length()).map { j -> tag.getString(j) }
            }
        }

        fun tagsToJson(tags: List<List<String>>): JSONArray {
            val outer = JSONArray()
            tags.forEach { tag ->
                val inner = JSONArray()
                tag.forEach { inner.put(it) }
                outer.put(inner)
            }
            return outer
        }
    }
}

/** The event kinds this client reads or writes. */
object Kind {
    const val PROFILE = 0
    const val DELETE = 5
    const val REACTION = 7
    const val CHANNEL_MESSAGE = 9
    const val AUTH = 22242
    const val BLOSSOM_AUTH = 24242
    const val CHANNEL_METADATA = 39000
    const val CHANNEL_MEMBERS = 39002
    const val MESSAGE_EDIT = 40003
}
