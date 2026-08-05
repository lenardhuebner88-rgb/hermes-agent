package net.hermes.deck.model

/**
 * The shape of what an agent is doing, as a short chain of glyphs.
 *
 * A single "last command" line is a snapshot and answers almost nothing: an
 * agent that has read five files in a row is *searching*, one that has run the
 * same test four times is *stuck on a gate*, and both look identical if you only
 * see the newest call. The chain makes the difference legible without reading —
 * `R R G R R E T` versus `T T E T E T T`.
 *
 * Deliberately short. Seven fit across a phone at a glyph size the eye can still
 * take in as one figure; more turns it into a barcode nobody parses.
 */
object TurnChain {

    const val LENGTH = 7

    data class Link(
        val glyph: String,
        val kind: ToolCall.Kind,
        val label: String,
        /** The newest call, and nothing has reported it finished. */
        val open: Boolean = false,
    )

    data class Chain(
        val links: List<Link>,
        /** How many further calls the window holds beyond the shown ones. */
        val more: Int,
        /** "4× gelesen, 1× geändert" — the chain in words, for the line beneath. */
        val summary: String,
    ) {
        val isEmpty: Boolean get() = links.isEmpty()
    }

    /**
     * One letter per tool family. German mnemonics on purpose: the app speaks
     * German, and `T` for Terminal next to `L` for Lesen reads as a sentence,
     * while `E`/`R` mixed from two languages reads as noise.
     */
    fun glyphFor(kind: ToolCall.Kind): String = when (kind) {
        ToolCall.Kind.EXECUTE -> "T"
        ToolCall.Kind.EDIT -> "Ä"
        ToolCall.Kind.READ -> "L"
        ToolCall.Kind.SEARCH -> "S"
        ToolCall.Kind.THINK -> "D"
        ToolCall.Kind.FETCH -> "H"
        ToolCall.Kind.OTHER -> "·"
        ToolCall.Kind.UNKNOWN -> "?"
    }

    fun of(pulse: AgentPulse?, length: Int = LENGTH): Chain {
        if (pulse == null || pulse.recent.isEmpty()) {
            return Chain(links = emptyList(), more = 0, summary = "")
        }
        // `recent` arrives newest-first; the chain reads left to right in time,
        // so it is reversed here — and the *newest* is the one that may be open.
        val shown = pulse.recent.take(length).reversed()
        val newest = pulse.recent.first()
        val links = shown.map { call ->
            Link(
                glyph = glyphFor(call.kind),
                kind = call.kind,
                label = call.label,
                open = call === newest && pulse.looksOpen,
            )
        }
        return Chain(
            links = links,
            more = (pulse.callsInWindow - links.size).coerceAtLeast(0),
            summary = summarise(shown),
        )
    }

    private fun summarise(calls: List<ToolCall>): String {
        val counts = calls.groupingBy { it.kind }.eachCount()
            .entries.sortedByDescending { it.value }
        return counts.take(2).joinToString(", ") { (kind, n) -> "$n× ${verb(kind)}" }
    }

    private fun verb(kind: ToolCall.Kind): String = when (kind) {
        ToolCall.Kind.EXECUTE -> "ausgeführt"
        ToolCall.Kind.EDIT -> "geändert"
        ToolCall.Kind.READ -> "gelesen"
        ToolCall.Kind.SEARCH -> "gesucht"
        ToolCall.Kind.THINK -> "gedacht"
        ToolCall.Kind.FETCH -> "geholt"
        ToolCall.Kind.OTHER -> "sonstiges"
        ToolCall.Kind.UNKNOWN -> "unbekannt"
    }
}
