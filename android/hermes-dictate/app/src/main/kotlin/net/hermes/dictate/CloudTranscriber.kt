package net.hermes.dictate

import java.io.IOException
import java.util.Base64
import org.json.JSONObject

/** Minimal HTTP seam so the cloud client is unit-testable without a network. */
interface HttpTransport {
    /** @throws IOException on connect/read failures. */
    @Throws(IOException::class)
    fun post(
        url: String,
        headers: Map<String, String>,
        body: ByteArray,
        connectTimeoutMs: Int,
        readTimeoutMs: Int,
    ): HttpResponse
}

data class HttpResponse(val status: Int, val body: String, val setCookies: List<String>)

/** Cookie seam; the Android implementation is backed by the WebView CookieManager. */
interface SessionCookieStore {
    fun cookieHeader(url: String): String?
    fun storeResponseCookies(url: String, setCookies: List<String>)
}

/**
 * Client for the existing dashboard endpoint `POST /api/audio/transcribe` (Slice-G contract:
 * no new STT backend). Auth is the dashboard's cookie session, established once via the login
 * WebView in settings and shared through the app-wide CookieManager.
 *
 * The dashboard middleware transparently refreshes an expired access token and returns the
 * ROTATED tokens via Set-Cookie; the portal runs refresh-token reuse detection, so writing
 * every Set-Cookie back into the store is mandatory — a stale RT would revoke the session.
 */
class CloudTranscriber(
    private val url: String,
    private val cookies: SessionCookieStore,
    private val transport: HttpTransport,
) {
    fun transcribe(audio: ByteArray, mimeType: String): CloudOutcome {
        if (audio.isEmpty()) return CloudOutcome.Server("Empty recording")
        if (audio.size > MAX_AUDIO_BYTES) return CloudOutcome.TooLarge

        val payload = JSONObject()
            .put("mime_type", mimeType)
            .put("data_url", "data:$mimeType;base64," + Base64.getEncoder().encodeToString(audio))
            .toString()
            .toByteArray(Charsets.UTF_8)

        val headers = buildMap {
            put("Content-Type", "application/json")
            put("Accept", "application/json")
            cookies.cookieHeader(url)?.let { put("Cookie", it) }
        }

        val response = try {
            transport.post(url, headers, payload, CONNECT_TIMEOUT_MS, READ_TIMEOUT_MS)
        } catch (e: IOException) {
            return CloudOutcome.Network(e.message ?: e.javaClass.simpleName)
        }

        // Always persist rotated session cookies, whatever the status (see class doc).
        if (response.setCookies.isNotEmpty()) {
            cookies.storeResponseCookies(url, response.setCookies)
        }

        return when (response.status) {
            200 -> parseSuccess(response.body)
            // 3xx = the gate bouncing an unauthenticated caller towards /login (redirects are
            // disabled in the transport) — same recovery as 401: sign in again.
            401, in 300..399 -> CloudOutcome.AuthRequired
            413 -> CloudOutcome.TooLarge
            else -> CloudOutcome.Server(errorDetail(response))
        }
    }

    private fun parseSuccess(body: String): CloudOutcome {
        return try {
            val json = JSONObject(body)
            if (!json.optBoolean("ok", false)) {
                CloudOutcome.Server("Unexpected response")
            } else {
                CloudOutcome.Success(
                    transcript = json.optString("transcript", ""),
                    provider = json.optString("provider", "").takeIf { it.isNotBlank() },
                )
            }
        } catch (e: Exception) {
            CloudOutcome.Server("Unreadable response")
        }
    }

    private fun errorDetail(response: HttpResponse): String {
        val detail = try {
            JSONObject(response.body).optString("detail", "")
        } catch (e: Exception) {
            ""
        }
        return detail.ifBlank { "HTTP ${response.status}" }
    }

    companion object {
        /** Client-side guard, far under the server's 25 MiB decoded cap (3 min AAC ~ 1.1 MiB). */
        const val MAX_AUDIO_BYTES = 8 * 1024 * 1024

        const val CONNECT_TIMEOUT_MS = 10_000

        /** Whisper-class transcription of a multi-minute clip can take a while server-side. */
        const val READ_TIMEOUT_MS = 90_000
    }
}
