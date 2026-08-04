package net.hermes.dictate

import java.io.File
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.RuntimeEnvironment

@RunWith(RobolectricTestRunner::class)
class DebugAudioInputTest {
    private val context get() = RuntimeEnvironment.getApplication()
    private val fixture get() = File(context.filesDir, DebugAudioInput.FILE_NAME)

    @Test
    fun `debug recorder consumes a valid wav fixture once without opening the microphone`() {
        val wav = "RIFF".toByteArray() + byteArrayOf(4, 0, 0, 0) + "WAVEfmt ".toByteArray()
        fixture.writeBytes(wav)
        val recorder = CloudRecorder(context, NoEvents)

        assertTrue(recorder.start())
        val recorded = recorder.stopAndRead()!!

        assertEquals("audio/wav", recorded.mimeType)
        assertArrayEquals(wav, recorded.bytes)
        assertFalse(fixture.exists())
    }

    @Test
    fun `invalid debug fixture fails closed and is deleted`() {
        fixture.writeText("not a wav")
        val recorder = CloudRecorder(context, NoEvents)

        assertFalse(recorder.start())
        assertFalse(fixture.exists())
    }

    private object NoEvents : CloudRecorder.Events {
        override fun onMaxDuration() = Unit
        override fun onRecorderError() = Unit
    }
}
