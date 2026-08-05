package net.hermes.deck.model

import net.hermes.deck.nostr.Kind
import net.hermes.deck.nostr.NostrEvent

/**
 * Folds a bag of relay events into the task list.
 *
 * The relay hands back roots, edits, replies and deletions interleaved and in no
 * guaranteed order, so this is written to be order-independent: edits are sorted
 * before they are applied, and a deletion wins regardless of when it arrives.
 */
object TaskAssembler {

    fun assemble(events: Collection<NostrEvent>): List<DeckTask> {
        val roots = LinkedHashMap<String, DeckTask>()
        val edits = ArrayList<NostrEvent>()
        val deletedIds = HashSet<String>()
        val replies = ArrayList<NostrEvent>()

        for (event in events) {
            when {
                TaskCodec.isTaskRoot(event) ->
                    TaskCodec.fromEvent(event)?.let { roots[it.id] = it }
                event.kind == Kind.MESSAGE_EDIT -> edits.add(event)
                event.kind == Kind.DELETE -> deletedIds.addAll(event.tagValues("e"))
                event.kind == Kind.CHANNEL_MESSAGE && event.replyTo != null -> replies.add(event)
            }
        }

        // Oldest first, so applyEdit's monotonic guard accepts each in turn
        // instead of discarding everything after the newest one seen.
        edits.sortBy { it.createdAt }
        var tasks = roots
        for (edit in edits) {
            val targets = edit.tagValues("e")
            for (targetId in targets) {
                val task = tasks[targetId] ?: continue
                tasks[targetId] = TaskCodec.applyEdit(task, edit)
            }
        }

        val replyStats = HashMap<String, Pair<Int, Long>>()
        for (reply in replies) {
            val rootId = reply.threadRoot ?: reply.replyTo ?: continue
            val current = replyStats[rootId]
            replyStats[rootId] = if (current == null) {
                1 to reply.createdAt
            } else {
                (current.first + 1) to maxOf(current.second, reply.createdAt)
            }
        }

        return tasks.values
            .filterNot { it.id in deletedIds }
            .map { task ->
                val stats = replyStats[task.id]
                if (stats == null) task else task.copy(replyCount = stats.first, lastReplyAt = stats.second)
            }
            .sortedWith(defaultOrder)
    }

    /**
     * Open work first, urgent above normal, newest first within a rank — the
     * order the deck is actually read in on a phone.
     */
    val defaultOrder: Comparator<DeckTask> = compareBy<DeckTask> { it.status.isClosed }
        .thenByDescending { it.priority.ordinal }
        .thenByDescending { maxOf(it.updatedAt, it.lastReplyAt ?: 0L) }

    /** Counts per status, for the overview tiles. */
    fun countByStatus(tasks: Collection<DeckTask>): Map<TaskStatus, Int> =
        TaskStatus.entries.associateWith { status -> tasks.count { it.status == status } }
}
