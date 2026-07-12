package net.hermes.dictate

import org.junit.Assert.assertEquals
import org.junit.Test

class TextPersonalizerTest {
    @Test
    fun `dictionary rewrites names and acronyms without touching word substrings`() {
        val rules = "piet => Piet\nhermes agent => Hermes Agent"
        assertEquals(
            "Piet baut den Hermes Agent, pietismus bleibt klein",
            TextPersonalizer.applyDictionary("piet baut den hermes agent, pietismus bleibt klein", rules),
        )
    }

    @Test
    fun `longer overlapping phrase wins`() {
        val rules = "hermes => HERMES\nhermes agent => Hermes Agent"
        assertEquals("Der Hermes Agent", TextPersonalizer.applyDictionary("Der hermes agent", rules))
    }

    @Test
    fun `parser ignores comments malformed lines duplicates and unsafe sizes`() {
        val tooLong = "x".repeat(121)
        val parsed = TextPersonalizer.parse(
            "# lokal\npiet => Piet\nkaputt\nPIET => anderer Wert\n$tooLong => nope\n => leer",
        )
        assertEquals(listOf(PersonalizationRule("piet", "Piet")), parsed)
    }

    @Test
    fun `pipeline applies punctuation before dictionary`() {
        val pipeline = DictationTextPipeline(dictionaryRules = { "hermes => Hermes" })
        assertEquals(DictationTransform.Text("Hermes."), pipeline.process("hermes punkt"))
    }

    @Test
    fun `snippet expands an exact spoken cue case insensitively`() {
        val snippets = "terminlink => https://cal.example/piet"
        assertEquals(
            "https://cal.example/piet",
            TextPersonalizer.expandSnippet("Terminlink.", snippets),
        )
    }

    @Test
    fun `snippet does not expand accidentally inside a longer sentence`() {
        val snippets = "terminlink => https://cal.example/piet"
        assertEquals(
            "hier ist mein terminlink",
            TextPersonalizer.expandSnippet("hier ist mein terminlink", snippets),
        )
    }

    @Test
    fun `snippet supports formatted line breaks without multiline rule ambiguity`() {
        val snippets = "signatur => Viele Grüße\\nPiet"
        assertEquals("Viele Grüße\nPiet", TextPersonalizer.expandSnippet("signatur", snippets))
    }
}
