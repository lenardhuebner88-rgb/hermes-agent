package net.hermes.dictate

import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import android.os.Bundle
import android.view.accessibility.AccessibilityNodeInfo

/**
 * Writes a dictated segment into whichever [AccessibilityNodeInfo] currently has input focus —
 * the overlay's equivalent of `InputConnection.commitText` in the IME. The splice/cursor math
 * lives in [TextSplicer] (pure, unit-tested); this class is only the thin Android boundary.
 *
 * Preview text is NEVER written here — [DictateOverlayService] renders `Cmd.Preview` inside the
 * pill only. Only `Cmd.CommitSegment` reaches this class.
 */
class AccessibilityNodeCommitter(private val context: Context) {

    /**
     * @return the exact formatted segment if it reached the field via ACTION_SET_TEXT or the
     *   clipboard fallback; null when neither path succeeded.
     */
    fun commit(node: AccessibilityNodeInfo, segment: String): String? {
        if (segment.isEmpty()) return ""
        val fieldText = node.text?.toString() ?: ""
        val selStart = node.textSelectionStart
        val selEnd = node.textSelectionEnd
        val result = TextSplicer.splice(fieldText, selStart, selEnd, segment)

        if (setTextDirect(node, result)) return result.formattedSegment
        return if (commitViaClipboard(node, result.formattedSegment)) result.formattedSegment else null
    }

    fun applyEdit(node: AccessibilityNodeInfo, result: DictationEdits.Result): Boolean =
        setTextDirect(
            node,
            TextSplicer.Result(result.newText, result.newCursor, formattedSegment = ""),
        )

    private fun setTextDirect(node: AccessibilityNodeInfo, result: TextSplicer.Result): Boolean {
        if (!node.actionList.any { it.id == AccessibilityNodeInfo.ACTION_SET_TEXT }) return false
        val setArgs = Bundle().apply {
            putCharSequence(
                AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE,
                result.newText,
            )
        }
        if (!node.performAction(AccessibilityNodeInfo.ACTION_SET_TEXT, setArgs)) return false
        val selectArgs = Bundle().apply {
            putInt(AccessibilityNodeInfo.ACTION_ARGUMENT_SELECTION_START_INT, result.newCursor)
            putInt(AccessibilityNodeInfo.ACTION_ARGUMENT_SELECTION_END_INT, result.newCursor)
        }
        // Best effort: some fields silently ignore ACTION_SET_SELECTION after a fresh
        // ACTION_SET_TEXT (cursor lands at 0 or the end instead) — text still landed, so the
        // overall commit still counts as successful.
        node.performAction(AccessibilityNodeInfo.ACTION_SET_SELECTION, selectArgs)
        return true
    }

    /**
     * Fallback for fields that reject ACTION_SET_TEXT (some WebView/Chrome inputs, a few
     * Samsung-app custom editors): paste the formatted segment at the cursor via the clipboard,
     * then restore whatever was on the clipboard before. Android 10+ background-clipboard-read
     * restrictions don't apply here — the accessibility service acts in response to a user
     * interaction, which counts as a foreground/interactive context for clipboard access.
     */
    private fun commitViaClipboard(node: AccessibilityNodeInfo, formatted: String): Boolean {
        if (!node.actionList.any { it.id == AccessibilityNodeInfo.ACTION_PASTE }) return false
        val clipboard = context.getSystemService(Context.CLIPBOARD_SERVICE) as? ClipboardManager
            ?: return false
        val previousClip = clipboard.primaryClip
        clipboard.setPrimaryClip(ClipData.newPlainText("hermes_dictate", formatted))
        val pasted = node.performAction(AccessibilityNodeInfo.ACTION_PASTE)
        // Never leave dictated text on the clipboard: restore what was there, or clear it.
        if (previousClip != null) {
            clipboard.setPrimaryClip(previousClip)
        } else {
            runCatching { clipboard.clearPrimaryClip() }
        }
        return pasted
    }
}
