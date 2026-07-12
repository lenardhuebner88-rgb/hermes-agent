package net.hermes.dictate

import android.content.Context
import java.io.IOException
import org.json.JSONObject

enum class DictateStatusEvent(val wireName: String) {
    CONTACT("contact"),
    SUCCESS("success"),
    FAILURE("failure"),
    RETRY("retry"),
}

object DictateAppVersion {
    @Suppress("DEPRECATION")
    fun current(context: Context): String =
        context.packageManager.getPackageInfo(context.packageName, 0).versionName ?: "unknown"
}

data class DictateStatusSnapshot(
    val appVersion: String,
    val engine: String,
    val language: String,
    val style: String,
    val surface: String,
    val microphonePermission: Boolean,
    val serviceEnabled: Boolean,
    val event: DictateStatusEvent = DictateStatusEvent.CONTACT,
    val latencyMs: Long? = null,
    val lastError: String? = null,
)

/**
 * Sends bounded operational metadata to the authenticated dashboard. There is intentionally no
 * API for transcript, field content, package name, or audio; reporting failures never affect
 * dictation itself.
 */
class DictateStatusReporter(
    private val url: String,
    private val cookies: SessionCookieStore,
    private val transport: HttpTransport,
) {
    fun report(snapshot: DictateStatusSnapshot): Boolean {
        if (snapshot.appVersion.isBlank() || snapshot.appVersion.length > 64) return false
        if (snapshot.engine !in ENGINES || snapshot.language !in LANGUAGES) return false
        if (snapshot.style !in STYLES || snapshot.surface !in SURFACES) return false
        if (snapshot.latencyMs != null && snapshot.latencyMs !in 0..120_000) return false
        if (snapshot.lastError != null && snapshot.lastError !in ERRORS) return false

        val body = JSONObject()
            .put("app_version", snapshot.appVersion)
            .put("engine", snapshot.engine)
            .put("language", snapshot.language)
            .put("style", snapshot.style)
            .put("surface", snapshot.surface)
            .put("microphone_permission", snapshot.microphonePermission)
            .put("service_enabled", snapshot.serviceEnabled)
            .put("event", snapshot.event.wireName)
            .apply {
                snapshot.latencyMs?.let { put("latency_ms", it) }
                snapshot.lastError?.let { put("last_error", it) }
            }
            .toString()
            .toByteArray(Charsets.UTF_8)
        val headers = buildMap {
            put("Content-Type", "application/json")
            put("Accept", "application/json")
            cookies.cookieHeader(url)?.let { put("Cookie", it) }
        }
        val response = try {
            transport.post(url, headers, body, CONNECT_TIMEOUT_MS, READ_TIMEOUT_MS)
        } catch (_: IOException) {
            return false
        }
        if (response.setCookies.isNotEmpty()) cookies.storeResponseCookies(url, response.setCookies)
        return response.status == 200
    }

    companion object {
        private const val CONNECT_TIMEOUT_MS = 5_000
        private const val READ_TIMEOUT_MS = 5_000
        private val ENGINES = setOf("on_device", "cloud")
        private val LANGUAGES = setOf("system", "german", "english", "auto")
        private val STYLES = setOf("auto", "formal", "casual", "concise", "neutral")
        private val SURFACES = setOf("overlay", "ime")
        private val ERRORS = ErrorKind.entries.map { it.name.lowercase() }.toSet()
    }
}
