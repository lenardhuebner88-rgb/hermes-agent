package net.hermes.voice

/** Single source of truth for the Hermes voice origin this shell is locked to. */
object VoiceAppConfig {
    const val ALLOWED_HOST = "huebners.tail50819a.ts.net"
    const val ALLOWED_ORIGIN = "https://$ALLOWED_HOST"
    const val VOICE_URL = "https://$ALLOWED_HOST/voice"
    /** The Jarvis board (/control SPA). Same origin as VOICE_URL, so the shell's
     *  origin lock and the single capture bridge cover it unchanged. */
    const val JARVIS_URL = "https://$ALLOWED_HOST/control/projekte"
    const val BRIDGE_JS_OBJECT_NAME = "HermesNative"

    /**
     * Which in-origin surface the shell loads, chosen by the launcher component
     * that started the Activity. The Jarvis launcher-alias (…JarvisActivity)
     * boots straight into the Jarvis board; every other entry (including the
     * default voice launcher and share/dictation intents) keeps the existing
     * voice surface — so the shipped voice flow never regresses. Both surfaces
     * are the same origin-pinned hull and share one MediaProjection bridge, so
     * the Jarvis screenshare button never needs an app or route switch.
     */
    fun startUrlForComponent(className: String?): String =
        if (className != null && className.endsWith("JarvisActivity")) JARVIS_URL else VOICE_URL

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
