package net.hermes.voice

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Exercises the actual `onNewIntent` routing collaborator that [MainActivity.onNewIntent]
 * delegates to — not just the pure URL-selection helper. Each test injects recording
 * lambdas in place of the real `::stopCaptureIfActive` / `::loadSurface` method
 * references, so it observes the *load* the shell would perform (and its ordering
 * against capture teardown), catching the original AC1 defect where a warm singleTop
 * relaunch never loaded the target surface.
 */
class SurfaceRelaunchTest {

    /** Records the side effects [SurfaceRelaunch.route] drives, in order. */
    private class Recorder {
        val calls = mutableListOf<String>()
        var loadedUrl: String? = null

        fun stopCapture() {
            calls += "stop"
        }

        fun loadSurface(url: String) {
            calls += "load:$url"
            loadedUrl = url
        }
    }

    @Test
    fun `warm voice activity relaunched by the jarvis alias loads the jarvis board`() {
        // The exact AC1 regression: MainActivity is singleTop, so tapping Hermes
        // Jarvis while a warm Voice activity is on top delivers the intent to
        // onNewIntent -> route(), which MUST actually load JARVIS_URL. Were the load
        // wiring dropped again (the original defect), loadedUrl would stay null and
        // this assertion would fail.
        val rec = Recorder()
        val switched = SurfaceRelaunch.route(
            isLauncherEntry = true,
            launchClass = "net.hermes.voice.JarvisActivity",
            currentUrl = VoiceAppConfig.VOICE_URL,
            stopCapture = rec::stopCapture,
            loadSurface = rec::loadSurface,
        )
        assertTrue(switched)
        assertEquals(VoiceAppConfig.JARVIS_URL, rec.loadedUrl)
    }

    @Test
    fun `a live capture is always stopped before the new surface is loaded`() {
        // No MediaProjection may survive a surface switch: stop must run first, then
        // the load — proven by the exact call order.
        val rec = Recorder()
        SurfaceRelaunch.route(
            isLauncherEntry = true,
            launchClass = "net.hermes.voice.JarvisActivity",
            currentUrl = VoiceAppConfig.VOICE_URL,
            stopCapture = rec::stopCapture,
            loadSurface = rec::loadSurface,
        )
        assertEquals(listOf("stop", "load:${VoiceAppConfig.JARVIS_URL}"), rec.calls)
    }

    @Test
    fun `jarvis surface relaunched by the voice launcher loads the voice surface`() {
        val rec = Recorder()
        val switched = SurfaceRelaunch.route(
            isLauncherEntry = true,
            launchClass = "net.hermes.voice.MainActivity",
            currentUrl = VoiceAppConfig.JARVIS_URL,
            stopCapture = rec::stopCapture,
            loadSurface = rec::loadSurface,
        )
        assertTrue(switched)
        assertEquals(VoiceAppConfig.VOICE_URL, rec.loadedUrl)
    }

    @Test
    fun `re-tapping the already-visible surface neither stops capture nor navigates`() {
        // A re-tap of the icon already showing must not reload — that would waste a
        // load and, worse, tear down a live MediaProjection behind the same page.
        for (url in listOf(VoiceAppConfig.JARVIS_URL, VoiceAppConfig.VOICE_URL)) {
            val rec = Recorder()
            val launchClass = if (url == VoiceAppConfig.JARVIS_URL) {
                "net.hermes.voice.JarvisActivity"
            } else {
                "net.hermes.voice.MainActivity"
            }
            val switched = SurfaceRelaunch.route(
                isLauncherEntry = true,
                launchClass = launchClass,
                currentUrl = url,
                stopCapture = rec::stopCapture,
                loadSurface = rec::loadSurface,
            )
            assertFalse(switched)
            assertNull(rec.loadedUrl)
            assertTrue(rec.calls.isEmpty())
        }
    }

    @Test
    fun `a dictation send intent never stops capture or navigates`() {
        // ACTION_SEND dictation is not a launcher entry: keep the existing
        // stash-draft-only behaviour, never navigate — even though its component
        // maps to the voice surface while Jarvis is showing.
        val rec = Recorder()
        val switched = SurfaceRelaunch.route(
            isLauncherEntry = false,
            launchClass = "net.hermes.voice.MainActivity",
            currentUrl = VoiceAppConfig.JARVIS_URL,
            stopCapture = rec::stopCapture,
            loadSurface = rec::loadSurface,
        )
        assertFalse(switched)
        assertNull(rec.loadedUrl)
        assertTrue(rec.calls.isEmpty())
    }
}
