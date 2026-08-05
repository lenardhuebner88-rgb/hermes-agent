package net.hermes.deck.model

import java.net.URLEncoder
import net.hermes.deck.nostr.NostrEvent

/**
 * Links that hand a message over to the Buzz app.
 *
 * Reading a conversation belongs to Buzz, so the deck's job ends at pointing
 * precisely at the right message. The shape is not invented here — it is the
 * contract Buzz's own clients use, read off
 * `mobile/lib/shared/deeplink/deep_link.dart`:
 *
 *     buzz://message?channel=<uuid>&id=<eventId>[&thread=<rootId>]
 *
 * `channel` and `id` must be non-empty or Buzz's parser returns null and the
 * app shows nothing at all, so an incomplete link is not built in the first
 * place. `thread` is omitted rather than sent empty.
 */
object BuzzLink {

    const val SCHEME = "buzz"

    /**
     * The thread root of an event, the way *Buzz* derives it — `root` marker if
     * present, otherwise the `reply` target.
     *
     * This is the whole correctness of the feature. Buzz tags a direct reply to
     * a thread head with **only** `["e", id, "", "reply"]` and no `root` marker
     * (`mobile/lib/features/channels/send_message_provider.dart`, `_buildReplyTags`),
     * and reads it back as `rootId = rootTag ?? parentId`
     * (`mobile/lib/shared/relay/nostr_models.dart`). Reading just the `root`
     * marker therefore yields null for the most common kind of mention there is,
     * the link goes out without `thread`, and Buzz looks for the message in the
     * top-level timeline — where a thread reply does not live. No error, no
     * message, simply the wrong place. Found in review before it shipped.
     */
    fun threadRootOf(event: NostrEvent): String? =
        (event.threadRoot ?: event.replyTo)?.takeIf { it != event.id }

    /**
     * A link to one message, or null if it could not be addressed completely.
     *
     * Null rather than a half link: Buzz's parser rejects the half form anyway,
     * and it would fail silently at the far end instead of here.
     */
    fun message(channelId: String?, eventId: String?, threadRootId: String? = null): String? {
        if (channelId.isNullOrEmpty() || eventId.isNullOrEmpty()) return null
        val sb = StringBuilder("$SCHEME://message?channel=")
        sb.append(encode(channelId)).append("&id=").append(encode(eventId))
        if (!threadRootId.isNullOrEmpty()) sb.append("&thread=").append(encode(threadRootId))
        return sb.toString()
    }

    /** Convenience for an event we hold: derives the thread root the Buzz way. */
    fun message(event: NostrEvent): String? =
        message(event.channelId, event.id, threadRootOf(event))

    /**
     * Percent-encoding, `%20` not `+`.
     *
     * `URLEncoder` is form encoding and turns a space into `+`, which Dart's
     * `Uri` reads back as a literal plus. Channel ids and event ids are uuid and
     * hex today so nothing would encode — but a link builder that is only
     * correct for the values it happens to see is a trap for the next caller.
     */
    private fun encode(value: String): String =
        URLEncoder.encode(value, "UTF-8").replace("+", "%20")
}
