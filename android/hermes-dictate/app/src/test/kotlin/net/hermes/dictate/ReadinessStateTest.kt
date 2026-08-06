package net.hermes.dictate

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class ReadinessStateTest {

    @Test
    fun `not all steps done reports progress and is not collapsible`() {
        val state = ReadinessState(
            listOf(
                ReadinessStep(ReadinessStepId.MIC, done = true),
                ReadinessStep(ReadinessStepId.ENABLE, done = true),
                ReadinessStep(ReadinessStepId.SELECT, done = true),
                ReadinessStep(ReadinessStepId.OVERLAY, done = false),
            ),
        )

        assertEquals(3, state.doneCount)
        assertEquals(4, state.total)
        assertFalse(state.allDone)
    }

    @Test
    fun `every step done collapses the card`() {
        val state = ReadinessState(
            listOf(
                ReadinessStep(ReadinessStepId.MIC, done = true),
                ReadinessStep(ReadinessStepId.ENABLE, done = true),
                ReadinessStep(ReadinessStepId.SELECT, done = true),
                ReadinessStep(ReadinessStepId.OVERLAY, done = true),
            ),
        )

        assertTrue(state.allDone)
    }
}
