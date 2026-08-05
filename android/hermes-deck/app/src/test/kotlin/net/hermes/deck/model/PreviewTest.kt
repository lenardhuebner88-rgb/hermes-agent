package net.hermes.deck.model

import org.junit.Assert.assertEquals
import org.junit.Test

class PreviewTest {

    @Test
    fun `bold markers around the whole line are removed, not left as asterisks`() {
        // Verbatim from the device on 2026-08-05 — the busiest row on the
        // screen read exactly this, asterisks included.
        assertEquals(
            "Aufgabe: Loop-Nachtbetrieb — Schritt 2, 4 und 5",
            Preview.line("**Aufgabe: Loop-Nachtbetrieb — Schritt 2, 4 und 5**"),
        )
    }

    @Test
    fun `bold in the middle of a sentence keeps the sentence intact`() {
        assertEquals(
            "@Piet R1 und R2 sind geprüft und gelandet. main = d1ace36637",
            Preview.line("@Piet **R1 und R2 sind geprüft und gelandet.** `main` = `d1ace36637`"),
        )
    }

    @Test
    fun `a heading, a bullet and a quote lose their marker but keep their words`() {
        assertEquals("Beobachtung", Preview.line("## Beobachtung"))
        assertEquals("erster Punkt", Preview.line("- erster Punkt"))
        assertEquals("zitiert", Preview.line("> zitiert"))
    }

    @Test
    fun `the first non-blank line wins, so a leading newline does not blank the row`() {
        assertEquals("Zweite Zeile", Preview.line("\n\nZweite Zeile\nDritte"))
    }

    @Test
    fun `a lone asterisk is text, not a marker`() {
        // Multiplication signs and shrug-shaped punctuation must survive; a
        // greedy stripper would eat the character and change the meaning.
        assertEquals("2 * 3 Läufe", Preview.line("2 * 3 Läufe"))
    }

    @Test
    fun `snake_case survives, because underscores inside a word are not emphasis`() {
        assertEquals("sys.boot_completed prüfen", Preview.line("sys.boot_completed prüfen"))
    }

    @Test
    fun `an empty message yields an empty preview rather than an invented one`() {
        assertEquals("", Preview.line(""))
        assertEquals("", Preview.line("   \n\t"))
    }

    @Test
    fun `runs of whitespace collapse so a padded line does not look broken`() {
        assertEquals("a b", Preview.line("a      b"))
    }
}
