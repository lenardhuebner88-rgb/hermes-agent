package net.hermes.deck.model

import net.hermes.deck.nostr.Hex
import net.hermes.deck.nostr.Kind
import net.hermes.deck.nostr.NostrEvent
import net.hermes.deck.nostr.RelayProtocol
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class TaskAssemblerTest {

    private val sk = Hex.decode("b7e151628aed2a6abf7158809cf4f3c762e7160f38b4da56a784d9045190cfef")
    private val channel = "ccd1bdf8-54b1-44ae-abee-b372b82b5196"

    private fun root(
        title: String,
        status: TaskStatus = TaskStatus.OPEN,
        priority: TaskPriority = TaskPriority.NORMAL,
        createdAt: Long,
    ) = NostrEvent.signed(
        sk, Kind.CHANNEL_MESSAGE, TaskCodec.buildContent(title, ""),
        TaskCodec.tagsFor(channel, status, priority, null, emptyList(), emptyList()), createdAt,
    )

    private fun edit(target: NostrEvent, status: TaskStatus, createdAt: Long): NostrEvent {
        val task = TaskCodec.fromEvent(target)!!.copy(status = status)
        return NostrEvent.signed(
            sk, Kind.MESSAGE_EDIT, TaskCodec.buildContent(task.title, task.body),
            TaskCodec.editEventTags(task), createdAt,
        )
    }

    private fun reply(rootId: String, createdAt: Long) = NostrEvent.signed(
        sk, Kind.CHANNEL_MESSAGE, "Antwort",
        listOf(
            listOf("h", channel),
            listOf("e", rootId, "", "root"),
            listOf("e", rootId, "", "reply"),
        ),
        createdAt,
    )

    @Test
    fun `roots become tasks and chatter is ignored`() {
        val chatter = NostrEvent.signed(
            sk, Kind.CHANNEL_MESSAGE, "Geplauder", listOf(listOf("h", channel)), 100,
        )
        val tasks = TaskAssembler.assemble(listOf(root("Eins", createdAt = 100), chatter))
        assertEquals(1, tasks.size)
        assertEquals("Eins", tasks.first().title)
    }

    @Test
    fun `edits apply oldest first regardless of arrival order`() {
        val r = root("Eins", createdAt = 100)
        val shuffled = listOf(
            edit(r, TaskStatus.DONE, 300),
            r,
            edit(r, TaskStatus.DOING, 200),
        )
        // Applied newest-first, the DOING edit would be dropped by the monotonic
        // guard and the task would end up DOING instead of DONE.
        assertEquals(TaskStatus.DONE, TaskAssembler.assemble(shuffled).first().status)
    }

    @Test
    fun `a deletion removes the task no matter when it arrives`() {
        val r = root("Weg damit", createdAt = 100)
        val deletion = NostrEvent.signed(
            sk, Kind.DELETE, "", listOf(listOf("e", r.id), listOf("h", channel)), 50,
        )
        assertTrue(TaskAssembler.assemble(listOf(deletion, r)).isEmpty())
    }

    @Test
    fun `replies are counted and the newest one dates the task`() {
        val r = root("Mit Diskussion", createdAt = 100)
        val tasks = TaskAssembler.assemble(listOf(r, reply(r.id, 150), reply(r.id, 400)))
        assertEquals(2, tasks.first().replyCount)
        assertEquals(400L, tasks.first().lastReplyAt)
    }

    @Test
    fun `a task without replies reports none rather than zero-dated`() {
        val tasks = TaskAssembler.assemble(listOf(root("Allein", createdAt = 100)))
        assertEquals(0, tasks.first().replyCount)
        assertNull(tasks.first().lastReplyAt)
    }

    @Test
    fun `ordering puts open urgent work first and done work last`() {
        val events = listOf(
            root("Erledigt", TaskStatus.DONE, TaskPriority.URGENT, 900),
            root("Normal offen", TaskStatus.OPEN, TaskPriority.NORMAL, 800),
            root("Dringend offen", TaskStatus.OPEN, TaskPriority.URGENT, 100),
        )
        val titles = TaskAssembler.assemble(events).map { it.title }
        assertEquals(listOf("Dringend offen", "Normal offen", "Erledigt"), titles)
    }

    @Test
    fun `a fresh reply lifts a task above an older untouched one`() {
        val old = root("Alt", createdAt = 100)
        val older = root("Aelter", createdAt = 50)
        val tasks = TaskAssembler.assemble(listOf(old, older, reply(older.id, 999)))
        assertEquals("Aelter", tasks.first().title)
    }

    @Test
    fun `status counts cover every status even when empty`() {
        val counts = TaskAssembler.countByStatus(
            TaskAssembler.assemble(listOf(root("Eins", TaskStatus.DOING, createdAt = 1))),
        )
        assertEquals(TaskStatus.entries.size, counts.size)
        assertEquals(1, counts[TaskStatus.DOING])
        assertEquals(0, counts[TaskStatus.DONE])
    }

    @Test
    fun `an edit for an unknown task does not invent one`() {
        val ghost = root("Nie eingespielt", createdAt = 100)
        val tasks = TaskAssembler.assemble(listOf(edit(ghost, TaskStatus.DONE, 200)))
        assertTrue(tasks.isEmpty())
    }

    @Test
    fun `the deck marker really is the single-letter t tag the relay can filter`() {
        // If this ever drifts to a longer tag name the relay would silently stop
        // filtering and every sync would drag the whole channel down.
        val tags = TaskCodec.tagsFor(channel, TaskStatus.OPEN, TaskPriority.NORMAL, null, emptyList(), emptyList())
        val marker = tags.first { it[0] == "t" }
        assertEquals(1, marker[0].length)
        assertEquals(RelayProtocol.DECK_TASK_MARKER, marker[1])
    }
}
