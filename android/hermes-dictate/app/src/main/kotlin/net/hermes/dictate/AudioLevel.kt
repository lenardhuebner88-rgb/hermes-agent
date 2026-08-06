package net.hermes.dictate

import kotlin.math.log10

/**
 * Converts each dictation path's raw microphone reading onto the SAME 0..100 meter scale
 * [OverlayWaveView.level] draws — otherwise the cloud and on-device paths would visibly pulse
 * differently for the same voice.
 */
object AudioLevel {

    /**
     * On-device path: [android.speech.RecognitionListener.onRmsChanged] reports roughly -2..10 dB
     * per the platform docs. This is the normalization [DictateOverlayService] already applied
     * inline before it was factored out here — same formula, same output range.
     */
    fun fromRmsDb(rmsDb: Float): Int =
        (((rmsDb + 2f) / 12f) * 100f).toInt().coerceIn(0, 100)

    /**
     * Cloud path: [android.media.MediaRecorder.getMaxAmplitude] reports a 0..32767 linear PCM
     * peak, not a dB value — there is no exact conversion onto the recognizer's -2..10 dB scale.
     * Converted to dBFS (20*log10(amplitude/32767)) it spans roughly -45 dB (room tone) to -6 dB
     * (loud, close-mic speech) for the VOICE_RECOGNITION source this app records with; that range
     * is calibrated onto the same 0..100 output as [fromRmsDb] so both paths drive the wave with
     * a comparable dynamic response.
     */
    fun fromMaxAmplitude(amplitude: Int): Int {
        if (amplitude <= 0) return 0
        val dbFs = 20f * log10(amplitude / MAX_AMPLITUDE)
        return (((dbFs - FLOOR_DB) / (CEIL_DB - FLOOR_DB)) * 100f).toInt().coerceIn(0, 100)
    }

    private const val MAX_AMPLITUDE = 32_767f
    private const val FLOOR_DB = -45f
    private const val CEIL_DB = -6f
}
