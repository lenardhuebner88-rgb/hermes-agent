package net.hermes.dictate

import org.junit.Assert.assertEquals
import org.junit.Test

/**
 * Privacy-safe regression corpus derived from operator-reported S24 output. It stores text only,
 * never audio, and adds close variants so the gate measures generalization rather than a lookup.
 */
class RecognitionQualityGateTest {
    private val dictionary = """
        plansback => PlanSpec
        planspeak => PlanSpec
        plan speck => PlanSpec
        plansweg => PlanSpec
        plan spec => PlanSpec
        planspec => PlanSpec
        kanban bord => Kanban Board
        kamernboot => Kanban Board
        kanban board => Kanban Board
        hades track => Health Track
        heldsweg => Health Track
        health track => Health Track
        healthtrack => Health Track
        hier ist die kart zur => Hermes Diktat soll
        zuverlässig erkennend => zuverlässig erkennen
    """.trimIndent()

    private val pipeline = DictationTextPipeline(
        dictionaryRules = { dictionary },
        languageTag = { "de-DE" },
    )

    @Test
    fun `canonical terms survive known S24 recognition failures`() {
        val cases = listOf(
            "Hermes Diktat soll Plansback, Kanban Board und Hades Track zuverlässig erkennen." to
                "Hermes Diktat soll PlanSpec, Kanban Board und Health Track zuverlässig erkennen.",
            "Hier ist die Kart zur Plansweg, Kamernboot und Heldsweg zuverlässig erkennend." to
                "Hermes Diktat soll PlanSpec, Kanban Board und Health Track zuverlässig erkennen.",
        )

        for ((recognized, expected) in cases) {
            assertEquals(expected, pipeline.process(recognized).text())
        }
    }

    @Test
    fun `canonical terms survive nearby unseen recognition failures`() {
        val cases = listOf(
            "Hermes Diktat soll Plansbek, Kameranboot und Heldswech zuverlässig erkennen." to
                "Hermes Diktat soll PlanSpec, Kanban Board und Health Track zuverlässig erkennen.",
            "Hermes Diktat soll Planspeck, Kanban Board und Hades Träck zuverlässig erkennen." to
                "Hermes Diktat soll PlanSpec, Kanban Board und Health Track zuverlässig erkennen.",
        )

        for ((recognized, expected) in cases) {
            assertEquals(expected, pipeline.process(recognized).text())
        }
    }

    private fun DictationTransform.text(): String = (this as DictationTransform.Text).value
}
