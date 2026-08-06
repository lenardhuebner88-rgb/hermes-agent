package net.hermes.dictate

import android.content.Context
import org.junit.Assert.assertEquals
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.RuntimeEnvironment

/**
 * The Compose settings rebuild must not silently rename a `SharedPreferences` key — anyone who
 * did that would ship an update that quietly resets every existing user's settings. Each
 * assertion writes through [SettingsFormState] (the new Compose-facing path) and reads the raw
 * key straight out of the `"dictate"` prefs file (the storage [DictateConfig]'s persistence
 * contract is defined against), independent of [DictatePrefs]'s own getter — so a rename on
 * *both* sides at once would still be caught.
 */
@RunWith(RobolectricTestRunner::class)
class SettingsPersistenceKeyTest {
    private val context get() = RuntimeEnvironment.getApplication()
    private val rawPrefs get() = context.getSharedPreferences("dictate", Context.MODE_PRIVATE)

    @Test
    fun `style override written through the new form still lands under style_override`() {
        val form = SettingsFormState(DictatePrefs(context))

        form.styleOverride = "concise"

        assertEquals("concise", rawPrefs.getString("style_override", null))
    }

    @Test
    fun `language mode written through the new form still lands under language_mode`() {
        val form = SettingsFormState(DictatePrefs(context))

        form.languageMode = LanguageMode.ENGLISH

        assertEquals("ENGLISH", rawPrefs.getString("language_mode", null))
    }

    @Test
    fun `cloud opt-in written through the new form still lands under cloud_enabled`() {
        val form = SettingsFormState(DictatePrefs(context))

        form.cloudEnabled = true

        assertEquals(true, rawPrefs.getBoolean("cloud_enabled", false))
    }

    @Test
    fun `bubble size written through the new form still lands under overlay_bubble_size`() {
        val form = SettingsFormState(DictatePrefs(context))

        form.bubbleSize = 100

        assertEquals(100, rawPrefs.getInt("overlay_bubble_size", -1))
    }

    @Test
    fun `bubble opacity written through the new form still lands under overlay_bubble_opacity`() {
        val form = SettingsFormState(DictatePrefs(context))

        form.bubbleOpacity = 60

        assertEquals(60, rawPrefs.getInt("overlay_bubble_opacity", -1))
    }

    @Test
    fun `cloud-preferred toggle written through the new form still lands under cloud_preferred`() {
        val form = SettingsFormState(DictatePrefs(context))

        form.cloudPreferred = true

        assertEquals(true, rawPrefs.getBoolean("cloud_preferred", false))
    }

    @Test
    fun `local-refine toggle written through the new form still lands under local_refine`() {
        val form = SettingsFormState(DictatePrefs(context))

        form.localRefine = false

        assertEquals(false, rawPrefs.getBoolean("local_refine", true))
    }

    @Test
    fun `idle-shrink toggle written through the new form still lands under overlay_shrink_idle`() {
        val form = SettingsFormState(DictatePrefs(context))

        form.bubbleShrinkIdle = true

        assertEquals(true, rawPrefs.getBoolean("overlay_shrink_idle", false))
    }

    @Test
    fun `dictionary rules written through the settings screen's DictatePrefs path still land under dictionary_rules`() {
        // dictionaryRules/snippetRules are the one pair SettingsFormState deliberately does NOT
        // auto-persist (see its doc comment) — SettingsActivity's onDictionaryChanged/
        // onSnippetChanged write to DictatePrefs directly, on the same debounced-push path as the
        // personalization sync. That is the setter this test has to hit.
        val prefs = DictatePrefs(context)

        prefs.dictionaryRules = "hermes agent => Hermes Agent"

        assertEquals("hermes agent => Hermes Agent", rawPrefs.getString("dictionary_rules", null))
    }

    @Test
    fun `snippet rules written through the settings screen's DictatePrefs path still land under snippet_rules`() {
        val prefs = DictatePrefs(context)

        prefs.snippetRules = "signatur => Viele Gruesse"

        assertEquals("signatur => Viele Gruesse", rawPrefs.getString("snippet_rules", null))
    }

    @Test
    fun `local recovery toggled off through the new form still clears last_recovery_text`() {
        rawPrefs.edit().putString("last_recovery_text", "leftover draft").apply()
        val form = SettingsFormState(DictatePrefs(context))

        form.localRecoveryEnabled = false

        assertEquals(false, rawPrefs.getBoolean("local_recovery_enabled", true))
        assertEquals("", rawPrefs.getString("last_recovery_text", null))
    }

    @Test
    fun `a value written directly via the old DictatePrefs key is read back through the new form`() {
        rawPrefs.edit().putBoolean("flow_polish", false).apply()

        val form = SettingsFormState(DictatePrefs(context))

        assertEquals(false, form.flowPolish)
    }
}
