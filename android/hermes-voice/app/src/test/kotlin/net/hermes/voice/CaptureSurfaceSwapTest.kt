package net.hermes.voice

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class CaptureSurfaceSwapTest {
    @Test
    fun `commit keeps candidate and skips rollback`() {
        var rolledBack = false
        var discarded = false
        val outcome = CaptureSurfaceSwap.execute({}, { rolledBack = true }, { discarded = true })
        assertEquals(CaptureSurfaceSwapOutcome.COMMITTED, outcome)
        assertTrue(!rolledBack && !discarded)
    }

    @Test
    fun `failed install discards candidate and proves rollback`() {
        var discarded = false
        val outcome = CaptureSurfaceSwap.execute(
            { error("install") },
            {},
            { discarded = true },
        )
        assertEquals(CaptureSurfaceSwapOutcome.ROLLED_BACK, outcome)
        assertTrue(discarded)
    }

    @Test
    fun `failed rollback is fatal and still discards candidate`() {
        var discarded = false
        val outcome = CaptureSurfaceSwap.execute(
            { error("install") },
            { error("rollback") },
            { discarded = true },
        )
        assertEquals(CaptureSurfaceSwapOutcome.FATAL, outcome)
        assertTrue(discarded)
    }
}

