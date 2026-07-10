package net.hermes.dictate

/**
 * Pure cursor/selection math for inserting a dictated, already-[CommitFormatter]-formatted
 * segment into arbitrary field text — the Accessibility-overlay equivalent of what
 * `InputConnection.commitText` does for free in the IME. Kept free of Android types so it is
 * unit-testable on the host JVM; [AccessibilityNodeCommitter] supplies the Android boundary.
 */
object TextSplicer {

    /** Result of splicing [segment] into [fieldText] at the current selection. */
    data class Result(val newText: String, val newCursor: Int)

    /**
     * @param fieldText the full text currently in the field (never null from the caller; an
     *   absent node is handled before this is called).
     * @param selStart selection start, or -1 if unknown/unavailable.
     * @param selEnd selection end, or -1 if unknown/unavailable.
     * @param segment the mapped dictation segment BEFORE [CommitFormatter] formatting — this
     *   function formats it against the text before the splice point itself, mirroring the IME.
     */
    fun splice(fieldText: String, selStart: Int, selEnd: Int, segment: String): Result {
        val length = fieldText.length
        // Unknown/invalid selection (-1, or out of range from a stale snapshot) is treated as
        // "insert at the end" — the safest fallback that never throws or drops text.
        val validStart = selStart in 0..length && selEnd in 0..length
        val start = if (validStart) minOf(selStart, selEnd) else length
        val end = if (validStart) maxOf(selStart, selEnd) else length

        val before = fieldText.substring(0, start)
        val after = fieldText.substring(end)
        val formatted = CommitFormatter.format(before, segment)

        val newText = before + formatted + after
        val newCursor = before.length + formatted.length
        return Result(newText, newCursor)
    }
}
