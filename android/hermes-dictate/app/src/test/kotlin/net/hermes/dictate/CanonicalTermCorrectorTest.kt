package net.hermes.dictate

import org.junit.Assert.assertEquals
import org.junit.Test

class CanonicalTermCorrectorTest {
    private val learnedRules = """
        plansback => PlanSpec
        planspeak => PlanSpec
        plan speck => PlanSpec
        plansweg => PlanSpec
        plan spec => PlanSpec
        kanban bord => Kanban Board
        kamernboot => Kanban Board
        kanban board => Kanban Board
        hades track => Health Track
        heldsweg => Health Track
        health track => Health Track
    """.trimIndent()

    @Test
    fun `generalizes only a small distance from explicitly learned mishearings`() {
        assertEquals(
            "PlanSpec, Kanban Board und Health Track",
            CanonicalTermCorrector.apply(
                "Plansbek, Kameranboot und Heldswech",
                learnedRules,
            ),
        )
    }

    @Test
    fun `bridges harmless whitespace and diacritic variation`() {
        assertEquals(
            "PlanSpec und Health Track",
            CanonicalTermCorrector.apply("Planspeck und Hades Träck", learnedRules),
        )
    }

    @Test
    fun `does not guess from a target with only one distinct learned form`() {
        val rules = "piet => Piet\nplansback => PlanSpec"
        assertEquals(
            "Piett und Plansbek",
            CanonicalTermCorrector.apply("Piett und Plansbek", rules),
        )
    }

    @Test
    fun `does not rewrite ordinary language urls email code or numbers`() {
        val input = "Der Plan steckt. https://x/Plansbek Plansbek-site.example " +
            "Plansbek+tag@example.com foo_Plansbek Plans2026"
        assertEquals(input, CanonicalTermCorrector.apply(input, learnedRules))
    }

    @Test
    fun `skips an ambiguous match instead of choosing a canonical term`() {
        val rules = """
            alfaa => Alpha
            alfaba => Alpha
            alfac => Alphas
            alfab => Alphas
        """.trimIndent()
        assertEquals("alfad", CanonicalTermCorrector.apply("alfad", rules))
    }
}
