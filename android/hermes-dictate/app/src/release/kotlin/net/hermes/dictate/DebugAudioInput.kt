package net.hermes.dictate

import android.content.Context

/** Release builds have no file name, lookup, or read path for injected audio. */
object DebugAudioInput {
    fun hasFixture(@Suppress("UNUSED_PARAMETER") context: Context): Boolean = false
    fun take(@Suppress("UNUSED_PARAMETER") context: Context): RecordedAudio? = null
}
