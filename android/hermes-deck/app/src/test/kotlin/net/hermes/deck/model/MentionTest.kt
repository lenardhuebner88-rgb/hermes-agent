package net.hermes.deck.model

import net.hermes.deck.nostr.Kind
import net.hermes.deck.nostr.NostrEvent
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class MentionTest {

    private val me = "a".repeat(64)
    private val claude = "b".repeat(64)
    private val now = 1_800_000_000L

    private fun event(
        id: String,
        author: String,
        createdAt: Long,
        channel: String = "chan-1",
        mentions: List<String> = emptyList(),
        content: String = "text",
        extraTags: List<List<String>> = emptyList(),
        kind: Int = Kind.CHANNEL_MESSAGE,
    ) = NostrEvent(
        id = id,
        pubkey = author,
        createdAt = createdAt,
        kind = kind,
        tags = buildList {
            add(listOf("h", channel))
            mentions.forEach { add(listOf("p", it)) }
            addAll(extraTags)
        },
        content = content,
        sig = "",
    )

    private val channels = listOf(
        Channel(
            id = "chan-1",
            name = "agent-lab",
            type = "stream",
            topic = null,
            about = null,
            purpose = null,
            archived = false,
            memberPubkeys = emptyList(),
        ),
    )

    private val profiles = mapOf(claude to Profile(claude, "Claude", null, null))

    @Test
    fun `a mention after my last word waits, and carries what the link needs`() {
        val entries = Mention.of(
            listOf(
                event("mine", me, now - 400),
                event("m1", claude, now - 100, mentions = listOf(me), content = "**Kurz?**"),
            ),
            channels,
            profiles,
            me,
        )

        assertEquals(1, entries.size)
        val entry = entries[0]
        assertEquals("m1", entry.eventId)
        assertEquals("chan-1", entry.channelId)
        assertEquals("agent-lab", entry.channelName)
        assertEquals("Claude", entry.authorLabel)
        // Markdown markers stripped like everywhere else on this screen.
        assertEquals("Kurz?", entry.text)
    }

    @Test
    fun `a mention that is a direct thread reply carries the parent as its root`() {
        // The blocker this feature turned on: Buzz tags a direct reply to a
        // thread head with only ["e", id, "", "reply"]. A fixture that politely
        // sets a `root` marker is green while the shipped link is wrong.
        val root = "r".repeat(64)
        val entries = Mention.of(
            listOf(
                event(
                    "m1", claude, now - 100, mentions = listOf(me),
                    extraTags = listOf(listOf("e", root, "", "reply")),
                ),
            ),
            channels,
            profiles,
            me,
        )

        assertEquals(root, entries[0].threadRootId)
    }

    @Test
    fun `a top-level mention has no thread root`() {
        val entries = Mention.of(
            listOf(event("m1", claude, now - 100, mentions = listOf(me))),
            channels,
            profiles,
            me,
        )

        assertNull(entries[0].threadRootId)
    }

    @Test
    fun `a mention older than my own last word in that channel is answered`() {
        val entries = Mention.of(
            listOf(
                event("m1", claude, now - 500, mentions = listOf(me)),
                event("mine", me, now - 200),
            ),
            channels,
            profiles,
            me,
        )

        assertTrue(entries.isEmpty())
    }

    @Test
    fun `speaking in one channel says nothing about another`() {
        val entries = Mention.of(
            listOf(
                event("mine", me, now - 400, channel = "chan-1"),
                event("m1", claude, now - 100, channel = "chan-1", mentions = listOf(me)),
                event("m2", claude, now - 900, channel = "chan-2", mentions = listOf(me)),
            ),
            channels,
            profiles,
            me,
        )

        assertEquals(2, entries.size)
        // Newest first, so the sheet reads like the rest of the screen.
        assertEquals(listOf("m1", "m2"), entries.map { it.eventId })
        // The unknown channel is named by its short id rather than dropped.
        assertEquals("chan-2", entries[1].channelId)
    }

    @Test
    fun `my own mention of myself is not something waiting on me`() {
        val entries = Mention.of(
            listOf(event("m1", me, now - 100, mentions = listOf(me))),
            channels,
            profiles,
            me,
        )

        assertTrue(entries.isEmpty())
    }

    @Test
    fun `non-message kinds are ignored`() {
        val entries = Mention.of(
            listOf(event("m1", claude, now - 100, mentions = listOf(me), kind = Kind.REACTION)),
            channels,
            profiles,
            me,
        )

        assertTrue(entries.isEmpty())
    }

    @Test
    fun `without an identity nothing is claimed to be waiting`() {
        val entries = Mention.of(
            listOf(event("m1", claude, now - 100, mentions = listOf(me))),
            channels,
            profiles,
            null,
        )

        assertTrue(entries.isEmpty())
    }

    @Test
    fun `the chip count is the size of this very list`() {
        // Chip and sheet must not be able to disagree.
        val events = listOf(
            event("mine", me, now - 400),
            event("m1", claude, now - 100, mentions = listOf(me)),
            event("m2", claude, now - 50, channel = "chan-2", mentions = listOf(me)),
        )

        assertEquals(
            Mention.of(events, channels, profiles, me).size,
            ThreadActivity.unansweredMentions(events, me),
        )
    }
}
