package net.hermes.dictate

import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.provider.Settings
import android.view.inputmethod.InputMethodManager
import android.widget.Toast
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.activity.result.contract.ActivityResultContracts
import androidx.activity.viewModels
import androidx.compose.runtime.mutableStateOf
import androidx.core.content.ContextCompat
import java.util.concurrent.Executors
import net.hermes.dictate.ui.DictateTheme

class SettingsActivity : ComponentActivity() {

    private lateinit var prefs: DictatePrefs
    private lateinit var form: SettingsFormState
    private val probeExecutor = Executors.newSingleThreadExecutor()
    private val mainHandler = Handler(Looper.getMainLooper())
    private val personalizationSync by lazy {
        PersonalizationSync(DictateConfig.PERSONALIZATION_URL, WebViewCookieStore(), UrlConnectionTransport())
    }
    private val personalizationPushRunnable = Runnable { pushPersonalization() }
    private val loginViewModel: SettingsLoginViewModel by viewModels()

    /** True while a pulled/merged value is being applied — suppresses the push debounce. */
    private var suppressPersonalizationPush = false

    private val micPermissionLauncher =
        registerForActivityResult(ActivityResultContracts.RequestPermission()) { refreshReadiness() }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()
        prefs = DictatePrefs(this)
        form = SettingsFormState(prefs)
        // Third cleanup hook besides recorder start + IME service start: after a crash during
        // a recording, even a settings-only launch must not leave audio in the cache.
        CloudRecorder.cleanupStale(this)
        // Kicked off before setContent below; the response (if any) lands async and only
        // overwrites the editor state if the user has not started typing into it meanwhile.
        startPersonalizationPull()

        setContent {
            DictateTheme {
                SettingsScreen(
                    readiness = readinessState.value,
                    onMicClick = { micPermissionLauncher.launch(Manifest.permission.RECORD_AUDIO) },
                    onEnableClick = { startActivity(Intent(Settings.ACTION_INPUT_METHOD_SETTINGS)) },
                    onSelectClick = {
                        (getSystemService(INPUT_METHOD_SERVICE) as InputMethodManager).showInputMethodPicker()
                    },
                    onOverlayClick = { startActivity(Intent(Settings.ACTION_ACCESSIBILITY_SETTINGS)) },
                    form = form,
                    onDictionaryChange = ::onDictionaryChanged,
                    onSnippetChange = ::onSnippetChanged,
                    onCloudEnabledChange = ::onCloudEnabledChanged,
                    loginStatus = loginViewModel.status,
                    onLoginClick = { startActivity(Intent(this, LoginActivity::class.java)) },
                    onHermesHandoffClick = ::handoffToHermesVoice,
                )
            }
        }

        handleMicRequestExtra(intent?.getBooleanExtra(EXTRA_REQUEST_MIC, false) == true)
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        handleMicRequestExtra(intent.getBooleanExtra(EXTRA_REQUEST_MIC, false))
    }

    override fun onResume() {
        super.onResume()
        refreshReadiness()
        // Cloud opt-in gate (old refreshCloudRows() had the same `if (!enabled) return` before
        // its probe): without opt-in, nothing is allowed to leave the device, including a probe
        // GET against AUTH_PROBE_URL. The user comes back here from LoginActivity, so this also
        // covers "re-probe every time the screen resumes", not just once on create.
        if (form.cloudEnabled) loginViewModel.refresh()
    }

    private fun onCloudEnabledChanged(enabled: Boolean) {
        form.cloudEnabled = enabled
        // Turning the switch on is also a resume-equivalent moment for the login row: it just
        // became visible and would otherwise show a stale/never-probed status until the next
        // onResume (e.g. coming back from LoginActivity).
        if (enabled) loginViewModel.refresh()
    }

    override fun onDestroy() {
        mainHandler.removeCallbacks(personalizationPushRunnable)
        probeExecutor.shutdownNow()
        super.onDestroy()
    }

    private fun handleMicRequestExtra(requested: Boolean) {
        if (requested && !micGranted()) {
            micPermissionLauncher.launch(Manifest.permission.RECORD_AUDIO)
        }
    }

    private fun handoffToHermesVoice() {
        val draft = prefs.lastRecoveryText.trim()
        if (draft.isEmpty()) {
            Toast.makeText(this, R.string.hermes_handoff_empty, Toast.LENGTH_SHORT).show()
            return
        }
        val intent = Intent().apply {
            setClassName("net.hermes.voice", "net.hermes.voice.MainActivity")
            action = Intent.ACTION_SEND
            type = "text/plain"
            putExtra(Intent.EXTRA_TEXT, draft.take(4_000))
            addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_SINGLE_TOP)
        }
        if (runCatching { startActivity(intent) }.isFailure) {
            Toast.makeText(this, R.string.hermes_handoff_missing, Toast.LENGTH_SHORT).show()
        }
    }

    // --- Readiness card: mic permission, IME enabled/selected, overlay accessibility service ---

    private val readinessState = mutableStateOf(ReadinessState(emptyList()))

    private fun refreshReadiness() {
        readinessState.value = ReadinessState(
            listOf(
                ReadinessStep(ReadinessStepId.MIC, micGranted()),
                ReadinessStep(ReadinessStepId.ENABLE, imeEnabled()),
                ReadinessStep(ReadinessStepId.SELECT, imeSelected()),
                ReadinessStep(ReadinessStepId.OVERLAY, overlayEnabled()),
            ),
        )
    }

    private fun micGranted(): Boolean =
        ContextCompat.checkSelfPermission(this, Manifest.permission.RECORD_AUDIO) ==
            PackageManager.PERMISSION_GRANTED

    private fun imeEnabled(): Boolean {
        val imm = getSystemService(INPUT_METHOD_SERVICE) as InputMethodManager
        return imm.enabledInputMethodList.any { it.packageName == packageName }
    }

    private fun imeSelected(): Boolean =
        Settings.Secure.getString(contentResolver, Settings.Secure.DEFAULT_INPUT_METHOD)
            ?.startsWith("$packageName/") == true

    private fun overlayEnabled(): Boolean {
        val enabledServices = Settings.Secure.getString(
            contentResolver,
            Settings.Secure.ENABLED_ACCESSIBILITY_SERVICES,
        ) ?: return false
        val component = "$packageName/${DictateOverlayService::class.java.name}"
        return enabledServices.split(':').any { it.equals(component, ignoreCase = true) }
    }

    // --- Diktat Stufe 9: dictionary/snippet sync ---
    // Contract Nachschaerfung F1: every async application of a sync outcome goes through the
    // CAS-guarded DictatePrefs methods — an edit that raced the network call is never clobbered,
    // it is discarded (pull) or left for the next debounced push to reconcile (push conflict).

    private fun onDictionaryChanged(text: String) {
        form.dictionaryRules = text
        prefs.dictionaryRules = text
        if (!suppressPersonalizationPush) schedulePersonalizationPush()
    }

    private fun onSnippetChanged(text: String) {
        form.snippetRules = text
        prefs.snippetRules = text
        if (!suppressPersonalizationPush) schedulePersonalizationPush()
    }

    private fun startPersonalizationPull() {
        if (probeExecutor.isShutdown) return
        probeExecutor.execute {
            val local = LocalPersonalizationState(
                dictionaryRules = prefs.dictionaryRules,
                snippetRules = prefs.snippetRules,
                syncLastRevision = prefs.syncLastRevision,
                syncLastFingerprint = prefs.syncLastFingerprint,
            )
            val outcome = personalizationSync.pull(local) ?: return@execute
            val applied = prefs.applyPersonalizationPullOutcome(local.dictionaryRules, local.snippetRules, outcome)
            if (!applied) return@execute
            runOnUiThread {
                if (isFinishing || isDestroyed) return@runOnUiThread
                reflectPersonalizationInEditors(outcome.dictionaryRules, outcome.snippetRules)
            }
        }
    }

    private fun reflectPersonalizationInEditors(dictionaryRules: String, snippetRules: String) {
        suppressPersonalizationPush = true
        form.dictionaryRules = dictionaryRules
        form.snippetRules = snippetRules
        suppressPersonalizationPush = false
    }

    private fun schedulePersonalizationPush() {
        mainHandler.removeCallbacks(personalizationPushRunnable)
        mainHandler.postDelayed(personalizationPushRunnable, PERSONALIZATION_PUSH_DEBOUNCE_MS)
    }

    private fun pushPersonalization() {
        if (probeExecutor.isShutdown) return
        val local = LocalPersonalizationState(
            dictionaryRules = prefs.dictionaryRules,
            snippetRules = prefs.snippetRules,
            syncLastRevision = prefs.syncLastRevision,
            syncLastFingerprint = prefs.syncLastFingerprint,
        )
        probeExecutor.execute {
            val outcome = personalizationSync.push(local) ?: return@execute
            // Markers always land (they describe the server state for what was actually pushed);
            // content (e.g. after a 409 union-merge) only if nothing raced the push.
            val applied = prefs.applyPersonalizationPushOutcome(local.dictionaryRules, local.snippetRules, outcome)
            if (!applied) return@execute
            runOnUiThread {
                if (isFinishing || isDestroyed) return@runOnUiThread
                reflectPersonalizationInEditors(outcome.dictionaryRules, outcome.snippetRules)
            }
        }
    }

    companion object {
        const val EXTRA_REQUEST_MIC = "request_mic"
        private const val PERSONALIZATION_PUSH_DEBOUNCE_MS = 2_000L
    }
}
