package net.hermes.voice

import org.junit.Assert.assertFalse
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
}
