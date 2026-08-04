package net.hermes.dictate

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class OverlaySafetyTest {
    @Test
    fun `idle bubble is visible only for an eligible focused field`() {
        assertFalse(OverlaySafety.shouldShow(false, DictationController.Phase.IDLE))
        assertTrue(OverlaySafety.shouldShow(true, DictationController.Phase.IDLE))
    }

    @Test
    fun `active dictation stays visible through transient focus loss`() {
        assertTrue(OverlaySafety.shouldShow(false, DictationController.Phase.LISTENING))
        assertTrue(OverlaySafety.shouldShow(false, DictationController.Phase.UPLOADING))
    }

    @Test
    fun `commit requires the same non-null live field`() {
        val fieldA = Any()
        val fieldB = Any()
        assertEquals(fieldA, OverlaySafety.commitTarget(fieldA, fieldA))
        assertNull(OverlaySafety.commitTarget(fieldA, fieldB))
        assertNull(OverlaySafety.commitTarget(fieldA, null))
        assertNull(OverlaySafety.commitTarget(null, fieldA))
    }
}
