package net.hermes.deck.model

import net.hermes.deck.nostr.Kind
import net.hermes.deck.nostr.NostrEvent
import org.json.JSONObject

/**
 * A `kind:0` profile: the difference between "7d122ae6… antwortete" and
 * "Claude antwortete".
 *
 * Buzz shows names, so a companion that shows hex is strictly worse than the
 * thing it means to complement. Everything an agent or a person is called on
 * screen resolves through here.
 */
data class Profile(
    val pubkey: String,
    val name: String?,
    val picture: String?,
    val about: String?,
) {
    /** Never blank: falls back to the short pubkey so a row always has a label. */
    val label: String get() = name?.takeIf { it.isNotBlank() } ?: shortKey(pubkey)

    companion object {
        /**
         * `content` is a JSON *string* inside the event, not an object — a
         * profile whose content is malformed must not take the whole roster
         * down with it, so parsing failures yield null and the caller keeps
         * the short key.
         */
        fun fromEvent(event: NostrEvent): Profile? {
            if (event.kind != Kind.PROFILE) return null
            val json = runCatching { JSONObject(event.content) }.getOrNull() ?: return null
            return Profile(
                pubkey = event.pubkey,
                // NIP-01 says `name`; many clients also set `display_name`, and
                // Buzz's agents are registered with the latter. Preferring the
                // display name matches what the chat shows.
                name = json.optNullable("display_name") ?: json.optNullable("name"),
                picture = json.optNullable("picture"),
                about = json.optNullable("about"),
            )
        }

        /**
         * Keeps the newest profile per pubkey.
         *
         * Relays hand back every historical `kind:0` for an author, and the
         * order is not guaranteed. Taking the last one seen would let a
         * superseded name win at random between syncs.
         */
        fun newestByPubkey(events: Collection<NostrEvent>): Map<String, Profile> {
            val newest = HashMap<String, Pair<Long, Profile>>()
            for (event in events) {
                val profile = fromEvent(event) ?: continue
                val current = newest[event.pubkey]
                if (current == null || event.createdAt > current.first) {
                    newest[event.pubkey] = event.createdAt to profile
                }
            }
            return newest.mapValues { it.value.second }
        }

        fun shortKey(pubkey: String): String =
            if (pubkey.length <= 8) pubkey else pubkey.take(8)

        /** The label to show for a pubkey, with or without a known profile. */
        fun labelFor(pubkey: String, profiles: Map<String, Profile>): String =
            profiles[pubkey]?.label ?: shortKey(pubkey)

        private fun JSONObject.optNullable(key: String): String? =
            if (isNull(key)) null else optString(key).ifBlank { null }
    }
}
