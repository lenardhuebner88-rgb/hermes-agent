package net.hermes.voice

import java.util.concurrent.CountDownLatch
import java.util.concurrent.Executors
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class CaptureStateMachineTest {

    @Test
    fun `concurrent starts admit exactly one capture generation`() {
        val machine = CaptureStateMachine()
        val workers = 16
        val ready = CountDownLatch(workers)
        val start = CountDownLatch(1)
        val pool = Executors.newFixedThreadPool(workers)
        val results = (1..workers).map {
            pool.submit<Boolean> {
                ready.countDown()
                start.await()
                machine.start()
            }
        }
        ready.await()
        start.countDown()
        assertEquals(1, results.count { it.get() })
        assertEquals(CaptureState.REQUESTING, machine.state)
        pool.shutdownNow()
    }

    @Test
    fun `starts from idle`() {
        val machine = CaptureStateMachine()
        assertTrue(machine.start())
        assertEquals(CaptureState.REQUESTING, machine.state)
    }

    @Test
    fun `double start is rejected`() {
        val machine = CaptureStateMachine()
        assertTrue(machine.start())
        assertFalse(machine.start())
        assertEquals(CaptureState.REQUESTING, machine.state)
    }

    @Test
    fun `double start is rejected while capturing`() {
        val machine = CaptureStateMachine()
        machine.start()
        machine.advanceToStarting()
        machine.advanceToCapturing()
        assertFalse(machine.start())
        assertEquals(CaptureState.CAPTURING, machine.state)
    }

    @Test
    fun `stop is idempotent from idle`() {
        val machine = CaptureStateMachine()
        assertFalse(machine.stop())
        assertEquals(CaptureState.IDLE, machine.state)
    }

    @Test
    fun `stop is idempotent from requesting`() {
        val machine = CaptureStateMachine()
        machine.start()
        assertTrue(machine.stop())
        assertEquals(CaptureState.STOPPING, machine.state)
        assertFalse(machine.stop())
    }

    @Test
    fun `stop is idempotent from starting`() {
        val machine = CaptureStateMachine()
        machine.start()
        machine.advanceToStarting()
        assertTrue(machine.stop())
        assertEquals(CaptureState.STOPPING, machine.state)
        assertFalse(machine.stop())
    }

    @Test
    fun `stop is idempotent from capturing`() {
        val machine = CaptureStateMachine()
        machine.start()
        machine.advanceToStarting()
        machine.advanceToCapturing()
        assertTrue(machine.stop())
        assertEquals(CaptureState.STOPPING, machine.state)
        assertFalse(machine.stop())
    }

    @Test
    fun `stop is idempotent from stopping`() {
        val machine = CaptureStateMachine()
        machine.start()
        machine.stop()
        assertFalse(machine.stop())
        assertFalse(machine.stop())
        assertEquals(CaptureState.STOPPING, machine.state)
    }

    @Test
    fun `restart after stop is allowed`() {
        val machine = CaptureStateMachine()
        machine.start()
        machine.advanceToStarting()
        machine.advanceToCapturing()
        machine.stop()
        machine.finishStop()
        assertEquals(CaptureState.IDLE, machine.state)
        assertTrue(machine.start())
        assertEquals(CaptureState.REQUESTING, machine.state)
    }

    @Test
    fun `advance calls are rejected from the wrong state`() {
        val machine = CaptureStateMachine()
        assertFalse(machine.advanceToStarting())
        assertFalse(machine.advanceToCapturing())
        machine.start()
        assertFalse(machine.advanceToCapturing())
    }

    @Test
    fun `stale consent-OK after a stop while requesting must not start capture`() {
        // Reproduces the consent-race: web/user issues start_screen_capture, then a stop
        // (web stop_screen_capture, voice end, activity pause) lands while the system consent
        // dialog is still open, then the racing RESULT_OK arrives.
        val machine = CaptureStateMachine()
        assertTrue(machine.start())
        assertEquals(CaptureState.REQUESTING, machine.state)

        // Stop races in while the consent dialog is still showing.
        assertTrue(machine.stop())
        assertEquals(CaptureState.STOPPING, machine.state)
        machine.finishStop()
        assertEquals(CaptureState.IDLE, machine.state)

        // The stale consent-OK result now arrives: advancing to STARTING must fail, because
        // the state machine no longer recognizes this session — capture must not start.
        assertFalse(machine.advanceToStarting())
        assertEquals(CaptureState.IDLE, machine.state)
        assertFalse(machine.advanceToCapturing())
        assertEquals(CaptureState.IDLE, machine.state)
    }

    @Test
    fun `finishStop is safe to call from any state including already idle`() {
        val machine = CaptureStateMachine()
        machine.finishStop()
        assertEquals(CaptureState.IDLE, machine.state)
        machine.finishStop()
        assertEquals(CaptureState.IDLE, machine.state)
    }
}
