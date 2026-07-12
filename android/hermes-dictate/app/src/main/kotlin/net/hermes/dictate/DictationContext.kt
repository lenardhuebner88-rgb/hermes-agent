package net.hermes.dictate

enum class AppCategory { PERSONAL, WORK, EMAIL, OTHER }

fun AppCategory.wireName(): String = name.lowercase()

/** Small, auditable equivalent of Flow's Android app categories. */
object DictationContext {
    private const val MAX_CONTEXT_CHARS = 500

    private val emailMarkers = listOf("android.gm", "gmail", "outlook", "email", "mail")
    private val personalMarkers = listOf(
        "whatsapp", "signal", "telegram", "messenger", "messages", "sms",
    )
    private val workMarkers = listOf(
        "slack", "teams", "notion", "office", "docs", "drive", "github", "linear", "obsidian",
    )

    fun category(packageName: String?): AppCategory {
        val value = packageName.orEmpty().lowercase()
        return when {
            emailMarkers.any(value::contains) -> AppCategory.EMAIL
            personalMarkers.any(value::contains) -> AppCategory.PERSONAL
            workMarkers.any(value::contains) -> AppCategory.WORK
            else -> AppCategory.OTHER
        }
    }

    /** Flow's documented Android defaults: Personal casual; everything else formal. */
    fun defaultStyle(category: AppCategory): String =
        if (category == AppCategory.PERSONAL) "casual" else "formal"

    fun textBeforeCursor(text: CharSequence?, selectionStart: Int): String {
        val raw = text?.toString().orEmpty()
        val end = selectionStart.coerceIn(0, raw.length)
        return raw.substring(0, end).takeLast(MAX_CONTEXT_CHARS)
    }
}
