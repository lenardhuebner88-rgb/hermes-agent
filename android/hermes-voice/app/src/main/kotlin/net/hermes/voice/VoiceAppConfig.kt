package net.hermes.voice

/** Single source of truth for the Hermes voice origin this shell is locked to. */
object VoiceAppConfig {
    const val ALLOWED_HOST = "huebners.tail50819a.ts.net"
    const val ALLOWED_ORIGIN = "https://$ALLOWED_HOST"
    const val VOICE_URL = "https://$ALLOWED_HOST/voice"
    const val BRIDGE_JS_OBJECT_NAME = "HermesNative"

    /** Origin comparison that tolerates a trailing slash on either side. */
    fun originMatches(candidate: String?): Boolean {
        if (candidate == null) return false
        return candidate.trimEnd('/') == ALLOWED_ORIGIN.trimEnd('/')
    }

    fun hostIsAllowed(host: String?): Boolean {
        return host != null && host.equals(ALLOWED_HOST, ignoreCase = true)
    }

    /**
     * True only for the exact allowed origin: scheme https, host case-insensitively equal to
     * [ALLOWED_HOST], AND effective port 443. Host cookies/navigation locks are port-agnostic,
     * so a host-only check would also accept e.g. `https://$ALLOWED_HOST:8443/...` — a
     * different origin. [port] follows `Uri.getPort()` semantics: -1 means "no port in the
     * URL" (the scheme default, i.e. 443 for https).
     */
    fun originIsAllowed(scheme: String?, host: String?, port: Int): Boolean {
        if (scheme == null || !scheme.equals("https", ignoreCase = true)) return false
        if (!hostIsAllowed(host)) return false
        return port == -1 || port == 443
    }
}
