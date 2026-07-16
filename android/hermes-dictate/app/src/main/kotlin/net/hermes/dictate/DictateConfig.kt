package net.hermes.dictate

import android.content.Context

/** Single source of truth for the Hermes origin the cloud opt-in path is locked to. */
object DictateConfig {
    const val ALLOWED_HOST = "huebners.tail50819a.ts.net"
    const val ALLOWED_ORIGIN = "https://$ALLOWED_HOST"

    /** Existing dashboard endpoint (Slice-G contract: no new STT backend). */
    const val TRANSCRIBE_URL = "$ALLOWED_ORIGIN/api/audio/transcribe"
    const val STATUS_URL = "$ALLOWED_ORIGIN/api/dictate/status"
    const val LOGIN_URL = "$ALLOWED_ORIGIN/login"

    /** Diktat Stufe 9: shared dictionary/snippet rules, synced with the /control/diktat editor. */
    const val PERSONALIZATION_URL = "$ALLOWED_ORIGIN/api/dictate/personalization"

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

    var languageMode: LanguageMode
        get() {
            val stored = prefs.getString("language_mode", null)
            return runCatching { LanguageMode.valueOf(stored.orEmpty()) }.getOrNull()
                ?: when (languageTag) {
                    "de-DE" -> LanguageMode.GERMAN
                    "en-US" -> LanguageMode.ENGLISH
                    else -> LanguageMode.SYSTEM
                }
        }
        set(value) = prefs.edit().putString("language_mode", value.name).apply()

    val recognitionLanguageTag: String
        get() = DictationLanguage.recognitionTag(languageMode)

    val cloudLanguageHint: String?
        get() = DictationLanguage.cloudHint(languageMode)

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

    var overlayBubbleSize: Int
        get() = BubbleAppearance.nearestSize(prefs.getInt("overlay_bubble_size", 85))
        set(value) = prefs.edit().putInt("overlay_bubble_size", BubbleAppearance.nearestSize(value)).apply()

    var overlayBubbleOpacity: Int
        get() = BubbleAppearance.nearestOpacity(prefs.getInt("overlay_bubble_opacity", 80))
        set(value) = prefs.edit().putInt("overlay_bubble_opacity", BubbleAppearance.nearestOpacity(value)).apply()

    var overlayShrinkIdle: Boolean
        get() = prefs.getBoolean("overlay_shrink_idle", false)
        set(value) = prefs.edit().putBoolean("overlay_shrink_idle", value).apply()

    /**
     * "Flow-Polish": server-side dictation cleanup (punctuation, filler words, self-corrections)
     * on cloud transcriptions. Applies only to the cloud path — on-device text never leaves the
     * phone. Default ON: the polish is the point of opting into cloud quality.
     */
    var flowPolish: Boolean
        get() = prefs.getBoolean("flow_polish", true)
        set(value) = prefs.edit().putBoolean("flow_polish", value).apply()

    /** Local personal dictionary, one `spoken => written` rule per line. */
    var dictionaryRules: String
        get() = prefs.getString("dictionary_rules", "") ?: ""
        set(value) = prefs.edit().putString("dictionary_rules", value).apply()

    /** Voice-triggered snippets, one `cue => expansion` rule per line. */
    var snippetRules: String
        get() = prefs.getString("snippet_rules", "") ?: ""
        set(value) = prefs.edit().putString("snippet_rules", value).apply()

    /** Server revision as of the last successful personalization sync; 0 = never synced. */
    var syncLastRevision: Int
        get() = prefs.getInt("sync_last_revision", 0)
        set(value) = prefs.edit().putInt("sync_last_revision", value).apply()

    /** [PersonalizationFingerprint] of dictionaryRules+snippetRules as of the last successful sync. */
    var syncLastFingerprint: String
        get() = prefs.getString("sync_last_fingerprint", "") ?: ""
        set(value) = prefs.edit().putString("sync_last_fingerprint", value).apply()

    /**
     * Shared between the IME and overlay service (same process, no `android:process` split) so
     * the personalization pull throttle (max. 1 attempt / 5 min) applies across both surfaces.
     */
    var syncLastPullAttemptAtMs: Long
        get() = prefs.getLong("sync_last_pull_attempt_at_ms", 0L)
        set(value) = prefs.edit().putLong("sync_last_pull_attempt_at_ms", value).apply()

    /** Deterministic on-device removal of fillers, repetitions and simple spoken backtracks. */
    var localRefine: Boolean
        get() = prefs.getBoolean("local_refine", true)
        set(value) = prefs.edit().putBoolean("local_refine", value).apply()

    var localRecoveryEnabled: Boolean
        get() = prefs.getBoolean("local_recovery_enabled", true)
        set(value) = prefs.edit().putBoolean("local_recovery_enabled", value).apply()

    var lastRecoveryText: String
        get() = prefs.getString("last_recovery_text", "") ?: ""
        set(value) = prefs.edit().putString("last_recovery_text", value.takeLast(4_000)).apply()

    var personalStyle: String
        get() = prefs.getString("style_personal", "casual") ?: "casual"
        set(value) = prefs.edit().putString("style_personal", value).apply()

    var workStyle: String
        get() = prefs.getString("style_work", "formal") ?: "formal"
        set(value) = prefs.edit().putString("style_work", value).apply()

    var emailStyle: String
        get() = prefs.getString("style_email", "formal") ?: "formal"
        set(value) = prefs.edit().putString("style_email", value).apply()

    var otherStyle: String
        get() = prefs.getString("style_other", "formal") ?: "formal"
        set(value) = prefs.edit().putString("style_other", value).apply()

    /** One-tap override; `auto` falls back to the documented per-category defaults above. */
    var styleOverride: String
        get() = prefs.getString("style_override", "auto") ?: "auto"
        set(value) = prefs.edit().putString("style_override", value).apply()

    fun styleForPackage(packageName: String?): String {
        if (styleOverride != "auto") return styleOverride
        return when (DictationContext.category(packageName)) {
            AppCategory.PERSONAL -> personalStyle
            AppCategory.WORK -> workStyle
            AppCategory.EMAIL -> emailStyle
            AppCategory.OTHER -> otherStyle
        }
    }

    /**
     * ISO-639-1 hint for the server ("de-DE" → "de"). Falls back to "de" — the same default
     * locale the on-device path uses — so cloud whisper never wastes quality on misdetection.
     */
    val languageHint: String?
        get() = cloudLanguageHint

    // --- Diktat Stufe 9 sync: contract Nachschaerfung F1/F7 — every read-check-write across the
    // pull throttle timestamp or a sync-outcome CAS runs under one shared lock, so the IME,
    // overlay and settings screen (all one process, no android:process split) can never race
    // each other into a double pull or a silently clobbered edit. ---

    /**
     * Atomic throttle claim: `true` (and marks [syncLastPullAttemptAtMs] = [nowMs]) only if the
     * throttle allows an attempt right now — the check and the write never interleave with a
     * concurrent claim from the IME/overlay/settings.
     */
    fun tryClaimPersonalizationPullAttempt(nowMs: Long): Boolean = synchronized(syncLock) {
        if (!PersonalizationPullThrottle.shouldAttempt(syncLastPullAttemptAtMs, nowMs)) return@synchronized false
        syncLastPullAttemptAtMs = nowMs
        true
    }

    /**
     * Pull-outcome CAS (contract F1): applies content AND markers together, atomically, but only
     * if the live prefs still exactly match [expectedDictionaryRules]/[expectedSnippetRules] — the
     * snapshot the pull decision was computed against. On mismatch the whole outcome is discarded
     * (no partial marker update either) — a concurrent edit is never silently overwritten.
     * Returns whether the outcome was applied.
     */
    fun applyPersonalizationPullOutcome(
        expectedDictionaryRules: String,
        expectedSnippetRules: String,
        outcome: PersonalizationSyncOutcome,
    ): Boolean = synchronized(syncLock) {
        if (!PersonalizationCas.unchanged(expectedDictionaryRules, expectedSnippetRules, dictionaryRules, snippetRules)) {
            return@synchronized false
        }
        dictionaryRules = outcome.dictionaryRules
        snippetRules = outcome.snippetRules
        syncLastRevision = outcome.revision
        syncLastFingerprint = outcome.fingerprint
        true
    }

    /**
     * Push-outcome CAS (contract F1 exception): markers always describe the server state for the
     * content that was actually pushed — set unconditionally. Content is only reflected back
     * (e.g. after a 409 union-merge) if the live prefs still match
     * [pushedDictionaryRules]/[pushedSnippetRules]; an interleaved edit is never clobbered, the
     * next debounced push reconciles it. Returns whether the content was applied.
     */
    fun applyPersonalizationPushOutcome(
        pushedDictionaryRules: String,
        pushedSnippetRules: String,
        outcome: PersonalizationSyncOutcome,
    ): Boolean = synchronized(syncLock) {
        syncLastRevision = outcome.revision
        syncLastFingerprint = outcome.fingerprint
        if (!PersonalizationCas.unchanged(pushedDictionaryRules, pushedSnippetRules, dictionaryRules, snippetRules)) {
            return@synchronized false
        }
        dictionaryRules = outcome.dictionaryRules
        snippetRules = outcome.snippetRules
        true
    }

    companion object {
        private val syncLock = Any()
    }
}
