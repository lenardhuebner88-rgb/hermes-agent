package net.hermes.deck.model

import net.hermes.deck.nostr.Kind
import net.hermes.deck.nostr.NostrEvent

/**
 * The messages that name the operator and have not been answered yet.
 *
 * The count under "Wartet auf dich" and the list behind it are the same
 * derivation, deliberately: a chip that says 10 and a sheet that shows 8 is a
 * bug nobody notices until it matters.
 *
 * "Unanswered" means: newer than the operator's own last word in that same
 * channel. A plain count over the sync window only ever rises — measured
 * against the real workspace it read 63, the oldest 133 hours old — which is a
 * claim about history, not about waiting.
 */
object Mention {

    data class Entry(
        val eventId: String,
        val channelId: String,
        /** Null when the mention is itself a top-level message. */
        val threadRootId: String?,
        val channelName: String,
        val authorPubkey: String,
        val authorLabel: String,
        val text: String,
        val at: Long,
    )

    fun of(
        events: Collection<NostrEvent>,
        channels: Collection<Channel>,
        profiles: Map<String, Profile>,
        me: String?,
    ): List<Entry> {
        if (me == null) return emptyList()
        val messages = events.filter { it.kind == Kind.CHANNEL_MESSAGE }
        val channelNames = channels.associate { it.id to it.displayName }

        val myLastWord = HashMap<String, Long>()
        for (event in messages) {
            if (event.pubkey != me) continue
            val channel = event.channelId ?: continue
            if (event.createdAt > (myLastWord[channel] ?: Long.MIN_VALUE)) {
                myLastWord[channel] = event.createdAt
            }
        }

        return messages
            .filter { event ->
                event.pubkey != me &&
                    me in event.tagValues("p") &&
                    event.createdAt > (myLastWord[event.channelId] ?: Long.MIN_VALUE)
            }
            .map { event ->
                val channelId = event.channelId.orEmpty()
                Entry(
                    eventId = event.id,
                    channelId = channelId,
                    // Derived the way Buzz derives it, not the way the tag reads.
                    // See BuzzLink.threadRootOf for why that difference matters.
                    threadRootId = BuzzLink.threadRootOf(event),
                    channelName = channelNames[channelId] ?: channelId.take(8),
                    authorPubkey = event.pubkey,
                    authorLabel = Profile.labelFor(event.pubkey, profiles),
                    text = Preview.line(event.content),
                    at = event.createdAt,
                )
            }
            .sortedByDescending { it.at }
    }
}
