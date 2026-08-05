package net.hermes.deck.model

import java.time.LocalDate
import org.junit.Assert.assertEquals
import org.junit.Test

class TaskFilterTest {

    private val today = LocalDate.of(2026, 8, 5)

    private fun task(
        id: String,
        channelId: String = "kanal-a",
        title: String = "Titel",
        body: String = "Rumpf",
        due: String? = null,
        status: TaskStatus = TaskStatus.OPEN,
    ) = DeckTask(
        id = id,
        channelId = channelId,
        authorPubkey = "ab".repeat(32),
        title = title,
        body = body,
        status = status,
        priority = TaskPriority.NORMAL,
        due = due,
        createdAt = 1785880000,
        updatedAt = 1785880000,
        attachments = emptyList(),
        assignees = emptyList(),
    )

    private val tasks = listOf(
        task("1", title = "Modellwechsel mobil", body = "unterwegs umschalten"),
        task("2", channelId = "kanal-b", title = "Nachtwache", body = "Unit ist ausgefallen"),
        task("3", title = "Anhang hochladen", body = "Blossom prüfen"),
    )

    @Test
    fun `no filter and no search returns everything`() {
        assertEquals(3, TaskFilter.apply(tasks, null, "").size)
    }

    @Test
    fun `channel filter keeps only that channel`() {
        assertEquals(listOf("2"), TaskFilter.apply(tasks, "kanal-b", "").map { it.id })
    }

    @Test
    fun `search matches the title`() {
        assertEquals(listOf("1"), TaskFilter.apply(tasks, null, "Modellwechsel").map { it.id })
    }

    @Test
    fun `search matches the body too`() {
        assertEquals(listOf("2"), TaskFilter.apply(tasks, null, "ausgefallen").map { it.id })
    }

    @Test
    fun `search ignores case and surrounding blanks`() {
        assertEquals(listOf("3"), TaskFilter.apply(tasks, null, "  BLOSSOM ").map { it.id })
    }

    @Test
    fun `a term nobody carries yields an empty list, not everything`() {
        // The failure this guards is not a wrong subset — it is the filter
        // being bypassed entirely and every task coming back.
        assertEquals(emptyList<String>(), TaskFilter.apply(tasks, null, "xyzxyz").map { it.id })
    }

    @Test
    fun `channel filter and search compose`() {
        assertEquals(emptyList<String>(), TaskFilter.apply(tasks, "kanal-b", "Blossom").map { it.id })
        assertEquals(listOf("3"), TaskFilter.apply(tasks, "kanal-a", "Blossom").map { it.id })
    }

    private val dated = listOf(
        task("heute", due = "2026-08-05"),
        task("heute-erledigt", due = "2026-08-05", status = TaskStatus.DONE),
        task("morgen", due = "2026-08-06"),
        task("gestern", due = "2026-08-04"),
        task("kaputt", due = "irgendwann"),
        task("ohne"),
    )

    @Test
    fun `due filter keeps exactly that day`() {
        assertEquals(
            listOf("heute", "heute-erledigt"),
            TaskFilter.apply(dated, due = today).map { it.id },
        )
    }

    @Test
    fun `day counts leave out closed tasks and unparseable dates`() {
        val counts = TaskFilter.openCountsByDay(dated, DueDates.week(today))
        assertEquals(1, counts[today])
        assertEquals(1, counts[LocalDate.of(2026, 8, 6)])
        // Yesterday is not in the week at all, so it must not appear.
        assertEquals(null, counts[LocalDate.of(2026, 8, 4)])
        assertEquals(2, counts.size)
    }

    @Test
    fun `overdue is open work whose day has passed`() {
        assertEquals(listOf("gestern"), TaskFilter.overdue(dated, today).map { it.id })
    }
}
