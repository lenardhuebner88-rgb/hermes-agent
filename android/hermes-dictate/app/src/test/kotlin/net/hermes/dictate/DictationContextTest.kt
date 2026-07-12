package net.hermes.dictate

import org.junit.Assert.assertEquals
import org.junit.Test

class DictationContextTest {
    @Test
    fun `classifies common Android apps into Flow style categories`() {
        assertEquals(AppCategory.EMAIL, DictationContext.category("com.google.android.gm"))
        assertEquals(AppCategory.PERSONAL, DictationContext.category("org.thoughtcrime.securesms"))
        assertEquals(AppCategory.WORK, DictationContext.category("com.Slack"))
        assertEquals(AppCategory.OTHER, DictationContext.category("com.example.editor"))
    }

    @Test
    fun `uses the documented Android style defaults`() {
        assertEquals("casual", DictationContext.defaultStyle(AppCategory.PERSONAL))
        assertEquals("formal", DictationContext.defaultStyle(AppCategory.EMAIL))
        assertEquals("formal", DictationContext.defaultStyle(AppCategory.WORK))
        assertEquals("formal", DictationContext.defaultStyle(AppCategory.OTHER))
    }

    @Test
    fun `context is cursor bounded and capped to the latest 500 characters`() {
        val text = "a".repeat(600) + "AFTER"
        assertEquals("a".repeat(500), DictationContext.textBeforeCursor(text, 600))
        assertEquals("", DictationContext.textBeforeCursor(text, -1))
    }
}
