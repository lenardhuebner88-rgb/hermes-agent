package net.hermes.dictate

import android.content.Context
import java.io.File

/** One-shot WAV injection seam for emulator/debug builds. Never logs or retains fixture bytes. */
object DebugAudioInput {
    const val FILE_NAME = "debug-dictation.wav"

    fun hasFixture(context: Context): Boolean = fixture(context).isFile

    fun take(context: Context): RecordedAudio? {
        val file = fixture(context)
        if (!file.isFile) return null
        return try {
            val bytes = file.readBytes()
            require(bytes.size in 12..CloudTranscriber.MAX_AUDIO_BYTES)
            require(bytes.copyOfRange(0, 4).contentEquals("RIFF".toByteArray(Charsets.US_ASCII)))
            require(bytes.copyOfRange(8, 12).contentEquals("WAVE".toByteArray(Charsets.US_ASCII)))
            RecordedAudio(bytes, "audio/wav")
        } finally {
            file.delete()
        }
    }

    private fun fixture(context: Context): File = File(context.filesDir, FILE_NAME)
}
