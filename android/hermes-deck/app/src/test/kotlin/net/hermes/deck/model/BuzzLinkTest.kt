package net.hermes.deck.model

import net.hermes.deck.nostr.Kind
import net.hermes.deck.nostr.NostrEvent
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

class BuzzLinkTest {

    private val channel = "57005de3-cc8d-47fb-a799-cb0cfbd01e2e"
    private val event = "a".repeat(64)
    private val root = "b".repeat(64)

    private fun message(
        id: String = event,
        tags: List<List<String>> = listOf(listOf("h", channel)),
    ) = NostrEvent(
        id = id,
        pubkey = "c".repeat(64),
        createdAt = 1_800_000_000,
        kind = Kind.CHANNEL_MESSAGE,
        tags = tags,
        content = "text",
        sig = "",
    )

    @Test
    fun `the canonical form matches what Buzz's own clients build`() {
        assertEquals(
            "buzz://message?channel=$channel&id=$event&thread=$root",
            BuzzLink.message(channel, event, root),
        )
    }

    @Test
    fun `thread is omitted, not sent empty`() {
        assertEquals(
            "buzz://message?channel=$channel&id=$event",
            BuzzLink.message(channel, event, null),
        )
        assertEquals(
            "buzz://message?channel=$channel&id=$event",
            BuzzLink.message(channel, event, ""),
        )
    }

    @Test
    fun `a link that cannot be addressed completely is null, not half built`() {
        // Buzz's parser rejects the half form and shows nothing at all, so the
        // failure has to surface here rather than silently at the far end.
        assertNull(BuzzLink.message(null, event))
        assertNull(BuzzLink.message("", event))
        assertNull(BuzzLink.message(channel, null))
        assertNull(BuzzLink.message(channel, ""))
    }

    @Test
    fun `a reply-only e-tag still yields the thread root`() {
        // The defect this whole feature turned on. Buzz tags a direct reply to a
        // thread head with ONLY ["e", id, "", "reply"] and no root marker, and
        // reads it back as rootTag ?? parentId. Reading just the root marker
        // gives null here, the link goes out without `thread`, and Buzz looks in
        // the top-level timeline where a thread reply does not live.
        val reply = message(tags = listOf(listOf("h", channel), listOf("e", root, "", "reply")))

        assertEquals(root, BuzzLink.threadRootOf(reply))
        assertEquals(
            "buzz://message?channel=$channel&id=$event&thread=$root",
            BuzzLink.message(reply),
        )
    }

    @Test
    fun `a nested reply uses the root marker, not the parent`() {
        val parent = "d".repeat(64)
        val nested = message(
            tags = listOf(
                listOf("h", channel),
                listOf("e", root, "", "root"),
                listOf("e", parent, "", "reply"),
            ),
        )

        assertEquals(root, BuzzLink.threadRootOf(nested))
    }

    @Test
    fun `a top-level message has no thread root`() {
        assertEquals(null, BuzzLink.threadRootOf(message()))
        assertEquals("buzz://message?channel=$channel&id=$event", BuzzLink.message(message()))
    }

    @Test
    fun `an event pointing at itself is its own root, so no thread is sent`() {
        // Buzz clients do emit self-referential e-tags; `thread=<the message
        // itself>` would be a link to a thread that does not exist.
        val selfRooted = message(tags = listOf(listOf("h", channel), listOf("e", event, "", "root")))

        assertNull(BuzzLink.threadRootOf(selfRooted))
    }

    @Test
    fun `a space encodes as percent-twenty, not plus`() {
        // URLEncoder is form encoding and would emit `+`, which Dart's Uri reads
        // back as a literal plus. Today's ids never contain a space; the next
        // caller's values might.
        val link = BuzzLink.message("a b", event)

        assertEquals("buzz://message?channel=a%20b&id=$event", link)
    }
}
