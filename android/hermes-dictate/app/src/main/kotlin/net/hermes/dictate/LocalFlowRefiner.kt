package net.hermes.dictate

sealed class DictationTransform {
    data class Text(val value: String) : DictationTransform()
    object UndoLastSegment : DictationTransform()
    object DeleteLastSentence : DictationTransform()
}

/** Fast on-device cleanup for the corrections people naturally make while speaking. */
object LocalFlowRefiner {
    private val germanFillers = Regex("(?iu)(?<![\\p{L}\\p{N}_])(?:äh+m?|hm+|mhm+)(?![\\p{L}\\p{N}_])")
    private val englishFillers = Regex("(?iu)(?<![\\p{L}\\p{N}_])(?:uh+|um+|erm+)(?![\\p{L}\\p{N}_])")
    private val repeatedWord = Regex("(?iu)(?<![\\p{L}\\p{N}_])([\\p{L}\\p{N}_-]+)(?:\\s+\\1)+(?![\\p{L}\\p{N}_])")
    private val selfCorrection = Regex(
        "(?iu)([\\p{L}\\p{N}_-]+)\\s*,?\\s*" +
            "(?:nein\\s*,?\\s*(?:ich\\s+meine|besser)|" +
            "no\\s*,?\\s*(?:wait\\s*,?\\s*)?i\\s+mean)\\s+" +
            "([\\p{L}\\p{N}_-]+)",
    )

    fun transform(raw: String, languageTag: String?): DictationTransform {
        val command = raw.trim().lowercase()
            .replace(Regex("[,.!?;:]"), "")
            .replace(Regex("\\s+"), " ")
        return when (command) {
            "nein zurück", "nein zurueck", "no go back", "undo that" ->
                DictationTransform.UndoLastSegment
            "lösche den letzten satz", "loesche den letzten satz", "delete the last sentence" ->
                DictationTransform.DeleteLastSentence
            else -> DictationTransform.Text(refine(raw, languageTag))
        }
    }

    fun refine(raw: String, languageTag: String?): String {
        var text = raw.trim()
        if (text.isEmpty()) return text
        text = germanFillers.replace(text, "")
        if (languageTag?.startsWith("de", ignoreCase = true) != true) {
            // German "um" is a real preposition and must never be stripped as an English filler.
            text = englishFillers.replace(text, "")
        }
        text = selfCorrection.replace(text) { it.groupValues[2] }
        text = repeatedWord.replace(text) { it.groupValues[1] }
        text = text.replace(Regex("\\s+([,.;:!?])"), "$1")
        return text.replace(Regex("\\s{2,}"), " ").trim()
    }
}
