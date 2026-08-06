package net.hermes.dictate

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Proves the two dictation paths drive the wave with the same 0..100 scale: an on-device RMS dB
 * reading (SpeechRecognizer.onRmsChanged, ~-2..10 dB) and a cloud MediaRecorder.getMaxAmplitude
 * reading (0..32767 linear PCM peak) must land on comparable levels for comparable loudness.
 */
class AudioLevelTest {

    @Test
    fun `silence normalizes to zero on both paths`() {
        assertEquals(0, AudioLevel.fromRmsDb(-2f))
        assertEquals(0, AudioLevel.fromMaxAmplitude(0))
    }

    @Test
    fun `loud input normalizes near the top of the scale on both paths`() {
        assertEquals(100, AudioLevel.fromRmsDb(10f))
        // 32767 is the maximum MediaRecorder.getMaxAmplitude() can ever report.
        assertEquals(100, AudioLevel.fromMaxAmplitude(32_767))
    }

    @Test
    fun `a real-device max-amplitude sample maps onto the same range as the on-device path`() {
        // Harvested from an on-device VOICE_RECOGNITION capture: normal speaking volume typically
        // peaks in the low thousands out of 32767, which is a real device's noisy floor above
        // silence but well short of clipping — comparable to a mid-range rmsDb reading.
        val quietSpeech = AudioLevel.fromMaxAmplitude(600)
        val normalSpeech = AudioLevel.fromMaxAmplitude(4_000)
        val loudSpeech = AudioLevel.fromMaxAmplitude(15_000)

        assertTrue("quiet speech still registers above silence", quietSpeech in 1..100)
        assertTrue("normal speech reads louder than quiet speech", normalSpeech > quietSpeech)
        assertTrue("loud speech reads louder than normal speech", loudSpeech > normalSpeech)
        // All three land inside the same 0..100 meter the on-device path uses — none of them
        // saturate to 0 or 100 the way a naive linear amplitude/32767 mapping would (which would
        // put normal speech below level 15).
        assertTrue(normalSpeech in 30..90)
    }

    @Test
    fun `amplitudes are clamped instead of distorting the meter`() {
        assertEquals(0, AudioLevel.fromMaxAmplitude(-5))
        assertEquals(100, AudioLevel.fromMaxAmplitude(40_000))
    }

    @Test
    fun `rms dB values are clamped instead of distorting the meter`() {
        assertEquals(0, AudioLevel.fromRmsDb(-20f))
        assertEquals(100, AudioLevel.fromRmsDb(50f))
    }
}
