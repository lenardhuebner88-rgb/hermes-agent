package net.hermes.dictate

/**
 * Decides how a mapped dictation segment fits into the text that is already in the field:
 * leading space (or not) and sentence-start capitalization. Pure so it is unit-testable;
 * the IME service supplies `textBeforeCursor` from the InputConnection.
 */
object CommitFormatter {

    /** The segment must NOT get a leading space when it starts with one of these. */
    private val noSpaceBefore = setOf('.', ',', '!', '?', ':', ';', ')', ']', '}', '-', '\n', ' ')

    /** No space is inserted after these field-ending characters. */
    private val noSpaceAfter = setOf('(', '[', '{', '-', '/', '„', '"', '\'')

    private val sentenceEnders = setOf('.', '!', '?')

    /**
     * @param beforeCursor text immediately before the insertion point, or null when the field
     *   refuses to expose it (treated like an empty field: capitalize, no leading space).
     */
    fun format(beforeCursor: CharSequence?, mapped: String): String {
        if (mapped.isEmpty()) return ""
        val before = beforeCursor?.toString() ?: ""
        val lastNonWs = before.trimEnd().lastOrNull()
        val trailingWhitespace = before.takeLastWhile { it.isWhitespace() }

        var text = mapped
        val startsSentence = lastNonWs == null ||
            lastNonWs in sentenceEnders ||
            trailingWhitespace.contains('\n')
        if (startsSentence) text = capitalizeFirstLetter(text)

        val last = before.lastOrNull()
        val needsSpace = last != null &&
            !last.isWhitespace() &&
            last !in noSpaceAfter &&
            text.first() !in noSpaceBefore
        return if (needsSpace) " $text" else text
    }
}
