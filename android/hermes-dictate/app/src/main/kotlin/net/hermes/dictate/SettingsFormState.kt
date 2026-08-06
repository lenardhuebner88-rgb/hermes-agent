package net.hermes.dictate

import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue

/**
 * Compose-state mirror of [DictatePrefs]: every property reads its initial value from prefs and
 * writes straight back through on every change, exactly like the old Activity's
 * `findViewById<...>().setOnCheckedChangeListener { … prefs.x = it }` wiring — just without a
 * View in between. The persistence keys themselves live in [DictatePrefs] and are untouched by
 * this rebuild (see `DictatePrefsPersistenceKeyTest`).
 *
 * Not a [androidx.lifecycle.ViewModel]: like the Activity it replaces, it is rebuilt from prefs
 * on every `onCreate` (including after a rotation) — there is no in-memory state here that prefs
 * does not already durably hold.
 */
class SettingsFormState(private val prefs: DictatePrefs) {
    var cloudPreferred: Boolean by prefsBacked(prefs.cloudPreferred) { prefs.cloudPreferred = it }
    var flowPolish: Boolean by prefsBacked(prefs.flowPolish) { prefs.flowPolish = it }
    var localRefine: Boolean by prefsBacked(prefs.localRefine) { prefs.localRefine = it }
    var bubbleSize: Int by prefsBacked(prefs.overlayBubbleSize) { prefs.overlayBubbleSize = it }
    var bubbleOpacity: Int by prefsBacked(prefs.overlayBubbleOpacity) { prefs.overlayBubbleOpacity = it }
    var bubbleShrinkIdle: Boolean by prefsBacked(prefs.overlayShrinkIdle) { prefs.overlayShrinkIdle = it }
    var localRecoveryEnabled: Boolean by prefsBacked(prefs.localRecoveryEnabled) {
        prefs.localRecoveryEnabled = it
        if (!it) prefs.lastRecoveryText = ""
    }
    var styleOverride: String by prefsBacked(prefs.styleOverride) { prefs.styleOverride = it }
    var languageMode: LanguageMode by prefsBacked(prefs.languageMode) { prefs.languageMode = it }
    var cloudEnabled: Boolean by prefsBacked(prefs.cloudEnabled) { prefs.cloudEnabled = it }

    // Dictionary/snippet text goes through the personalization pull/push pipeline
    // (`SettingsActivity`), so plain mutableStateOf without the auto-persist wrapper — the
    // caller decides when a keystroke should trigger a debounced push vs. a pulled overwrite.
    var dictionaryRules: String by mutableStateOf(prefs.dictionaryRules)
    var snippetRules: String by mutableStateOf(prefs.snippetRules)

    private fun <T> prefsBacked(initial: T, persist: (T) -> Unit): PrefsBackedState<T> =
        PrefsBackedState(initial, persist)
}

private class PrefsBackedState<T>(initial: T, private val persist: (T) -> Unit) {
    private var state by mutableStateOf(initial)

    operator fun getValue(thisRef: Any?, property: kotlin.reflect.KProperty<*>): T = state

    operator fun setValue(thisRef: Any?, property: kotlin.reflect.KProperty<*>, value: T) {
        state = value
        persist(value)
    }
}
