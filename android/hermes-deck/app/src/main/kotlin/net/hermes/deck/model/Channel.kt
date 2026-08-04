package net.hermes.deck.model

import net.hermes.deck.nostr.Kind
import net.hermes.deck.nostr.NostrEvent

/**
 * A Buzz channel — what the deck presents as a "project".
 *
 * Metadata lives in `kind:39000` keyed by the `d` tag, membership in `kind:39002`.
 * The two arrive separately, so a channel we are a member of but have no
 * metadata for still has to render; it falls back to its id.
 */
data class Channel(
    val id: String,
    val name: String,
    val type: String,
    val topic: String?,
    val about: String?,
    /** Buzz's "what does NOT belong here" note — worth showing when filing. */
    val purpose: String?,
    val archived: Boolean,
    val memberPubkeys: List<String>,
) {
    val isStream: Boolean get() = type == "stream"
    val isDirectMessage: Boolean get() = type == "dm"

    /** DMs and archived channels are noise in a project list. */
    val usableAsProject: Boolean get() = isStream && !archived

    val displayName: String get() = name.ifBlank { id.take(8) }

    companion object {
        fun fromMetadata(event: NostrEvent, members: List<String> = emptyList()): Channel? {
            if (event.kind != Kind.CHANNEL_METADATA) return null
            val id = event.tag("d") ?: return null
            return Channel(
                id = id,
                name = event.tag("name").orEmpty(),
                // Buzz falls back to a dm when the channel is marked hidden.
                type = event.tag("t") ?: if (event.tag("hidden") != null) "dm" else "stream",
                topic = event.tag("topic"),
                about = event.tag("about"),
                purpose = event.tag("purpose"),
                // Only the archived channels carry the tag at all; its value is
                // not what marks them.
                archived = event.tag("archived") != null,
                memberPubkeys = members,
            )
        }

        /** Channel ids from a `kind:39002` membership event. */
        fun membershipChannelId(event: NostrEvent): String? =
            if (event.kind == Kind.CHANNEL_MEMBERS) event.tag("d") else null

        fun membersOf(event: NostrEvent): List<String> =
            if (event.kind == Kind.CHANNEL_MEMBERS) event.tagValues("p") else emptyList()
    }
}
