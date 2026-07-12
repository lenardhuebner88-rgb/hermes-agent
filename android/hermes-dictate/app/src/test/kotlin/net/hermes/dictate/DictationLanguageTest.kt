package net.hermes.dictate

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

class DictationLanguageTest {
    @Test
    fun `explicit and system modes resolve identically for recognizer and cloud`() {
        assertEquals("de-DE", DictationLanguage.recognitionTag(LanguageMode.GERMAN, "fr-FR"))
        assertEquals("de", DictationLanguage.cloudHint(LanguageMode.GERMAN, "fr-FR"))
        assertEquals("fr-FR", DictationLanguage.recognitionTag(LanguageMode.SYSTEM, "fr-FR"))
        assertEquals("fr", DictationLanguage.cloudHint(LanguageMode.SYSTEM, "fr-FR"))
    }

    @Test
    fun `auto leaves both engines unpinned`() {
        assertEquals("", DictationLanguage.recognitionTag(LanguageMode.AUTO, "de-DE"))
        assertNull(DictationLanguage.cloudHint(LanguageMode.AUTO, "de-DE"))
    }

    @Test
    fun `invalid system language fails to the established German default`() {
        assertEquals("de-DE", DictationLanguage.recognitionTag(LanguageMode.SYSTEM, "und"))
    }
}
