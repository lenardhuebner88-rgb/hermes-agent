package net.hermes.deck.model

import net.hermes.deck.nostr.Kind
import net.hermes.deck.nostr.NostrEvent

/**
 * Which task threads are being talked in right now.
 *
 * This is the one question Buzz cannot answer in one look: the chat shows each
 * conversation on its own, so "where is something happening" means opening
 * thirteen channels. Here it is one list.
 *
 * Pure, and takes its inputs as parameters. The search bug of 2026-08-05 came
 * from a derivation that read flows itself and was therefore invisible to both
 * Compose and tests; this takes events in and returns values.
 */
object ThreadActivity {

    data class Entry(
        val taskId: String,
        val title: String,
        val channelId: String,
        val channelName: String,
        val replyCount: Int,
        val lastAt: Long,
        val lastAuthorPubkey: String,
        val lastAuthorLabel: String,
        val lastText: String,
        /** True when the newest word in the thread is not the operator's own. */
        val awaitingMe: Boolean,
    )

    /**
     * Folds channel chatter onto the tasks it belongs to.
     *
     * Replies carry no `deck-task` marker — they are ordinary channel messages
     * that point at the root with an `e` tag — so they can only be attributed
     * by matching that pointer against the tasks already held. A reply whose
     * root is not a known task is dropped rather than guessed at.
     */
    fun of(
        events: Collection<NostrEvent>,
        tasks: Collection<DeckTask>,
        channels: Collection<Channel>,
        profiles: Map<String, Profile>,
        me: String?,
        now: Long,
        withinSeconds: Long = DEFAULT_WINDOW_SECONDS,
        limit: Int = DEFAULT_LIMIT,
    ): List<Entry> {
        val tasksById = tasks.associateBy { it.id }
        val channelNames = channels.associate { it.id to it.displayName }
        val floor = now - withinSeconds

        // Newest reply per task, plus how many replies that task has at all.
        val counts = HashMap<String, Int>()
        val newest = HashMap<String, NostrEvent>()
        for (event in events) {
            if (event.kind != Kind.CHANNEL_MESSAGE) continue
            val rootId = event.threadRoot ?: event.replyTo ?: continue
            if (event.id == rootId) continue
            if (rootId !in tasksById) continue
            counts[rootId] = (counts[rootId] ?: 0) + 1
            val current = newest[rootId]
            if (current == null || event.createdAt > current.createdAt) newest[rootId] = event
        }

        return newest.entries
            .filter { it.value.createdAt >= floor }
            .mapNotNull { (rootId, last) ->
                val task = tasksById[rootId] ?: return@mapNotNull null
                Entry(
                    taskId = rootId,
                    title = task.title,
                    channelId = task.channelId,
                    channelName = channelNames[task.channelId] ?: task.channelId.take(8),
                    replyCount = counts[rootId] ?: 0,
                    lastAt = last.createdAt,
                    lastAuthorPubkey = last.pubkey,
                    lastAuthorLabel = Profile.labelFor(last.pubkey, profiles),
                    lastText = last.content.trim().lineSequence().firstOrNull().orEmpty(),
                    // If the last word was mine, the thread is waiting on the
                    // other side, not on me. That inversion is the whole point
                    // of the flag, so it is computed here and not in the UI.
                    awaitingMe = me != null && last.pubkey != me,
                )
            }
            .sortedByDescending { it.lastAt }
            .take(limit)
    }

    /** How many distinct people or agents spoke in the window. */
    fun speakers(entries: Collection<Entry>): Set<String> =
        entries.map { it.lastAuthorPubkey }.toSet()

    const val DEFAULT_WINDOW_SECONDS: Long = 3600
    const val DEFAULT_LIMIT: Int = 6
}
