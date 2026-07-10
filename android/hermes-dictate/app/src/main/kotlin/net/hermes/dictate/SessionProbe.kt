package net.hermes.dictate

import java.net.HttpURLConnection
import java.net.URL

/**
 * Answers "is the cookie session valid?" with one cheap gated GET. Runs off the main thread.
 * Like the transcriber, it writes rotated Set-Cookie tokens back (transparent refresh).
 */
object SessionProbe {
    /** true = signed in, false = not signed in, null = server unreachable. */
    fun check(cookies: SessionCookieStore = WebViewCookieStore()): Boolean? {
        val url = DictateConfig.AUTH_PROBE_URL
        val conn = try {
            URL(url).openConnection() as HttpURLConnection
        } catch (e: Exception) {
            return null
        }
        return try {
            conn.requestMethod = "GET"
            conn.connectTimeout = 6_000
            conn.readTimeout = 6_000
            conn.instanceFollowRedirects = false
            cookies.cookieHeader(url)?.let { conn.setRequestProperty("Cookie", it) }
            val status = conn.responseCode
            val setCookies = conn.headerFields
                .filterKeys { it != null && it.equals("Set-Cookie", ignoreCase = true) }
                .values
                .flatten()
            if (setCookies.isNotEmpty()) cookies.storeResponseCookies(url, setCookies)
            when (status) {
                in 200..299 -> true
                // 401 = rejected; 3xx = the gate bouncing an unauthenticated browser to /login.
                401, 403, in 300..399 -> false
                else -> null
            }
        } catch (e: Exception) {
            null
        } finally {
            conn.disconnect()
        }
    }
}
