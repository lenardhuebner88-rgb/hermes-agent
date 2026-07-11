package net.hermes.voice

import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Test

class BridgeGenerationGateTest {
    @Test
    fun `detach invalidates queued snapshot`() {
        val gate = BridgeGenerationGate<Any>()
        val first = Any()
        gate.attach(first)
        val snapshot = gate.snapshot()
        assertNotNull(snapshot)
        gate.detach()
        assertFalse(gate.isCurrent(snapshot!!.first, snapshot.second))
    }

    @Test
    fun `replacement cannot receive previous generation payload`() {
        val gate = BridgeGenerationGate<Any>()
        val first = Any()
        gate.attach(first)
        val snapshot = gate.snapshot()!!
        gate.attach(Any())
        assertFalse(gate.isCurrent(snapshot.first, snapshot.second))
        val current = gate.snapshot()!!
        assertTrue(gate.isCurrent(current.first, current.second))
    }

    @Test
    fun `same channel reattachment keeps queued snapshot current`() {
        val gate = BridgeGenerationGate<Any>()
        val channel = Any()
        gate.attach(channel)
        val snapshot = gate.snapshot()!!
        gate.attach(channel)
        assertTrue(gate.isCurrent(snapshot.first, snapshot.second))
    }
}
