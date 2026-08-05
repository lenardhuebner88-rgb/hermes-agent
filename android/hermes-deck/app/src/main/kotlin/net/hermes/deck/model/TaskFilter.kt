package net.hermes.deck.model

import java.time.LocalDate

/**
 * What the deck screen shows, given a channel filter, a due-date filter and a
 * search term.
 *
 * Pure and free of Android so every rule here is covered by JVM tests — the
 * same reason [TaskCodec] and [DueDates] are shaped this way. Callers pass the
 * state they observed; nothing in here reads a flow, because a filter that
 * reads its own inputs is invisible to Compose (see `DeckViewModel.visibleTasks`).
 */
object TaskFilter {

    fun apply(
        tasks: List<DeckTask>,
        channel: String? = null,
        search: String = "",
        due: LocalDate? = null,
    ): List<DeckTask> {
        val needle = search.trim().lowercase()
        val dueKey = due?.let { DueDates.format(it) }
        return tasks.asSequence()
            .filter { channel == null || it.channelId == channel }
            .filter { dueKey == null || DueDates.parse(it.due)?.toString() == dueKey }
            .filter {
                needle.isEmpty() ||
                    it.title.lowercase().contains(needle) ||
                    it.body.lowercase().contains(needle)
            }
            .toList()
    }

    /**
     * Open tasks per day, for the day strip. Closed tasks are left out: a
     * finished task is not something the day still owes you.
     */
    fun openCountsByDay(tasks: List<DeckTask>, days: List<LocalDate>): Map<LocalDate, Int> {
        if (days.isEmpty()) return emptyMap()
        val wanted = days.toSet()
        val counts = HashMap<LocalDate, Int>(days.size)
        for (task in tasks) {
            if (task.status.isClosed) continue
            val date = DueDates.parse(task.due) ?: continue
            if (date in wanted) counts[date] = (counts[date] ?: 0) + 1
        }
        return counts
    }

    /** Open tasks whose date has passed — the strip cannot show these. */
    fun overdue(tasks: List<DeckTask>, today: LocalDate): List<DeckTask> =
        tasks.filter { !it.status.isClosed && DueDates.isOverdue(it.due, today) }
}
