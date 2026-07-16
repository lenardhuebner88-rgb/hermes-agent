package net.hermes.dictate

import android.webkit.CookieManager
import java.io.IOException
import java.net.HttpURLConnection
import java.net.URL

/** HttpURLConnection-backed transport; no third-party HTTP dependency. */
class UrlConnectionTransport : HttpTransport {
    @Throws(IOException::class)
    override fun post(
        url: String,
        headers: Map<String, String>,
        body: ByteArray,
        connectTimeoutMs: Int,
        readTimeoutMs: Int,
    ): HttpResponse = send("POST", url, headers, body, connectTimeoutMs, readTimeoutMs)

    @Throws(IOException::class)
    override fun put(
        url: String,
        headers: Map<String, String>,
        body: ByteArray,
        connectTimeoutMs: Int,
        readTimeoutMs: Int,
    ): HttpResponse = send("PUT", url, headers, body, connectTimeoutMs, readTimeoutMs)

    @Throws(IOException::class)
    override fun get(
        url: String,
        headers: Map<String, String>,
        connectTimeoutMs: Int,
        readTimeoutMs: Int,
    ): HttpResponse = send("GET", url, headers, body = null, connectTimeoutMs, readTimeoutMs)

    @Throws(IOException::class)
    private fun send(
        method: String,
        url: String,
        headers: Map<String, String>,
        body: ByteArray?,
        connectTimeoutMs: Int,
        readTimeoutMs: Int,
    ): HttpResponse {
        val conn = URL(url).openConnection() as HttpURLConnection
        try {
            conn.requestMethod = method
            conn.doOutput = body != null
            conn.connectTimeout = connectTimeoutMs
            conn.readTimeout = readTimeoutMs
            // The gate must 401 JSON instead of 302-ing to the login page.
            conn.instanceFollowRedirects = false
            headers.forEach { (k, v) -> conn.setRequestProperty(k, v) }
            if (body != null) conn.outputStream.use { it.write(body) }

            val status = conn.responseCode
            val stream = if (status >= 400) conn.errorStream else conn.inputStream
            val text = stream?.bufferedReader()?.use { it.readText() } ?: ""
            val setCookies = conn.headerFields
                .filterKeys { it != null && it.equals("Set-Cookie", ignoreCase = true) }
                .values
                .flatten()
            return HttpResponse(status, text, setCookies)
        } finally {
            conn.disconnect()
        }
    }
}

/**
 * Session store shared with the login WebView: `CookieManager` is app-wide, so the session
 * established in [LoginActivity] is directly usable for native requests, and rotated tokens
 * from Set-Cookie responses flow back to the WebView side.
 */
class WebViewCookieStore : SessionCookieStore {
    override fun cookieHeader(url: String): String? =
        CookieManager.getInstance().getCookie(url)?.takeIf { it.isNotBlank() }

    override fun storeResponseCookies(url: String, setCookies: List<String>) {
        if (setCookies.isEmpty()) return
        val manager = CookieManager.getInstance()
        setCookies.forEach { manager.setCookie(url, it) }
        manager.flush()
    }
}
