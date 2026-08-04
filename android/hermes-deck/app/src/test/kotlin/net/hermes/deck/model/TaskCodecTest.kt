package net.hermes.deck.model

import net.hermes.deck.nostr.Hex
import net.hermes.deck.nostr.Kind
import net.hermes.deck.nostr.NostrEvent
import net.hermes.deck.nostr.RelayProtocol
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class TaskCodecTest {

    private val sk = Hex.decode("b7e151628aed2a6abf7158809cf4f3c762e7160f38b4da56a784d9045190cfef")
    private val channel = "ccd1bdf8-54b1-44ae-abee-b372b82b5196"

    private fun rootEvent(
        title: String = "Modellwechsel mobil möglich machen",
        body: String = "Damit ich unterwegs umschalten kann.",
        status: TaskStatus = TaskStatus.OPEN,
        priority: TaskPriority = TaskPriority.HIGH,
        due: String? = "2026-08-09",
        attachments: List<Attachment> = emptyList(),
        assignees: List<String> = emptyList(),
        createdAt: Long = 1785880000,
        secretKey: ByteArray = sk,
    ) = NostrEvent.signed(
        secretKey = secretKey,
        kind = Kind.CHANNEL_MESSAGE,
        content = TaskCodec.buildContent(title, body),
        tags = TaskCodec.tagsFor(channel, status, priority, due, attachments, assignees),
        createdAt = createdAt,
    )

    @Test
    fun `a task round-trips through an event`() {
        val task = TaskCodec.fromEvent(rootEvent())!!
        assertEquals("Modellwechsel mobil möglich machen", task.title)
        assertEquals("Damit ich unterwegs umschalten kann.", task.body)
        assertEquals(TaskStatus.OPEN, task.status)
        assertEquals(TaskPriority.HIGH, task.priority)
        assertEquals("2026-08-09", task.due)
        assertEquals(channel, task.channelId)
    }

    @Test
    fun `the event stays a valid signed Buzz message`() {
        val event = rootEvent()
        assertTrue("a task must be a legitimate kind:9 message", event.verify())
        assertEquals(Kind.CHANNEL_MESSAGE, event.kind)
        assertEquals(channel, event.channelId)
        assertTrue(event.tagValues("t").contains(RelayProtocol.DECK_TASK_MARKER))
    }

    @Test
    fun `an ordinary channel message is not mistaken for a task`() {
        val chatter = NostrEvent.signed(
            secretKey = sk,
            kind = Kind.CHANNEL_MESSAGE,
            content = "Nur eine normale Nachricht",
            tags = listOf(listOf("h", channel)),
            createdAt = 1785880000,
        )
        assertFalse(TaskCodec.isTaskRoot(chatter))
        assertNull(TaskCodec.fromEvent(chatter))
    }

    @Test
    fun `a reply carrying the marker is not a second task`() {
        // Otherwise quoting a task in a thread would spawn a phantom entry.
        val reply = NostrEvent.signed(
            secretKey = sk,
            kind = Kind.CHANNEL_MESSAGE,
            content = "Antwort",
            tags = listOf(
                listOf("h", channel),
                listOf("t", RelayProtocol.DECK_TASK_MARKER),
                listOf("e", "rootid", "", "reply"),
            ),
            createdAt = 1785880100,
        )
        assertFalse(TaskCodec.isTaskRoot(reply))
    }

    @Test
    fun `an edit moves the task forward`() {
        val task = TaskCodec.fromEvent(rootEvent())!!
        val closed = task.copy(status = TaskStatus.DONE)
        val edit = NostrEvent.signed(
            secretKey = sk,
            kind = Kind.MESSAGE_EDIT,
            content = TaskCodec.buildContent(task.title, task.body),
            tags = TaskCodec.editEventTags(closed),
            createdAt = task.createdAt + 60,
        )
        val updated = TaskCodec.applyEdit(task, edit)
        assertEquals(TaskStatus.DONE, updated.status)
        assertEquals(task.createdAt + 60, updated.updatedAt)
    }

    @Test
    fun `an out-of-order edit cannot resurrect an older status`() {
        val task = TaskCodec.fromEvent(rootEvent())!!
        val newer = TaskCodec.applyEdit(
            task,
            NostrEvent.signed(
                sk, Kind.MESSAGE_EDIT, TaskCodec.buildContent(task.title, task.body),
                TaskCodec.editEventTags(task.copy(status = TaskStatus.DONE)), task.createdAt + 100,
            ),
        )
        assertEquals(TaskStatus.DONE, newer.status)

        val stale = TaskCodec.applyEdit(
            newer,
            NostrEvent.signed(
                sk, Kind.MESSAGE_EDIT, TaskCodec.buildContent(task.title, task.body),
                TaskCodec.editEventTags(task.copy(status = TaskStatus.OPEN)), task.createdAt + 50,
            ),
        )
        assertEquals("a late-arriving older edit must not win", TaskStatus.DONE, stale.status)
    }

    @Test
    fun `an edit from someone else is ignored`() {
        val task = TaskCodec.fromEvent(rootEvent())!!
        val strangerKey = Hex.decode("c90fdaa22168c234c4c6628b80dc1cd129024e088a67cc74020bbea63b14e5c9")
        val forged = NostrEvent.signed(
            strangerKey, Kind.MESSAGE_EDIT, TaskCodec.buildContent("Gekapert", ""),
            TaskCodec.editEventTags(task.copy(status = TaskStatus.DONE)), task.createdAt + 60,
        )
        assertEquals(TaskStatus.OPEN, TaskCodec.applyEdit(task, forged).status)
    }

    @Test
    fun `an edit aimed at a different task is ignored`() {
        val task = TaskCodec.fromEvent(rootEvent())!!
        val other = task.copy(id = "f".repeat(64), status = TaskStatus.DONE)
        val edit = NostrEvent.signed(
            sk, Kind.MESSAGE_EDIT, "Anderes", TaskCodec.editEventTags(other), task.createdAt + 60,
        )
        assertEquals(TaskStatus.OPEN, TaskCodec.applyEdit(task, edit).status)
    }

    @Test
    fun `attachments survive the imeta round-trip`() {
        val attachment = Attachment(
            url = "https://huebners.tail50819a.ts.net:9444/media/abc123.png",
            mime = "image/png",
            sha256 = "abc123",
            size = 20480,
            name = "screenshot.png",
        )
        val task = TaskCodec.fromEvent(rootEvent(attachments = listOf(attachment)))!!
        assertEquals(listOf(attachment), task.attachments)
        assertTrue(task.attachments.first().isImage)
    }

    @Test
    fun `assignees land in p tags so the agent is really mentioned`() {
        val agent = "f1d66f405a79e94579dfdd629dc1b2e09c7e6752bc2baa2867aa925c33db31e9"
        val event = rootEvent(assignees = listOf(agent))
        assertEquals(listOf(agent), event.tagValues("p"))
        assertEquals(listOf(agent), TaskCodec.fromEvent(event)!!.assignees)
    }

    @Test
    fun `title and body split on the first line only`() {
        assertEquals("Titel" to "Rest\n\nmit Absatz", TaskCodec.splitContent("Titel\nRest\n\nmit Absatz"))
        assertEquals("Nur Titel" to "", TaskCodec.splitContent("  Nur Titel  "))
        assertEquals("" to "", TaskCodec.splitContent("   "))
    }

    @Test
    fun `an empty title still yields something addressable`() {
        assertEquals("Ohne Titel", TaskCodec.splitContent(TaskCodec.buildContent("", "Text")).first)
    }

    @Test
    fun `unknown status and priority values fall back instead of throwing`() {
        val event = NostrEvent.signed(
            sk, Kind.CHANNEL_MESSAGE, "Titel",
            listOf(
                listOf("h", channel),
                listOf("t", RelayProtocol.DECK_TASK_MARKER),
                listOf(TaskCodec.TAG_STATUS, "erfunden"),
                listOf(TaskCodec.TAG_PRIORITY, "auchErfunden"),
            ),
            1785880000,
        )
        val task = TaskCodec.fromEvent(event)!!
        assertEquals(TaskStatus.INBOX, task.status)
        assertEquals(TaskPriority.NORMAL, task.priority)
    }
}
