package net.hermes.deck.model

import net.hermes.deck.nostr.Kind
import net.hermes.deck.nostr.NostrEvent
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class ThreadActivityTest {

    private val me = "a".repeat(64)
    private val claude = "b".repeat(64)
    private val now = 1_800_000_000L

    private fun event(
        id: String,
        author: String,
        createdAt: Long,
        root: String? = null,
        content: String = "text",
        kind: Int = Kind.CHANNEL_MESSAGE,
        channel: String = "chan-1",
        mentions: List<String> = emptyList(),
    ) = NostrEvent(
        id = id,
        pubkey = author,
        createdAt = createdAt,
        kind = kind,
        tags = buildList {
            add(listOf("h", channel))
            root?.let {
                add(listOf("e", it, "", "root"))
                add(listOf("e", it, "", "reply"))
            }
            mentions.forEach { add(listOf("p", it)) }
        },
        content = content,
        sig = "",
    )

    private fun task(id: String, title: String, channel: String = "chan-1") = DeckTask(
        id = id,
        channelId = channel,
        authorPubkey = me,
        title = title,
        body = "",
        status = TaskStatus.OPEN,
        priority = TaskPriority.NORMAL,
        due = null,
        createdAt = now - 100_000,
        updatedAt = now - 100_000,
        attachments = emptyList(),
        assignees = emptyList(),
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

    private val profiles = mapOf(
        claude to Profile(claude, "Claude", null, null),
    )

    @Test
    fun `a reply lifts its task into the active list with the speaker's name`() {
        val entries = ThreadActivity.of(
            events = listOf(event("r1", claude, now - 120, root = "t1", content = "Bin dran.")),
            tasks = listOf(task("t1", "Deck bauen")),
            channels = channels,
            profiles = profiles,
            me = me,
            now = now,
        )

        assertEquals(1, entries.size)
        assertEquals("Deck bauen", entries[0].title)
        // The whole point of kind:0: a name, not "b0b0b0b0".
        assertEquals("Claude", entries[0].lastAuthorLabel)
        assertEquals("agent-lab", entries[0].channelName)
        assertEquals("Bin dran.", entries[0].lastText)
    }

    @Test
    fun `an unknown author still yields a row, labelled with the short key`() {
        val stranger = "c".repeat(64)
        val entries = ThreadActivity.of(
            events = listOf(event("r1", stranger, now - 60, root = "t1")),
            tasks = listOf(task("t1", "T")),
            channels = channels,
            profiles = profiles,
            me = me,
            now = now,
        )

        // Dropping the row for want of a name would hide real activity.
        assertEquals(1, entries.size)
        assertEquals("cccccccc", entries[0].lastAuthorLabel)
    }

    @Test
    fun `only the newest reply per thread decides the row, but all are counted`() {
        val entries = ThreadActivity.of(
            events = listOf(
                event("r1", me, now - 500, root = "t1", content = "erste"),
                event("r2", claude, now - 100, root = "t1", content = "letzte"),
                event("r3", me, now - 300, root = "t1", content = "mittlere"),
            ),
            tasks = listOf(task("t1", "T")),
            channels = channels,
            profiles = profiles,
            me = me,
            now = now,
        )

        assertEquals(3, entries[0].replyCount)
        assertEquals("letzte", entries[0].lastText)
        assertEquals(now - 100, entries[0].lastAt)
    }

    @Test
    fun `chatter outside the window is not active`() {
        val entries = ThreadActivity.of(
            events = listOf(event("r1", claude, now - 7200, root = "t1")),
            tasks = listOf(task("t1", "T")),
            channels = channels,
            profiles = profiles,
            me = me,
            now = now,
            withinSeconds = 3600,
        )

        assertTrue(entries.isEmpty())
    }

    @Test
    fun `a reply whose root is not a known task is dropped, not guessed at`() {
        val entries = ThreadActivity.of(
            events = listOf(event("r1", claude, now - 60, root = "unbekannt")),
            tasks = listOf(task("t1", "T")),
            channels = channels,
            profiles = profiles,
            me = me,
            now = now,
        )

        assertTrue(entries.isEmpty())
    }

    @Test
    fun `a task root without reply tags is not chatter`() {
        val entries = ThreadActivity.of(
            events = listOf(event("t1", me, now - 60)),
            tasks = listOf(task("t1", "T")),
            channels = channels,
            profiles = profiles,
            me = me,
            now = now,
        )

        assertTrue(entries.isEmpty())
    }

    @Test
    fun `an event that names itself as root is not a reply to itself`() {
        // Buzz clients do emit self-referential `e` tags. Counted as a reply,
        // every task with one would sit permanently in "gerade im Gespräch"
        // and the list would stop meaning anything.
        val entries = ThreadActivity.of(
            events = listOf(event("t1", me, now - 60, root = "t1")),
            tasks = listOf(task("t1", "T")),
            channels = channels,
            profiles = profiles,
            me = me,
            now = now,
        )

        assertTrue(entries.isEmpty())
    }

    @Test
    fun `my own last word means the thread is not waiting on me`() {
        val mine = ThreadActivity.of(
            events = listOf(event("r1", me, now - 60, root = "t1")),
            tasks = listOf(task("t1", "T")),
            channels = channels, profiles = profiles, me = me, now = now,
        )
        val theirs = ThreadActivity.of(
            events = listOf(event("r1", claude, now - 60, root = "t1")),
            tasks = listOf(task("t1", "T")),
            channels = channels, profiles = profiles, me = me, now = now,
        )

        assertFalse(mine[0].awaitingMe)
        assertTrue(theirs[0].awaitingMe)
    }

    @Test
    fun `newest thread comes first and the list is capped`() {
        val tasks = (1..10).map { task("t$it", "T$it") }
        val events = (1..10).map { event("r$it", claude, now - it * 10L, root = "t$it") }

        val entries = ThreadActivity.of(
            events = events, tasks = tasks, channels = channels,
            profiles = profiles, me = me, now = now, limit = 3,
        )

        assertEquals(3, entries.size)
        assertEquals(listOf("t1", "t2", "t3"), entries.map { it.taskId })
    }

    @Test
    fun `non-message kinds are ignored`() {
        val entries = ThreadActivity.of(
            events = listOf(event("r1", claude, now - 60, root = "t1", kind = Kind.REACTION)),
            tasks = listOf(task("t1", "T")),
            channels = channels, profiles = profiles, me = me, now = now,
        )

        assertTrue(entries.isEmpty())
    }

    @Test
    fun `only the first line of a long reply reaches the row`() {
        val entries = ThreadActivity.of(
            events = listOf(
                event("r1", claude, now - 60, root = "t1", content = "Kurzfassung\nDetails\nMehr"),
            ),
            tasks = listOf(task("t1", "T")),
            channels = channels, profiles = profiles, me = me, now = now,
        )

        assertEquals("Kurzfassung", entries[0].lastText)
    }
}
