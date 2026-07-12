package net.hermes.dictate

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class DictateStatusReporterTest {
    private class FakeTransport : HttpTransport {
        var body = ""
        override fun post(
            url: String,
            headers: Map<String, String>,
            body: ByteArray,
            connectTimeoutMs: Int,
            readTimeoutMs: Int,
        ): HttpResponse {
            this.body = body.toString(Charsets.UTF_8)
            return HttpResponse(200, "{}", emptyList())
        }
    }

    private object NoCookies : SessionCookieStore {
        override fun cookieHeader(url: String): String? = null
        override fun storeResponseCookies(url: String, setCookies: List<String>) = Unit
    }

    @Test
    fun `reports only bounded metadata`() {
        val transport = FakeTransport()
        val reporter = DictateStatusReporter("https://example.test/status", NoCookies, transport)

        assertTrue(reporter.report(DictateStatusSnapshot(
            appVersion = "1.0",
            engine = "on_device",
            language = "german",
            style = "formal",
            surface = "overlay",
            microphonePermission = true,
            serviceEnabled = true,
            event = DictateStatusEvent.SUCCESS,
            latencyMs = 840,
        )))

        val json = JSONObject(transport.body)
        assertEquals("success", json.getString("event"))
        assertEquals(840L, json.getLong("latency_ms"))
        assertFalse(json.has("transcript"))
        assertFalse(json.has("audio"))
        assertFalse(json.has("package_name"))
    }

    @Test
    fun `rejects unknown metadata before transport`() {
        val transport = FakeTransport()
        val reporter = DictateStatusReporter("https://example.test/status", NoCookies, transport)
        assertFalse(reporter.report(DictateStatusSnapshot(
            appVersion = "1.0",
            engine = "unknown",
            language = "german",
            style = "formal",
            surface = "overlay",
            microphonePermission = true,
            serviceEnabled = true,
        )))
        assertEquals("", transport.body)
    }
}
