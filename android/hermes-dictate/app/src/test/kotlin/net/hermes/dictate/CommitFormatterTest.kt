package net.hermes.dictate

import org.junit.Assert.assertEquals
import org.junit.Test

class CommitFormatterTest {

    @Test
    fun `empty or unreadable field starts a capitalized sentence without leading space`() {
        assertEquals("Hallo", CommitFormatter.format("", "hallo"))
        assertEquals("Hallo", CommitFormatter.format(null, "hallo"))
        assertEquals("Hallo", CommitFormatter.format("   ", "hallo"))
    }

    @Test
    fun `continuation gets a leading space, no capitalization`() {
        assertEquals(" welt", CommitFormatter.format("Hallo", "welt"))
    }

    @Test
    fun `no double space when the field already ends with one`() {
        assertEquals("welt", CommitFormatter.format("Hallo ", "welt"))
    }

    @Test
    fun `after a sentence ender the next segment is capitalized`() {
        assertEquals(" Neuer", CommitFormatter.format("Satz.", "neuer"))
        assertEquals("Neuer", CommitFormatter.format("Satz. ", "neuer"))
        assertEquals(" Echt", CommitFormatter.format("Wow!", "echt"))
    }

    @Test
    fun `after a newline the segment starts a sentence without leading space`() {
        assertEquals("Start", CommitFormatter.format("Zeile\n", "start"))
    }

    @Test
    fun `segments starting with punctuation attach directly`() {
        assertEquals(", und", CommitFormatter.format("Wort", ", und"))
        assertEquals(".", CommitFormatter.format("Wort", "."))
        assertEquals("-mail", CommitFormatter.format("e", "-mail"))
    }

    @Test
    fun `no space after opening brackets or hyphen`() {
        assertEquals("klammer", CommitFormatter.format("(", "klammer"))
        assertEquals("teil", CommitFormatter.format("wort-", "teil"))
    }

    @Test
    fun `empty mapped text stays empty`() {
        assertEquals("", CommitFormatter.format("Hallo", ""))
    }
}
