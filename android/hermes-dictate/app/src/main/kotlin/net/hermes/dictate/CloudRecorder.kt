package net.hermes.dictate

import android.content.Context
import android.media.MediaRecorder
import android.os.Build
import java.io.File

/**
 * Buffers one cloud-opt-in dictation as AAC/MP4 (16 kHz mono, ~48 kbit/s — ~1.1 MiB for the
 * 3-minute cap) in the app cache dir.
 *
 * Privacy contract (PlanSpec): the file exists only between start and stopAndRead/abort and is
 * deleted in every path, including crash leftovers from earlier runs (cleaned on each start).
 */
class CloudRecorder(
    private val context: Context,
    private val events: Events,
) {
    interface Events {
        fun onMaxDuration()
        fun onRecorderError()
    }

    private var recorder: MediaRecorder? = null
    private var file: File? = null

    /**
     * Set when the recorder hit max duration and stopped ITSELF: our later stop() call then
     * throws, but the auto-stopped file is fully finalized and must still be used.
     */
    @Volatile
    private var autoStopped = false

    fun start(): Boolean {
        cleanupStaleFiles()
        autoStopped = false
        val target: File
        val r: MediaRecorder
        try {
            target = File.createTempFile(FILE_PREFIX, ".m4a", context.cacheDir)
            @Suppress("DEPRECATION")
            r = if (Build.VERSION.SDK_INT >= 31) MediaRecorder(context) else MediaRecorder()
        } catch (e: Exception) {
            return false
        }
        return try {
            r.setAudioSource(MediaRecorder.AudioSource.VOICE_RECOGNITION)
            r.setOutputFormat(MediaRecorder.OutputFormat.MPEG_4)
            r.setAudioEncoder(MediaRecorder.AudioEncoder.AAC)
            r.setAudioSamplingRate(16_000)
            r.setAudioEncodingBitRate(48_000)
            r.setAudioChannels(1)
            r.setMaxDuration(DictateConfig.MAX_RECORDING_MS)
            r.setOutputFile(target.absolutePath)
            r.setOnInfoListener { _, what, _ ->
                if (what == MediaRecorder.MEDIA_RECORDER_INFO_MAX_DURATION_REACHED) {
                    autoStopped = true
                    events.onMaxDuration()
                }
            }
            r.setOnErrorListener { _, _, _ -> events.onRecorderError() }
            r.prepare()
            r.start()
            recorder = r
            file = target
            true
        } catch (e: Exception) {
            runCatching { r.release() }
            target.delete()
            false
        }
    }

    /** Stops and returns the recorded bytes; null when nothing usable was captured. */
    fun stopAndRead(): ByteArray? {
        val r = recorder ?: return null
        recorder = null
        val f = file
        file = null
        // stop() throws both when the recording is too short to contain data (file is garbage)
        // and when the recorder already auto-stopped at max duration (file is complete).
        val stopped = runCatching { r.stop() }.isSuccess || autoStopped
        runCatching { r.release() }
        return try {
            if (stopped) f?.takeIf { it.length() > 0 }?.readBytes() else null
        } catch (e: Exception) {
            null
        } finally {
            f?.delete()
        }
    }

    fun abort() {
        recorder?.let { r ->
            runCatching { r.stop() }
            runCatching { r.release() }
        }
        recorder = null
        file?.delete()
        file = null
    }

    private fun cleanupStaleFiles() {
        cleanupStale(context)
    }

    companion object {
        private const val FILE_PREFIX = "dictate-"

        /**
         * Deletes crash leftovers. Called on every recorder start AND on IME service creation,
         * so a process killed mid-recording never leaves audio in the cache indefinitely.
         */
        fun cleanupStale(context: Context) {
            context.cacheDir.listFiles { f -> f.name.startsWith(FILE_PREFIX) }?.forEach { it.delete() }
        }
    }
}
