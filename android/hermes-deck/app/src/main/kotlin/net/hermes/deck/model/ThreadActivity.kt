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
        /** The thread's root event — a deck task or a plain Buzz message. */
        val rootId: String,
        /** Set only when the root is a deck task, which is what makes the row tappable. */
        val taskId: String?,
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
     * Folds channel chatter onto the threads it belongs to.
     *
     * Any thread counts, not only those rooted in a deck task. Restricting it
     * to deck tasks made the list structurally empty: measured against the
     * thirteen real channels on 2026-08-05 there were five deck-task roots in
     * the whole workspace, all of them test artefacts in one channel, while
     * fourteen ordinary threads had been spoken in within the day. The band
     * claims to answer "where is something happening"; answering it only for
     * threads this app itself created answers a different, useless question.
     *
     * Replies carry no `deck-task` marker — they are ordinary channel messages
     * pointing at the root with an `e` tag — so the root is looked up among the
     * tasks first and the chatter second. A thread whose root is in neither
     * still yields a row: dropping it would hide activity that is provably
     * there, and the row says so rather than inventing a title.
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
        val messages = events.filter { it.kind == Kind.CHANNEL_MESSAGE }
        val messagesById = messages.associateBy { it.id }
        val floor = now - withinSeconds

        // Newest reply per thread, plus how many replies that thread has at all.
        val counts = HashMap<String, Int>()
        val newest = HashMap<String, NostrEvent>()
        for (event in messages) {
            val rootId = event.threadRoot ?: event.replyTo ?: continue
            if (event.id == rootId) continue
            counts[rootId] = (counts[rootId] ?: 0) + 1
            val current = newest[rootId]
            if (current == null || event.createdAt > current.createdAt) newest[rootId] = event
        }

        return newest.entries
            .filter { it.value.createdAt >= floor }
            .map { (rootId, last) ->
                val task = tasksById[rootId]
                val root = messagesById[rootId]
                val channelId = task?.channelId ?: last.channelId ?: root?.channelId.orEmpty()
                Entry(
                    rootId = rootId,
                    taskId = task?.id,
                    title = task?.title
                        ?: root?.content?.let(Preview::line)?.takeIf { it.isNotEmpty() }
                        ?: UNTITLED,
                    channelId = channelId,
                    channelName = channelNames[channelId] ?: channelId.take(8),
                    replyCount = counts[rootId] ?: 0,
                    lastAt = last.createdAt,
                    lastAuthorPubkey = last.pubkey,
                    lastAuthorLabel = Profile.labelFor(last.pubkey, profiles),
                    lastText = Preview.line(last.content),
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

    /**
     * How many mentions of [me] are still waiting — the size of [Mention.of].
     *
     * Deliberately not a second implementation: the chip and the list behind it
     * must never be able to disagree. The rule itself lives in [Mention].
     */
    fun unansweredMentions(events: Collection<NostrEvent>, me: String): Int =
        Mention.of(events, emptyList(), emptyMap(), me).size

    /**
     * Said of a thread whose root is older than the window that pulled its
     * replies. Naming the gap beats borrowing the reply's own text as a title,
     * which would read like the thread's subject and be wrong.
     */
    const val UNTITLED: String = "Thread ohne sichtbaren Anfang"

    /**
     * Six hours, not one.
     *
     * Measured against the real workspace on 2026-08-05: across all channels
     * one thread had been spoken in within the hour, six within six hours,
     * fourteen within the day. An hour is shorter than the rhythm this system
     * actually works at, so the band was empty most of the time while work was
     * plainly going on. Each row carries its own age, so a row from four hours
     * ago says so rather than passing for "just now".
     */
    const val DEFAULT_WINDOW_SECONDS: Long = 6 * 3600
    const val DEFAULT_LIMIT: Int = 6
}
