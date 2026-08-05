package net.hermes.deck.model

/**
 * What the deck screen shows, given a channel filter and a search term.
 *
 * Pure and free of Android so every rule here is covered by JVM tests — the
 * same reason [TaskCodec] is shaped this way.
 */
object TaskFilter {

    fun apply(tasks: List<DeckTask>, channel: String?, search: String): List<DeckTask> {
        val needle = search.trim().lowercase()
        return tasks.asSequence()
            .filter { channel == null || it.channelId == channel }
            .filter {
                needle.isEmpty() ||
                    it.title.lowercase().contains(needle) ||
                    it.body.lowercase().contains(needle)
            }
            .toList()
    }
}
