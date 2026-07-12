package net.hermes.dictate

/** Pure cursor-safe deletion math for spoken edit commands. */
object DictationEdits {
    data class Result(val newText: String, val newCursor: Int, val deletedChars: Int)

    fun undoLastSegment(fieldText: String, cursor: Int, lastInserted: String?): Result? {
        val inserted = lastInserted?.takeIf { it.isNotEmpty() } ?: return null
        val end = cursor.coerceIn(0, fieldText.length)
        if (!fieldText.substring(0, end).endsWith(inserted)) return null
        val start = end - inserted.length
        return Result(fieldText.removeRange(start, end), start, inserted.length)
    }

    fun deleteLastSentence(fieldText: String, cursor: Int): Result? {
        val end = cursor.coerceIn(0, fieldText.length)
        if (end == 0) return null
        var scan = end - 1
        while (scan >= 0 && fieldText[scan].isWhitespace()) scan -= 1
        if (scan >= 0 && fieldText[scan] in ".!?") scan -= 1
        var boundary = -1
        while (scan >= 0) {
            if (fieldText[scan] in ".!?") {
                boundary = scan
                break
            }
            scan -= 1
        }
        val start = boundary + 1
        if (start >= end) return null
        return Result(fieldText.removeRange(start, end), start, end - start)
    }
}
