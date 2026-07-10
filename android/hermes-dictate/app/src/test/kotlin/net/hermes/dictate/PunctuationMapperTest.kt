package net.hermes.dictate

import org.junit.Assert.assertEquals
import org.junit.Test

class PunctuationMapperTest {

    @Test
    fun `german sentence marks attach to the preceding word`() {
        assertEquals("hallo welt.", PunctuationMapper.map("hallo welt punkt"))
        assertEquals("hallo, wie geht es?", PunctuationMapper.map("hallo komma wie geht es fragezeichen"))
        assertEquals("achtung!", PunctuationMapper.map("achtung ausrufezeichen"))
        assertEquals("also: los;", PunctuationMapper.map("also doppelpunkt los semikolon"))
    }

    @Test
    fun `word after a sentence mark is capitalized`() {
        assertEquals("erster satz. Zweiter satz", PunctuationMapper.map("erster satz punkt zweiter satz"))
        assertEquals("echt? Ja", PunctuationMapper.map("echt fragezeichen ja"))
        // Comma is not a sentence ender — no capitalization.
        assertEquals("eins, zwei", PunctuationMapper.map("eins komma zwei"))
    }

    @Test
    fun `line and paragraph breaks`() {
        assertEquals("eins\nZwei", PunctuationMapper.map("eins neue zeile zwei"))
        assertEquals("eins\n\nZwei", PunctuationMapper.map("eins neuer absatz zwei"))
        assertEquals("one\nTwo", PunctuationMapper.map("one new line two"))
    }

    @Test
    fun `english marks including two-word phrases`() {
        assertEquals("hello, world. Next", PunctuationMapper.map("hello comma world period next"))
        assertEquals("really?", PunctuationMapper.map("really question mark"))
        assertEquals("stop!", PunctuationMapper.map("stop exclamation mark"))
        assertEquals("done.", PunctuationMapper.map("done full stop"))
    }

    @Test
    fun `hyphen joins the surrounding words`() {
        assertEquals("e-mail", PunctuationMapper.map("e bindestrich mail"))
        assertEquals("check-in", PunctuationMapper.map("check hyphen in"))
    }

    @Test
    fun `matching is case-insensitive`() {
        assertEquals("hallo.", PunctuationMapper.map("hallo Punkt"))
        assertEquals("hallo,", PunctuationMapper.map("hallo KOMMA"))
    }

    @Test
    fun `punctuation words only match as whole words`() {
        assertEquals("punkte sammeln", PunctuationMapper.map("punkte sammeln"))
        assertEquals("das kommando", PunctuationMapper.map("das kommando"))
    }

    @Test
    fun `mark at utterance start stands alone for the formatter to attach`() {
        assertEquals(".", PunctuationMapper.map("punkt"))
        assertEquals("?!", PunctuationMapper.map("fragezeichen ausrufezeichen"))
    }

    @Test
    fun `blank input maps to empty`() {
        assertEquals("", PunctuationMapper.map(""))
        assertEquals("", PunctuationMapper.map("   "))
    }

    @Test
    fun `existing capitalization is never lowered`() {
        assertEquals("Berlin ist toll.", PunctuationMapper.map("Berlin ist toll punkt"))
    }
}
