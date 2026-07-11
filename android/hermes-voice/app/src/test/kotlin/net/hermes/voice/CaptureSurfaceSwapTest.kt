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

    @Test
    fun `stop winning before commit rolls candidate back`() {
        var rolledBack = false
        var discarded = false
        val outcome = CaptureSurfaceSwap.execute(
            install = {},
            rollback = { rolledBack = true },
            discardCandidate = { discarded = true },
            canCommit = { false },
        )
        assertEquals(CaptureSurfaceSwapOutcome.ROLLED_BACK, outcome)
        assertTrue(rolledBack && discarded)
    }

    @Test
    fun `stop dispatches to capture owner instead of blocking caller`() {
        assertTrue(CaptureThreadOwnership.shouldDispatchStop(true, false))
        assertTrue(!CaptureThreadOwnership.shouldDispatchStop(true, true))
        assertTrue(!CaptureThreadOwnership.shouldDispatchStop(false, false))
    }

    @Test
    fun `normal and detail frame delivery are denied once stop was requested`() {
        assertTrue(CaptureDeliveryPolicy.shouldDeliver(true, false))
        assertTrue(!CaptureDeliveryPolicy.shouldDeliver(true, true))
        assertTrue(!CaptureDeliveryPolicy.shouldDeliver(false, false))
        assertTrue(CaptureDeliveryPolicy.shouldNotifyUnavailable(false))
        assertTrue(!CaptureDeliveryPolicy.shouldNotifyUnavailable(true))
    }
}
