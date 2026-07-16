package net.hermes.dictate

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class BiasingVocabularyTest {

    @Test
    fun `biases dictionary targets and snippet cues but never dictionary mishearings`() {
        val phrases = BiasingVocabulary.fromRules(
            "her mess => Hermes\nplan spec => PlanSpec",
            "meine adresse => Musterweg 1, 12345 Berlin",
        )
        // parse() orders rules longest-trigger-first; for biasing only membership matters.
        assertEquals(setOf("Hermes", "PlanSpec", "meine adresse"), phrases.toSet())
        assertTrue("her mess" !in phrases)
    }

    @Test
    fun `filters urls emails multiline escapes and non-letter phrases`() {
        val phrases = BiasingVocabulary.fromRules(
            listOf(
                "link => https://example.com/x",
                "mail => piet@example.com",
                "gruss => Viele Gruesse\\nPiet",
                "nummer => 12345",
                "ok => Hermes Voice",
            ).joinToString("\n"),
            "",
        )
        assertEquals(listOf("Hermes Voice"), phrases)
    }

    @Test
    fun `caps phrase length word count and total size with case-insensitive dedupe`() {
        val many = (1..150).joinToString("\n") { "sagwort$it => Wort$it" }
        assertEquals(100, BiasingVocabulary.fromRules(many, "").size)

        val phrases = BiasingVocabulary.fromRules(
            listOf(
                "a => ${"x".repeat(51)}",
                "b => eins zwei drei vier fuenf",
                "c => Hermes",
                "d => hermes",
            ).joinToString("\n"),
            "",
        )
        assertEquals(listOf("Hermes"), phrases)
    }

    @Test
    fun `empty rules produce an empty vocabulary`() {
        assertTrue(BiasingVocabulary.fromRules("", "").isEmpty())
        assertTrue(BiasingVocabulary.fromRules("# nur kommentar", "  ").isEmpty())
    }
}
