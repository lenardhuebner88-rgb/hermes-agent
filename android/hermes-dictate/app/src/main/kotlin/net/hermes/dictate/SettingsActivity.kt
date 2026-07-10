package net.hermes.dictate

import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Bundle
import android.provider.Settings
import android.view.View
import android.view.inputmethod.InputMethodManager
import android.widget.Button
import android.widget.RadioGroup
import android.widget.Switch
import android.widget.TextView
import androidx.activity.ComponentActivity
import androidx.activity.result.contract.ActivityResultContracts
import androidx.core.content.ContextCompat
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

        val radioGroup = findViewById<RadioGroup>(R.id.language_group)
        radioGroup.check(
            when (prefs.languageTag) {
                "de-DE" -> R.id.radio_de
                "en-US" -> R.id.radio_en
                else -> R.id.radio_system
            },
        )
        radioGroup.setOnCheckedChangeListener { _, checkedId ->
            prefs.languageTag = when (checkedId) {
                R.id.radio_de -> "de-DE"
                R.id.radio_en -> "en-US"
                else -> null
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
