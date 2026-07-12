package net.hermes.dictate

import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Bundle
import android.provider.Settings
import android.view.View
import android.view.inputmethod.InputMethodManager
import android.widget.Button
import android.widget.EditText
import android.widget.RadioGroup
import android.widget.SeekBar
import android.widget.Switch
import android.widget.TextView
import androidx.activity.ComponentActivity
import androidx.activity.result.contract.ActivityResultContracts
import androidx.core.content.ContextCompat
import androidx.core.widget.doAfterTextChanged
import java.util.concurrent.Executors

class SettingsActivity : ComponentActivity() {

    private lateinit var prefs: DictatePrefs
    private val probeExecutor = Executors.newSingleThreadExecutor()

    private val micPermissionLauncher =
        registerForActivityResult(ActivityResultContracts.RequestPermission()) { refreshStates() }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_settings)
        prefs = DictatePrefs(this)
        // Third cleanup hook besides recorder start + IME service start: after a crash during
        // a recording, even a settings-only launch must not leave audio in the cache.
        CloudRecorder.cleanupStale(this)

        findViewById<Button>(R.id.mic_button).setOnClickListener {
            micPermissionLauncher.launch(Manifest.permission.RECORD_AUDIO)
        }
        findViewById<Button>(R.id.enable_button).setOnClickListener {
            startActivity(Intent(Settings.ACTION_INPUT_METHOD_SETTINGS))
        }
        findViewById<Button>(R.id.select_button).setOnClickListener {
            (getSystemService(INPUT_METHOD_SERVICE) as InputMethodManager).showInputMethodPicker()
        }
        findViewById<Button>(R.id.login_button).setOnClickListener {
            startActivity(Intent(this, LoginActivity::class.java))
        }
        findViewById<Button>(R.id.overlay_button).setOnClickListener {
            startActivity(Intent(Settings.ACTION_ACCESSIBILITY_SETTINGS))
        }

        @Suppress("UseSwitchCompatOrMaterialCode")
        val cloudPreferredSwitch = findViewById<Switch>(R.id.cloud_preferred_switch)
        cloudPreferredSwitch.isChecked = prefs.cloudPreferred
        cloudPreferredSwitch.setOnCheckedChangeListener { _, checked -> prefs.cloudPreferred = checked }

        @Suppress("UseSwitchCompatOrMaterialCode")
        val flowPolishSwitch = findViewById<Switch>(R.id.flow_polish_switch)
        flowPolishSwitch.isChecked = prefs.flowPolish
        flowPolishSwitch.setOnCheckedChangeListener { _, checked -> prefs.flowPolish = checked }

        @Suppress("UseSwitchCompatOrMaterialCode")
        val localRefineSwitch = findViewById<Switch>(R.id.local_refine_switch)
        localRefineSwitch.isChecked = prefs.localRefine
        localRefineSwitch.setOnCheckedChangeListener { _, checked -> prefs.localRefine = checked }

        findViewById<SeekBar>(R.id.bubble_size_seek).apply {
            progress = prefs.overlayBubbleSize
            setOnSeekBarChangeListener(prefListener { prefs.overlayBubbleSize = it })
        }
        findViewById<SeekBar>(R.id.bubble_opacity_seek).apply {
            progress = prefs.overlayBubbleOpacity
            setOnSeekBarChangeListener(prefListener { prefs.overlayBubbleOpacity = it })
        }
        @Suppress("UseSwitchCompatOrMaterialCode")
        findViewById<Switch>(R.id.bubble_shrink_switch).apply {
            isChecked = prefs.overlayShrinkIdle
            setOnCheckedChangeListener { _, checked -> prefs.overlayShrinkIdle = checked }
        }
        @Suppress("UseSwitchCompatOrMaterialCode")
        findViewById<Switch>(R.id.local_recovery_switch).apply {
            isChecked = prefs.localRecoveryEnabled
            setOnCheckedChangeListener { _, checked ->
                prefs.localRecoveryEnabled = checked
                if (!checked) prefs.lastRecoveryText = ""
            }
        }

        findViewById<EditText>(R.id.dictionary_editor).apply {
            setText(prefs.dictionaryRules)
            doAfterTextChanged { prefs.dictionaryRules = it?.toString().orEmpty() }
        }
        findViewById<EditText>(R.id.snippet_editor).apply {
            setText(prefs.snippetRules)
            doAfterTextChanged { prefs.snippetRules = it?.toString().orEmpty() }
        }

        val radioGroup = findViewById<RadioGroup>(R.id.language_group)
        radioGroup.check(
            when (prefs.languageMode) {
                LanguageMode.GERMAN -> R.id.radio_de
                LanguageMode.ENGLISH -> R.id.radio_en
                LanguageMode.AUTO -> R.id.radio_auto
                LanguageMode.SYSTEM -> R.id.radio_system
            },
        )
        radioGroup.setOnCheckedChangeListener { _, checkedId ->
            prefs.languageMode = when (checkedId) {
                R.id.radio_de -> LanguageMode.GERMAN
                R.id.radio_en -> LanguageMode.ENGLISH
                R.id.radio_auto -> LanguageMode.AUTO
                else -> LanguageMode.SYSTEM
            }
        }

        val styleGroup = findViewById<RadioGroup>(R.id.style_group)
        styleGroup.check(
            when (prefs.styleOverride) {
                "neutral" -> R.id.style_neutral
                "formal" -> R.id.style_formal
                "casual" -> R.id.style_casual
                "concise" -> R.id.style_concise
                else -> R.id.style_auto
            },
        )
        styleGroup.setOnCheckedChangeListener { _, checkedId ->
            prefs.styleOverride = when (checkedId) {
                R.id.style_neutral -> "neutral"
                R.id.style_formal -> "formal"
                R.id.style_casual -> "casual"
                R.id.style_concise -> "concise"
                else -> "auto"
            }
        }

        @Suppress("UseSwitchCompatOrMaterialCode")
        val cloudSwitch = findViewById<Switch>(R.id.cloud_switch)
        cloudSwitch.isChecked = prefs.cloudEnabled
        cloudSwitch.setOnCheckedChangeListener { _, checked ->
            prefs.cloudEnabled = checked
            refreshCloudRows()
        }

        findViewById<TextView>(R.id.cloud_hint).text =
            getString(R.string.cloud_hint, DictateConfig.ALLOWED_HOST)

        handleMicRequestExtra(intent?.getBooleanExtra(EXTRA_REQUEST_MIC, false) == true)
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        handleMicRequestExtra(intent.getBooleanExtra(EXTRA_REQUEST_MIC, false))
    }

    override fun onResume() {
        super.onResume()
        refreshStates()
    }

    override fun onDestroy() {
        probeExecutor.shutdownNow()
        super.onDestroy()
    }

    private fun handleMicRequestExtra(requested: Boolean) {
        if (requested && !micGranted()) {
            micPermissionLauncher.launch(Manifest.permission.RECORD_AUDIO)
        }
    }

    private fun prefListener(save: (Int) -> Unit) = object : SeekBar.OnSeekBarChangeListener {
        override fun onProgressChanged(seekBar: SeekBar?, progress: Int, fromUser: Boolean) {
            if (fromUser) save(progress)
        }
        override fun onStartTrackingTouch(seekBar: SeekBar?) {}
        override fun onStopTrackingTouch(seekBar: SeekBar?) {}
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

    private fun refreshStates() {
        setRowState(R.id.mic_state, R.id.mic_button, micGranted(), R.string.state_granted)
        setRowState(R.id.enable_state, R.id.enable_button, imeEnabled(), R.string.state_enabled)
        setRowState(R.id.select_state, R.id.select_button, imeSelected(), R.string.state_selected)
        setRowState(R.id.overlay_state, R.id.overlay_button, overlayEnabled(), R.string.state_active)
        refreshCloudRows()
    }

    private fun overlayEnabled(): Boolean {
        val enabledServices = Settings.Secure.getString(
            contentResolver,
            Settings.Secure.ENABLED_ACCESSIBILITY_SERVICES,
        ) ?: return false
        val component = "$packageName/${DictateOverlayService::class.java.name}"
        return enabledServices.split(':').any { it.equals(component, ignoreCase = true) }
    }

    private fun setRowState(stateId: Int, buttonId: Int, done: Boolean, doneText: Int) {
        val state = findViewById<TextView>(stateId)
        state.text = getString(if (done) doneText else R.string.state_pending)
        state.setTextColor(
            ContextCompat.getColor(this, if (done) R.color.state_ok else R.color.text_dim),
        )
        findViewById<Button>(buttonId).visibility = if (done) View.GONE else View.VISIBLE
    }

    private fun refreshCloudRows() {
        val enabled = prefs.cloudEnabled
        findViewById<View>(R.id.login_row).visibility = if (enabled) View.VISIBLE else View.GONE
        if (!enabled) return

        val stateView = findViewById<TextView>(R.id.login_state)
        stateView.text = getString(R.string.login_state_checking)
        stateView.setTextColor(ContextCompat.getColor(this, R.color.text_dim))
        if (probeExecutor.isShutdown) return
        probeExecutor.execute {
            val signedIn = SessionProbe.check()
            runOnUiThread {
                if (isFinishing || isDestroyed) return@runOnUiThread
                val (textId, colorId) = when (signedIn) {
                    true -> R.string.login_state_in to R.color.state_ok
                    false -> R.string.login_state_out to R.color.status_error
                    null -> R.string.login_state_unreachable to R.color.status_error
                }
                stateView.text = getString(textId)
                stateView.setTextColor(ContextCompat.getColor(this, colorId))
            }
        }
    }

    companion object {
        const val EXTRA_REQUEST_MIC = "request_mic"
    }
}
