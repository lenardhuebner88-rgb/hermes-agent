package net.hermes.voice

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class VoiceAppConfigTest {

    @Test
    fun `allowed origin passes with no explicit port`() {
        assertTrue(
            VoiceAppConfig.originIsAllowed("https", VoiceAppConfig.ALLOWED_HOST, -1),
        )
    }

    @Test
    fun `allowed origin passes with explicit port 443`() {
        assertTrue(
            VoiceAppConfig.originIsAllowed("https", VoiceAppConfig.ALLOWED_HOST, 443),
        )
    }

    @Test
    fun `non-443 port on the allowed host is rejected`() {
        // https://huebners.tail50819a.ts.net:8443/... is a DIFFERENT origin — host cookies and
        // the WebView navigation lock are port-agnostic, so this must not be waved through.
        assertFalse(
            VoiceAppConfig.originIsAllowed("https", VoiceAppConfig.ALLOWED_HOST, 8443),
        )
    }

    @Test
    fun `http scheme is rejected even on the allowed host and default port`() {
        assertFalse(
            VoiceAppConfig.originIsAllowed("http", VoiceAppConfig.ALLOWED_HOST, -1),
        )
    }

    @Test
    fun `case-insensitive host is still allowed`() {
        assertTrue(
            VoiceAppConfig.originIsAllowed(
                "https",
                VoiceAppConfig.ALLOWED_HOST.uppercase(),
                -1,
            ),
        )
    }

    @Test
    fun `unrelated host is rejected`() {
        assertFalse(VoiceAppConfig.originIsAllowed("https", "evil.example.com", -1))
    }

    @Test
    fun `null scheme or host is rejected`() {
        assertFalse(VoiceAppConfig.originIsAllowed(null, VoiceAppConfig.ALLOWED_HOST, -1))
        assertFalse(VoiceAppConfig.originIsAllowed("https", null, -1))
    }

    @Test
    fun `jarvis launcher alias boots the jarvis board`() {
        assertEquals(
            VoiceAppConfig.JARVIS_URL,
            VoiceAppConfig.startUrlForComponent("net.hermes.voice.JarvisActivity"),
        )
    }

    @Test
    fun `default and voice launchers keep the voice surface`() {
        assertEquals(
            VoiceAppConfig.VOICE_URL,
            VoiceAppConfig.startUrlForComponent("net.hermes.voice.MainActivity"),
        )
        // A null launch component (share/dictation intent, cold relaunch) must not
        // silently switch surfaces — the voice flow stays the safe default.
        assertEquals(VoiceAppConfig.VOICE_URL, VoiceAppConfig.startUrlForComponent(null))
    }

    @Test
    fun `warm voice activity switches to jarvis when the jarvis alias relaunches it`() {
        // The bug this guards: MainActivity is singleTop, so tapping Hermes Jarvis
        // while a warm Voice activity is on top delivers the intent to onNewIntent.
        // The surface must be re-derived to JARVIS_URL, not left on /voice.
        assertEquals(
            VoiceAppConfig.JARVIS_URL,
            VoiceAppConfig.relaunchSurfaceTarget(
                isLauncherEntry = true,
                launchClass = "net.hermes.voice.JarvisActivity",
                currentUrl = VoiceAppConfig.VOICE_URL,
            ),
        )
    }

    @Test
    fun `re-tapping the already-visible surface does not reload`() {
        // No wasteful reload — and, crucially, no navigation that would tear down a
        // live MediaProjection — when the user re-taps the icon they are already on.
        assertNull(
            VoiceAppConfig.relaunchSurfaceTarget(
                isLauncherEntry = true,
                launchClass = "net.hermes.voice.JarvisActivity",
                currentUrl = VoiceAppConfig.JARVIS_URL,
            ),
        )
        assertNull(
            VoiceAppConfig.relaunchSurfaceTarget(
                isLauncherEntry = true,
                launchClass = "net.hermes.voice.MainActivity",
                currentUrl = VoiceAppConfig.VOICE_URL,
            ),
        )
    }

    @Test
    fun `jarvis surface switches back to voice when the voice launcher relaunches it`() {
        assertEquals(
            VoiceAppConfig.VOICE_URL,
            VoiceAppConfig.relaunchSurfaceTarget(
                isLauncherEntry = true,
                launchClass = "net.hermes.voice.MainActivity",
                currentUrl = VoiceAppConfig.JARVIS_URL,
            ),
        )
    }

    @Test
    fun `a non-launcher dictation intent never switches the surface`() {
        // ACTION_SEND dictation drafts must keep their existing behavior: stash the
        // draft only, never navigate — even if their component would map to voice
        // while Jarvis is showing.
        assertNull(
            VoiceAppConfig.relaunchSurfaceTarget(
                isLauncherEntry = false,
                launchClass = "net.hermes.voice.MainActivity",
                currentUrl = VoiceAppConfig.JARVIS_URL,
            ),
        )
        assertNull(
            VoiceAppConfig.relaunchSurfaceTarget(
                isLauncherEntry = false,
                launchClass = "net.hermes.voice.JarvisActivity",
                currentUrl = VoiceAppConfig.VOICE_URL,
            ),
        )
    }

    @Test
    fun `jarvis and voice surfaces are the same origin-pinned host`() {
        // Both surfaces must pass the shell's own origin lock, else in-hull
        // navigation to them would be bounced out to an external browser.
        for (url in listOf(VoiceAppConfig.JARVIS_URL, VoiceAppConfig.VOICE_URL)) {
            assertTrue(url, url.startsWith("${VoiceAppConfig.ALLOWED_ORIGIN}/"))
        }
    }
}
