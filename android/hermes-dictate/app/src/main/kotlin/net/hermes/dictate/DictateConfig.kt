package net.hermes.dictate

import android.content.Context

/** Single source of truth for the Hermes origin the cloud opt-in path is locked to. */
object DictateConfig {
    const val ALLOWED_HOST = "huebners.tail50819a.ts.net"
    const val ALLOWED_ORIGIN = "https://$ALLOWED_HOST"

    /** Existing dashboard endpoint (Slice-G contract: no new STT backend). */
    const val TRANSCRIBE_URL = "$ALLOWED_ORIGIN/api/audio/transcribe"
    const val LOGIN_URL = "$ALLOWED_ORIGIN/login"

    /** Cheap gated GET used to answer "is the cookie session still valid?" (200 vs 401). */
    const val AUTH_PROBE_URL = "$ALLOWED_ORIGIN/api/health-status"

    /** Hard cap on a single cloud recording; also caps the upload size (~1.1 MiB AAC). */
    const val MAX_RECORDING_MS = 180_000

    /**
     * True only for the exact allowed origin: scheme https, host case-insensitively equal to
     * [ALLOWED_HOST], AND effective port 443. A host-only check would also accept e.g.
     * `https://$ALLOWED_HOST:8443/...` — a different origin. [port] follows `Uri.getPort()`
     * semantics: -1 means "no port in the URL" (the scheme default, i.e. 443 for https).
     */
    fun originIsAllowed(scheme: String?, host: String?, port: Int): Boolean {
        if (scheme == null || !scheme.equals("https", ignoreCase = true)) return false
        if (host == null || !host.equals(ALLOWED_HOST, ignoreCase = true)) return false
        return port == -1 || port == 443
    }
}

/** User settings. Everything defaults to the privacy-preserving choice. */
class DictatePrefs(context: Context) {
    private val prefs = context.getSharedPreferences("dictate", Context.MODE_PRIVATE)

    /** Master switch for the cloud path. OFF by default — without it no audio leaves the device. */
    var cloudEnabled: Boolean
        get() = prefs.getBoolean("cloud_enabled", false)
        set(value) = prefs.edit().putBoolean("cloud_enabled", value).apply()

    /** BCP-47 tag for dictation ("de-DE", "en-US") or null = device default locale. */
    var languageTag: String?
        get() = prefs.getString("language_tag", null)?.takeIf { it.isNotBlank() }
        set(value) = prefs.edit().putString("language_tag", value ?: "").apply()

    /**
     * Overlay bubble: prefer cloud transcription over on-device for every tap. Still gated by
     * [cloudEnabled] and an active login — the controller's per-use reset to ON_DEVICE after
     * each upload is unchanged; the overlay service re-arms cloud mode before the NEXT tap.
     */
    var cloudPreferred: Boolean
        get() = prefs.getBoolean("cloud_preferred", false)
        set(value) = prefs.edit().putBoolean("cloud_preferred", value).apply()

    /** Remembered vertical bubble position (px, top-left origin) so it survives restarts. */
    var overlayBubbleY: Int
        get() = prefs.getInt("overlay_bubble_y", -1)
        set(value) = prefs.edit().putInt("overlay_bubble_y", value).apply()

    /** Which screen edge the bubble last snapped to. */
    var overlayBubbleOnRight: Boolean
        get() = prefs.getBoolean("overlay_bubble_on_right", true)
        set(value) = prefs.edit().putBoolean("overlay_bubble_on_right", value).apply()
}
