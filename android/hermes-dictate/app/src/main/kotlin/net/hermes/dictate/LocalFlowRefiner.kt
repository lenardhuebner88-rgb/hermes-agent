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
    private val leadingDiscourseFiller = Regex("(?iu)^\\s*also\\s*,?\\s+")
    // "okay also dann ..." — the same filler, one interjection later.
    private val interjectionAlso = Regex("(?iu)^(\\s*(?:okay|ok|ja|gut|so|na)\\s*,?\\s+)also\\s*,?\\s+")
    // "im Grunde genommen" is a phrase the speaker meant; only the bare hedge is stripped.
    private val discourseFillers = Regex(
        "(?iu)(?<![\\p{L}\\p{N}_])(?:quasi|sozusagen|gewissermaßen|im\\s+grunde(?!\\s+genommen))(?![\\p{L}\\p{N}_])",
    )
    private val repeatedWord = Regex("(?iu)(?<![\\p{L}\\p{N}_])([\\p{L}\\p{N}_-]+)(?:\\s+\\1)+(?![\\p{L}\\p{N}_])")
    // A bare "besser" is NOT a correction marker: "das läuft besser als gestern" is ordinary prose
    // and losing the words around it is far worse than missing one spoken correction.
    private val selfCorrection = Regex(
        "(?iu)([\\p{L}\\p{N}_-]+)\\s*,?\\s*" +
            "(?:nein\\s*,?\\s*(?:(?:halt\\s*,?\\s*)?(?:eher)|ich\\s+meine|besser)?|" +
            "ich\\s+meine|genauer\\s+gesagt|besser\\s+gesagt|" +
            "no\\s*,?\\s*(?:wait\\s*,?\\s*)?i\\s+mean)\\s+" +
            "([\\p{L}\\p{N}_-]+)",
    )
    // A leading "nein" is only a correction when the speaker paused or qualified it. Plain
    // "nein danke das reicht mir" must keep its first word.
    private val correctionAtStart = Regex(
        "(?iu)^\\s*(?:nein\\s*,\\s*(?:halt\\s*,?\\s*)?(?:eher\\s+)?|" +
            "nein\\s+(?:halt|eher)\\s*,?\\s*|ich\\s+meine\\s+)",
    )

    /** After these words "ich meine"/"nein" opens a relative clause, never a correction. */
    private val relativeOpeners = setOf(
        "was", "wie", "wer", "wen", "wem", "das", "dass", "welche", "welches", "welcher",
        "wenn", "weil", "ob", "so", "sofern",
    )

    /** "... nein gesagt" reports a refusal; it does not replace the previous word. */
    private val participle = Regex("(?iu)^ge[\\p{L}]+(?:t|en)$")

    /** Spoken punctuation commands repeat legitimately ("neuer Absatz Absatz zwei"). */
    private val commandWords = setOf(
        "absatz", "zeile", "punkt", "komma", "fragezeichen", "ausrufezeichen",
        "doppelpunkt", "semikolon", "strichpunkt", "bindestrich",
        "neue", "neuer", "paragraph", "line",
    )

    /**
     * Voice commands. A command fires only when it is the ENTIRE finished segment, so "streich das
     * Meeting am Montag" stays dictated text. A bare "zurück" is deliberately not a command — it is
     * an ordinary word people dictate on its own.
     */
    val undoCommands = setOf(
        "nein zurück", "nein zurueck", "zurücknehmen", "zuruecknehmen",
        "streich das", "streiche das", "streich das wieder", "lösch das", "loesch das",
        "lösche das", "loesche das", "vergiss das", "das war falsch",
        "no go back", "undo that", "undo", "scratch that",
    )

    val deleteSentenceCommands = setOf(
        "lösche den letzten satz", "loesche den letzten satz",
        "letzten satz löschen", "letzten satz loeschen",
        "streich den letzten satz", "streiche den letzten satz",
        "delete the last sentence", "delete last sentence",
    )

    fun transform(raw: String, languageTag: String?): DictationTransform {
        val command = raw.trim().lowercase()
            .replace(Regex("[,.!?;:]"), "")
            .replace(Regex("\\s+"), " ")
        return when (command) {
            in undoCommands -> DictationTransform.UndoLastSegment
            in deleteSentenceCommands -> DictationTransform.DeleteLastSentence
            else -> DictationTransform.Text(refine(raw, languageTag))
        }
    }

    fun refine(raw: String, languageTag: String?): String {
        var text = raw.trim()
        if (text.isEmpty()) return text
        text = correctionAtStart.replace(text, "")
        text = selfCorrection.replace(text) { match ->
            val preceding = match.groupValues[1].lowercase()
            val replacement = match.groupValues[2]
            // Keep the sentence verbatim where the marker is grammar, not a correction.
            if (preceding in relativeOpeners || participle.matches(replacement)) match.value else replacement
        }
        text = interjectionAlso.replace(text, "$1")
        text = leadingDiscourseFiller.replace(text, "")
        text = germanFillers.replace(text, "")
        if (languageTag?.startsWith("de", ignoreCase = true) != true) {
            // German "um" is a real preposition and must never be stripped as an English filler.
            text = englishFillers.replace(text, "")
        }
        text = discourseFillers.replace(text, "")
        text = repeatedWord.replace(text) { match ->
            if (match.groupValues[1].lowercase() in commandWords) match.value else match.groupValues[1]
        }
        text = text.replace(Regex("\\s+([,.;:!?])"), "$1")
        text = text.replace(Regex("(?iu)^\\s*[,;:]\\s*"), "")
        return text.replace(Regex("\\s{2,}"), " ").trim()
    }
}
