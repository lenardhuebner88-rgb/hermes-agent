package net.hermes.deck.model

/**
 * One line of a Buzz message, fit to be read at a glance.
 *
 * Buzz messages are Markdown and its clients render them. This app shows single
 * lines out of those messages — a thread title, the last thing said — and until
 * now showed them raw: measured on the device on 2026-08-05 the busiest row read
 * `**Aufgabe: Loop-Nachtbetrieb — Schritt 2, 4 und 5**`, asterisks and all.
 *
 * So the markers are removed rather than interpreted. Rendering real Markdown
 * here would mean a second, worse renderer next to the one Buzz already has;
 * this is a preview, and a preview only has to be legible.
 */
object Preview {

    /**
     * The first line worth showing, without its formatting noise.
     *
     * Empty in, empty out — the caller decides what an empty preview means,
     * because "no text" and "text we could not read" are different claims.
     */
    fun line(raw: String): String {
        val first = raw.lineSequence().firstOrNull { it.isNotBlank() }?.trim().orEmpty()
        if (first.isEmpty()) return ""
        return first
            // Block markers only bind at the start of the line.
            .replace(HEADING, "")
            .replace(BULLET, "")
            .replace(QUOTE, "")
            // Emphasis and code marks, wherever they sit. The pairs go first so
            // `**bold**` does not leave a stray asterisk behind.
            .replace(BOLD, "$1")
            .replace(ITALIC, "$1")
            .replace(CODE, "$1")
            .replace(WHITESPACE, " ")
            .trim()
    }

    private val HEADING = Regex("^#{1,6}\\s+")
    private val BULLET = Regex("^[-*+]\\s+")
    private val QUOTE = Regex("^>\\s*")
    private val BOLD = Regex("(?:\\*\\*|__)(.+?)(?:\\*\\*|__)")
    private val ITALIC = Regex("(?<![*_\\w])[*_]([^*_]+)[*_](?![*_\\w])")
    private val CODE = Regex("`([^`]*)`")
    private val WHITESPACE = Regex("\\s+")
}
