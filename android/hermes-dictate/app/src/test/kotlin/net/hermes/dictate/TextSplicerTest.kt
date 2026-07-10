package net.hermes.dictate

import org.junit.Assert.assertEquals
import org.junit.Test

class TextSplicerTest {

    @Test
    fun `insertion at cursor in the middle of text`() {
        val r = TextSplicer.splice("Hallo Welt", 5, 5, "schöne")
        assertEquals("Hallo schöne Welt", r.newText)
        assertEquals(12, r.newCursor)
    }

    @Test
    fun `insertion at the end`() {
        val r = TextSplicer.splice("Hallo", 5, 5, "welt")
        assertEquals("Hallo welt", r.newText)
        assertEquals(10, r.newCursor)
    }

    @Test
    fun `selection is replaced by the segment`() {
        val r = TextSplicer.splice("Hallo Welt", 6, 10, "Erde")
        assertEquals("Hallo Erde", r.newText)
        assertEquals(10, r.newCursor)
    }

    @Test
    fun `reversed selection indices are normalized`() {
        val r = TextSplicer.splice("Hallo Welt", 10, 6, "Erde")
        assertEquals("Hallo Erde", r.newText)
        assertEquals(10, r.newCursor)
    }

    @Test
    fun `empty field starts a capitalized sentence`() {
        val r = TextSplicer.splice("", 0, 0, "hallo")
        assertEquals("Hallo", r.newText)
        assertEquals(5, r.newCursor)
    }

    @Test
    fun `null-ish selection (-1) falls back to inserting at the end`() {
        val r = TextSplicer.splice("Hallo", -1, -1, "welt")
        assertEquals("Hallo welt", r.newText)
        assertEquals(10, r.newCursor)
    }

    @Test
    fun `out of range selection falls back to inserting at the end`() {
        val r = TextSplicer.splice("Hallo", 99, 99, "welt")
        assertEquals("Hallo welt", r.newText)
        assertEquals(10, r.newCursor)
    }

    @Test
    fun `formatter spacing and capitalization apply at the splice point`() {
        val r = TextSplicer.splice("Satz. Rest danach", 5, 5, "neuer")
        assertEquals("Satz. Neuer Rest danach", r.newText)
        assertEquals(11, r.newCursor)
    }

    @Test
    fun `empty mapped segment leaves text unchanged`() {
        val r = TextSplicer.splice("Hallo Welt", 5, 5, "")
        assertEquals("Hallo Welt", r.newText)
        assertEquals(5, r.newCursor)
    }
}
