package net.hermes.dictate

import java.io.IOException
import java.util.Base64
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Verified against the LIVE endpoint contract in `hermes_cli/web_server.py`
 * (`transcribe_audio_upload`, 2026-07-10): request `{data_url, mime_type}` where the data_url
 * header must contain ";base64" and an audio mime; responses below are the server's real
 * shapes (FastAPI `{"detail": ...}` errors, gate 401 `{"error": "unauthenticated", ...}`).
 */
class CloudTranscriberTest {

    private class FakeTransport(
        var response: HttpResponse? = null,
        var error: IOException? = null,
    ) : HttpTransport {
        var lastUrl: String? = null
        var lastHeaders: Map<String, String> = emptyMap()
        var lastBody: ByteArray = ByteArray(0)
        var calls = 0

        override fun post(
            url: String,
            headers: Map<String, String>,
            body: ByteArray,
            connectTimeoutMs: Int,
            readTimeoutMs: Int,
        ): HttpResponse {
            calls += 1
            lastUrl = url
            lastHeaders = headers
            lastBody = body
            error?.let { throw it }
            return response!!
        }
    }

    private class FakeCookies(var header: String? = null) : SessionCookieStore {
        val stored = mutableListOf<String>()
        override fun cookieHeader(url: String) = header
        override fun storeResponseCookies(url: String, setCookies: List<String>) {
            stored += setCookies
        }
    }

    private val url = DictateConfig.TRANSCRIBE_URL
    private val audio = byteArrayOf(1, 2, 3, 4, 5)

    private fun transcriber(transport: FakeTransport, cookies: FakeCookies = FakeCookies()) =
        CloudTranscriber(url, cookies, transport)

    @Test
    fun `success parses the real response shape`() {
        // Live shape: web_server.py returns {"ok": True, "transcript": ..., "provider": ...}.
        val transport = FakeTransport(
            HttpResponse(200, """{"ok":true,"transcript":"Hallo Welt.","provider":"openai"}""", emptyList()),
        )
        val outcome = transcriber(transport).transcribe(audio, "audio/mp4")
        assertEquals(CloudOutcome.Success("Hallo Welt.", "openai"), outcome)
    }

    @Test
    fun `request matches the server contract`() {
        val transport = FakeTransport(HttpResponse(200, """{"ok":true,"transcript":"x"}""", emptyList()))
        val cookies = FakeCookies(header = "hermes_session_at=abc; hermes_session_rt=def")
        transcriber(transport, cookies).transcribe(audio, "audio/mp4")

        assertEquals(url, transport.lastUrl)
        assertEquals("application/json", transport.lastHeaders["Content-Type"])
        assertEquals("hermes_session_at=abc; hermes_session_rt=def", transport.lastHeaders["Cookie"])

        val body = JSONObject(String(transport.lastBody, Charsets.UTF_8))
        assertEquals("audio/mp4", body.getString("mime_type"))
        val dataUrl = body.getString("data_url")
        // Server requirements: "data:" prefix, ";base64" marker, decodable payload.
        assertTrue(dataUrl.startsWith("data:audio/mp4;base64,"))
        val decoded = Base64.getDecoder().decode(dataUrl.substringAfter(","))
        assertTrue(decoded.contentEquals(audio))
    }

    @Test
    fun `polish request carries only allowlisted app category and style metadata`() {
        val transport = FakeTransport(HttpResponse(200, """{"ok":true,"transcript":"x"}""", emptyList()))
        transcriber(transport).transcribe(
            audio,
            "audio/mp4",
            language = "de",
            polish = true,
            appCategory = "email",
            style = "formal",
        )
        val body = JSONObject(String(transport.lastBody, Charsets.UTF_8))
        assertEquals("email", body.getString("app_category"))
        assertEquals("formal", body.getString("style"))
        assertTrue(!body.has("context_before"))
        assertTrue(!body.has("app_package"))
    }

    @Test
    fun `missing cookie header is simply omitted`() {
        val transport = FakeTransport(HttpResponse(200, """{"ok":true,"transcript":"x"}""", emptyList()))
        transcriber(transport, FakeCookies(header = null)).transcribe(audio, "audio/mp4")
        assertNull(transport.lastHeaders["Cookie"])
    }

    @Test
    fun `rotated session cookies are always written back`() {
        // The gate's transparent refresh sets rotated tokens on the response; failing to
        // persist them would trip refresh-token reuse detection and revoke the session.
        val setCookies = listOf(
            "hermes_session_at=new; Path=/; HttpOnly; Secure",
            "hermes_session_rt=rotated; Path=/; HttpOnly; Secure; Max-Age=2592000",
        )
        val transport = FakeTransport(HttpResponse(200, """{"ok":true,"transcript":"x"}""", setCookies))
        val cookies = FakeCookies()
        transcriber(transport, cookies).transcribe(audio, "audio/mp4")
        assertEquals(setCookies, cookies.stored)
    }

    @Test
    fun `401 maps to AuthRequired`() {
        // Live shape of the auth gate's rejection.
        val transport = FakeTransport(
            HttpResponse(401, """{"error":"unauthenticated","detail":"Unauthorized"}""", emptyList()),
        )
        assertEquals(CloudOutcome.AuthRequired, transcriber(transport).transcribe(audio, "audio/mp4"))
    }

    @Test
    fun `login redirect maps to AuthRequired too`() {
        // Redirects are disabled in the transport; a 3xx is the gate bouncing an
        // unauthenticated caller to /login — the recovery is signing in, not "server error".
        val transport = FakeTransport(HttpResponse(302, "", emptyList()))
        assertEquals(CloudOutcome.AuthRequired, transcriber(transport).transcribe(audio, "audio/mp4"))
    }

    @Test
    fun `fastapi error detail is surfaced`() {
        val transport = FakeTransport(HttpResponse(400, """{"detail":"Transcription failed"}""", emptyList()))
        assertEquals(
            CloudOutcome.Server("Transcription failed"),
            transcriber(transport).transcribe(audio, "audio/mp4"),
        )
    }

    @Test
    fun `413 maps to TooLarge`() {
        val transport = FakeTransport(
            HttpResponse(413, """{"detail":"Audio recording is too large"}""", emptyList()),
        )
        assertEquals(CloudOutcome.TooLarge, transcriber(transport).transcribe(audio, "audio/mp4"))
    }

    @Test
    fun `non-json error body falls back to the http status`() {
        val transport = FakeTransport(HttpResponse(502, "<html>Bad Gateway</html>", emptyList()))
        assertEquals(CloudOutcome.Server("HTTP 502"), transcriber(transport).transcribe(audio, "audio/mp4"))
    }

    @Test
    fun `network failures map to Network`() {
        val transport = FakeTransport(error = IOException("unreachable"))
        assertEquals(
            CloudOutcome.Network("unreachable"),
            transcriber(transport).transcribe(audio, "audio/mp4"),
        )
    }

    @Test
    fun `oversized audio never touches the network`() {
        val transport = FakeTransport()
        val big = ByteArray(CloudTranscriber.MAX_AUDIO_BYTES + 1)
        assertEquals(CloudOutcome.TooLarge, transcriber(transport).transcribe(big, "audio/mp4"))
        assertEquals(0, transport.calls)
    }

    @Test
    fun `200 without ok flag is an unexpected response`() {
        val transport = FakeTransport(HttpResponse(200, """{"transcript":"x"}""", emptyList()))
        assertEquals(
            CloudOutcome.Server("Unexpected response"),
            transcriber(transport).transcribe(audio, "audio/mp4"),
        )
    }
}
