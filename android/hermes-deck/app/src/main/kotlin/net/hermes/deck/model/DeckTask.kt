package net.hermes.deck.model

import net.hermes.deck.nostr.Kind
import net.hermes.deck.nostr.NostrEvent
import net.hermes.deck.nostr.RelayProtocol

/**
 * A task is not a private record — it *is* a Buzz thread.
 *
 * The root is an ordinary `kind:9` channel message so it shows up in Buzz on the
 * phone and can be mentioned by an agent; the deck's own fields ride along as
 * extra tags, which the relay stores verbatim and other clients ignore. Changing
 * a task publishes a `kind:40003` edit, and the Buzz client renders the edit's
 * tags in place of the root's — so a task closed here reads as closed there too.
 */
enum class TaskStatus(val wire: String, val label: String) {
    INBOX("inbox", "Eingang"),
    OPEN("open", "Offen"),
    DOING("doing", "Läuft"),
    WAITING("waiting", "Wartet"),
    DONE("done", "Erledigt");

    val isClosed: Boolean get() = this == DONE

    companion object {
        fun fromWire(value: String?): TaskStatus =
            entries.firstOrNull { it.wire == value } ?: INBOX
    }
}

enum class TaskPriority(val wire: String, val label: String) {
    NORMAL("normal", "Normal"),
    HIGH("high", "Wichtig"),
    URGENT("urgent", "Dringend");

    companion object {
        fun fromWire(value: String?): TaskPriority =
            entries.firstOrNull { it.wire == value } ?: NORMAL
    }
}

/** A file stored in Buzz, referenced from the task's `imeta` tag. */
data class Attachment(
    val url: String,
    val mime: String,
    val sha256: String,
    val size: Long,
    val name: String,
) {
    val isImage: Boolean get() = mime.startsWith("image/")

    /** The `imeta` tag form Buzz's own clients emit and understand. */
    fun toTag(): List<String> {
        // Bound outside buildList on purpose: inside it, `size` would resolve to
        // the MutableList receiver's own size and silently emit the wrong number.
        val byteCount = size
        return buildList {
            add("imeta")
            add("url $url")
            add("m $mime")
            add("x $sha256")
            add("size $byteCount")
            if (name.isNotBlank()) add("filename $name")
        }
    }

    companion object {
        fun fromTag(tag: List<String>): Attachment? {
            if (tag.isEmpty() || tag[0] != "imeta") return null
            val fields = tag.drop(1).mapNotNull { part ->
                val space = part.indexOf(' ')
                if (space <= 0) null else part.substring(0, space) to part.substring(space + 1)
            }.toMap()
            val url = fields["url"] ?: return null
            return Attachment(
                url = url,
                mime = fields["m"] ?: "application/octet-stream",
                sha256 = fields["x"] ?: "",
                size = fields["size"]?.toLongOrNull() ?: 0L,
                name = fields["filename"] ?: url.substringAfterLast('/'),
            )
        }
    }
}

data class DeckTask(
    /** The root event id — also the Buzz thread id. */
    val id: String,
    val channelId: String,
    val authorPubkey: String,
    val title: String,
    val body: String,
    val status: TaskStatus,
    val priority: TaskPriority,
    val due: String?,
    val createdAt: Long,
    /** When the deck last changed it; equals [createdAt] until the first edit. */
    val updatedAt: Long,
    val attachments: List<Attachment>,
    /** Agents this task was handed to, as pubkeys. */
    val assignees: List<String>,
    val replyCount: Int = 0,
    val lastReplyAt: Long? = null,
) {
    val hasAttachments: Boolean get() = attachments.isNotEmpty()
}

/**
 * Converts between [DeckTask] and the Nostr events on the wire.
 *
 * Pure and free of transport so every rule here is covered by JVM tests.
 */
object TaskCodec {

    const val TAG_STATUS = "status"
    const val TAG_PRIORITY = "prio"
    const val TAG_DUE = "due"
    /** Schema marker, so a later format change can be told apart from corruption. */
    const val TAG_VERSION = "deck-v"
    const val VERSION = "1"

    /** First line is the title; the rest is the body, Markdown as Buzz renders it. */
    fun splitContent(content: String): Pair<String, String> {
        val trimmed = content.trim()
        if (trimmed.isEmpty()) return "" to ""
        val newline = trimmed.indexOf('\n')
        if (newline < 0) return trimmed to ""
        return trimmed.substring(0, newline).trim() to trimmed.substring(newline + 1).trim()
    }

    fun buildContent(title: String, body: String): String {
        val head = title.trim().ifEmpty { "Ohne Titel" }
        val rest = body.trim()
        return if (rest.isEmpty()) head else "$head\n\n$rest"
    }

    /** The tag set carried by both the root event and every later edit. */
    fun tagsFor(
        channelId: String,
        status: TaskStatus,
        priority: TaskPriority,
        due: String?,
        attachments: List<Attachment>,
        assignees: List<String>,
    ): List<List<String>> = buildList {
        add(listOf("h", channelId))
        add(listOf("t", RelayProtocol.DECK_TASK_MARKER))
        add(listOf(TAG_VERSION, VERSION))
        add(listOf(TAG_STATUS, status.wire))
        add(listOf(TAG_PRIORITY, priority.wire))
        due?.takeIf { it.isNotBlank() }?.let { add(listOf(TAG_DUE, it)) }
        assignees.forEach { add(listOf("p", it)) }
        attachments.forEach { add(it.toTag()) }
    }

    fun isTaskRoot(event: NostrEvent): Boolean =
        event.kind == Kind.CHANNEL_MESSAGE &&
            event.tagValues("t").contains(RelayProtocol.DECK_TASK_MARKER) &&
            // A reply that merely quotes the marker is not itself a task.
            event.replyTo == null

    fun fromEvent(event: NostrEvent): DeckTask? {
        if (!isTaskRoot(event)) return null
        val channelId = event.channelId ?: return null
        val (title, body) = splitContent(event.content)
        return DeckTask(
            id = event.id,
            channelId = channelId,
            authorPubkey = event.pubkey,
            title = title,
            body = body,
            status = TaskStatus.fromWire(event.tag(TAG_STATUS)),
            priority = TaskPriority.fromWire(event.tag(TAG_PRIORITY)),
            due = event.tag(TAG_DUE),
            createdAt = event.createdAt,
            updatedAt = event.createdAt,
            attachments = event.tags.mapNotNull { Attachment.fromTag(it) },
            assignees = event.tagValues("p"),
        )
    }

    /**
     * Folds a `kind:40003` edit onto the task it targets.
     *
     * Only the task's own author may edit it — the relay enforces that too, but a
     * client that trusted a foreign edit would show a state the server never
     * accepted. Older edits are ignored so out-of-order delivery cannot resurrect
     * a stale status.
     */
    fun applyEdit(task: DeckTask, edit: NostrEvent): DeckTask {
        if (edit.kind != Kind.MESSAGE_EDIT) return task
        if (edit.pubkey != task.authorPubkey) return task
        if (!edit.tagValues("e").contains(task.id)) return task
        if (edit.createdAt <= task.updatedAt) return task

        val (title, body) = splitContent(edit.content)
        return task.copy(
            title = title.ifEmpty { task.title },
            body = body,
            status = TaskStatus.fromWire(edit.tag(TAG_STATUS)),
            priority = TaskPriority.fromWire(edit.tag(TAG_PRIORITY)),
            due = edit.tag(TAG_DUE),
            updatedAt = edit.createdAt,
            attachments = edit.tags.mapNotNull { Attachment.fromTag(it) },
            assignees = edit.tagValues("p"),
        )
    }

    /** The edit event that publishes a changed task. */
    fun editEventTags(task: DeckTask): List<List<String>> =
        listOf(listOf("e", task.id)) + tagsFor(
            channelId = task.channelId,
            status = task.status,
            priority = task.priority,
            due = task.due,
            attachments = task.attachments,
            assignees = task.assignees,
        )
}
