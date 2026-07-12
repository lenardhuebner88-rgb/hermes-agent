package net.hermes.dictate

import android.Manifest
import android.animation.ObjectAnimator
import android.animation.PropertyValuesHolder
import android.content.Intent
import android.content.pm.PackageManager
import android.content.res.ColorStateList
import android.inputmethodservice.InputMethodService
import android.os.Handler
import android.os.Looper
import android.os.SystemClock
import android.text.InputType
import android.view.HapticFeedbackConstants
import android.view.KeyEvent
import android.view.MotionEvent
import android.view.View
import android.view.animation.LinearInterpolator
import android.view.inputmethod.EditorInfo
import android.view.inputmethod.InputMethodManager
import android.widget.ImageButton
import android.widget.TextView
import androidx.core.content.ContextCompat
import java.util.concurrent.ExecutorService
import java.util.concurrent.Executors

/**
 * Hermes Diktat — a system-wide push-to-talk dictation keyboard (Wispr-Flow replacement).
 *
 * All dictation logic lives in the pure [DictationController]; this service only executes its
 * commands against Android: SpeechRecognizer, MediaRecorder, InputConnection, and the panel UI.
 *
 * Privacy: dictated text and audio are never logged; audio leaves the device only on the
 * explicit per-use cloud opt-in.
 */
class DictateInputMethodService :
    InputMethodService(),
    OnDeviceDictation.Callbacks,
    CloudRecorder.Events {

    private lateinit var controller: DictationController
    private val mainHandler = Handler(Looper.getMainLooper())
    private lateinit var prefs: DictatePrefs
    private lateinit var uploadExecutor: ExecutorService
    private lateinit var statusExecutor: ExecutorService

    private var dictation: OnDeviceDictation? = null
    private var recorder: CloudRecorder? = null
    private val transcriber by lazy {
        CloudTranscriber(DictateConfig.TRANSCRIBE_URL, WebViewCookieStore(), UrlConnectionTransport())
    }
    private val statusReporter by lazy {
        DictateStatusReporter(DictateConfig.STATUS_URL, WebViewCookieStore(), UrlConnectionTransport())
    }

    /** Audio captured for the cloud path, alive only between StopRecording and Upload. */
    private var pendingAudio: ByteArray? = null
    private var cloudAppCategory: String? = null
    private var cloudStyle: String? = null
    private var cloudPolishAllowed = true

    /** Field text before the current composing region; basis for spacing/capitalization. */
    private var segmentBefore: CharSequence? = null
    private var composingActive = false
    private var lastCommittedText: String? = null
    private var dictationStartedAtMs: Long? = null

    private var panel: View? = null
    private var statusView: TextView? = null
    private var cloudChip: TextView? = null
    private var micButton: ImageButton? = null
    private var micPulse: ObjectAnimator? = null

    private val statusResetRunnable = Runnable { showStatus(getString(R.string.status_idle), error = false) }

    /** Set in onDestroy; late async callbacks (upload thread posts) must become no-ops. */
    private var destroyed = false

    override fun onCreate() {
        super.onCreate()
        prefs = DictatePrefs(this)
        val pipeline = DictationTextPipeline(
            dictionaryRules = { prefs.dictionaryRules },
            snippetRules = { prefs.snippetRules },
            languageTag = { prefs.recognitionLanguageTag.takeIf(String::isNotBlank) },
            localRefine = { prefs.localRefine },
        )
        controller = DictationController(pipeline::process)
        uploadExecutor = Executors.newSingleThreadExecutor()
        statusExecutor = Executors.newSingleThreadExecutor()
        // A process killed mid-recording must not leave audio in the cache indefinitely.
        CloudRecorder.cleanupStale(this)
        reportStatus(DictateStatusEvent.CONTACT)
    }

    override fun onDestroy() {
        destroyed = true
        dictation?.destroy()
        recorder?.abort()
        uploadExecutor.shutdownNow()
        statusExecutor.shutdownNow()
        micPulse?.cancel()
        micPulse = null
        mainHandler.removeCallbacksAndMessages(null)
        super.onDestroy()
    }

    // The panel is compact; never switch to the fullscreen extract UI in landscape.
    override fun onEvaluateFullscreenMode(): Boolean = false

    override fun onCreateInputView(): View {
        val view = layoutInflater.inflate(R.layout.keyboard_view, null)
        panel = view
        statusView = view.findViewById(R.id.status)
        cloudChip = view.findViewById(R.id.cloud_chip)
        micButton = view.findViewById(R.id.key_mic)

        micButton?.setOnClickListener {
            it.performHapticFeedback(HapticFeedbackConstants.KEYBOARD_TAP)
            onMicTapped()
        }
        cloudChip?.setOnClickListener {
            it.performHapticFeedback(HapticFeedbackConstants.KEYBOARD_TAP)
            run(controller.cloudToggleTapped(prefs.cloudEnabled))
        }
        view.findViewById<View>(R.id.key_switch).apply {
            setOnClickListener { switchAway() }
            setOnLongClickListener {
                (getSystemService(INPUT_METHOD_SERVICE) as InputMethodManager).showInputMethodPicker()
                true
            }
        }
        wireBackspace(view.findViewById(R.id.key_backspace))
        view.findViewById<View>(R.id.key_space).setOnClickListener { commitRaw(" ") }
        view.findViewById<View>(R.id.key_enter).setOnClickListener { pressEnter() }
        listOf(
            R.id.key_period to ".",
            R.id.key_comma to ",",
            R.id.key_question to "?",
            R.id.key_exclamation to "!",
        ).forEach { (id, char) ->
            view.findViewById<View>(id).setOnClickListener { commitRaw(char) }
        }
        return view
    }

    override fun onStartInputView(editorInfo: EditorInfo?, restarting: Boolean) {
        super.onStartInputView(editorInfo, restarting)
        cloudChip?.visibility = if (prefs.cloudEnabled) View.VISIBLE else View.GONE
        refreshModeChip()
        applyStatus(UiStatus.Idle)
    }

    override fun onFinishInputView(finishingInput: Boolean) {
        run(controller.hidden())
        super.onFinishInputView(finishingInput)
    }

    override fun onWindowHidden() {
        run(controller.hidden())
        super.onWindowHidden()
    }

    override fun onFinishInput() {
        // Fires on every editor teardown — including a focus switch to another field while the
        // keyboard stays visible, which onFinishInputView does NOT cover. A dictation started
        // in field A must never keep writing into field B.
        run(controller.hidden())
        super.onFinishInput()
    }

    // --- Mic + permission ---

    private fun onMicTapped() {
        if (ContextCompat.checkSelfPermission(this, Manifest.permission.RECORD_AUDIO) !=
            PackageManager.PERMISSION_GRANTED
        ) {
            applyStatus(UiStatus.Failed(ErrorKind.MIC_PERMISSION))
            startActivity(
                Intent(this, SettingsActivity::class.java)
                    .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
                    .putExtra(SettingsActivity.EXTRA_REQUEST_MIC, true),
            )
            return
        }
        dictationStartedAtMs = SystemClock.elapsedRealtime()
        run(controller.micTapped())
    }

    // --- Command execution ---

    private fun run(cmds: List<Cmd>) {
        if (destroyed) return
        for (cmd in cmds) {
            when (cmd) {
                Cmd.StartRecognizer -> startRecognizerSegment()
                Cmd.StopRecognizer -> dictation?.stopSegment()
                Cmd.CancelRecognizer -> dictation?.stopSegment()
                Cmd.StartRecording -> startRecording()
                Cmd.StopRecording -> stopRecordingAndReport()
                Cmd.AbortRecording -> {
                    recorder?.abort()
                    pendingAudio = null
                }
                is Cmd.Upload -> startUpload(cmd.token)
                is Cmd.Preview -> showPreview(cmd.text)
                is Cmd.CommitSegment -> commitSegment(cmd.text)
                Cmd.UndoLastSegment -> undoLastSegment()
                Cmd.DeleteLastSentence -> deleteLastSentence()
                Cmd.ClearPreview -> clearPreview()
                is Cmd.Status -> applyStatus(cmd.status)
                Cmd.ModeChanged -> refreshModeChip()
            }
        }
    }

    private fun startRecognizerSegment() {
        // Snapshot the field BEFORE composing text appears; every preview/commit of this
        // segment formats against it. Chained segments re-snapshot after the previous commit.
        segmentBefore = currentInputConnection?.getTextBeforeCursor(64, 0)?.toString() ?: ""
        val d = dictation ?: run {
            // On-device only: if the dedicated recognizer is unavailable, surface it visibly
            // rather than silently falling back to the networked recognizer (privacy contract).
            if (!OnDeviceRecognizerFactory.isAvailable(this)) {
                mainHandler.post { run(controller.recognizerError(RecognizerFailure.UNAVAILABLE)) }
                return
            }
            OnDeviceDictation(
                OnDeviceRecognizerFactory(this),
                this,
                DefaultRecognizeIntentFactory(callingPackage = packageName),
            ).also { dictation = it }
        }
        if (!d.startSegment(prefs.recognitionLanguageTag)) {
            // If the recognizer rejected the segment, it is likely wedged. Destroy the instance,
            // rebind a fresh one, and retry once.
            d.recreate()
            if (!d.startSegment(prefs.recognitionLanguageTag)) {
                mainHandler.post { run(controller.recognizerError(RecognizerFailure.BUSY)) }
            }
        }
    }

    private fun startRecording() {
        val packageName = currentInputEditorInfo?.packageName
        cloudAppCategory = DictationContext.category(packageName).wireName()
        cloudStyle = prefs.styleForPackage(packageName)
        cloudPolishAllowed = !SensitiveFieldPolicy.isSensitive(currentInputEditorInfo?.inputType ?: 0)
        val r = recorder ?: CloudRecorder(this, this).also { recorder = it }
        if (!r.start()) {
            mainHandler.post { run(controller.recordingError()) }
        }
    }

    private fun stopRecordingAndReport() {
        val bytes = recorder?.stopAndRead()
        pendingAudio = bytes
        mainHandler.post { run(controller.recordingReady(bytes != null)) }
    }

    private fun startUpload(token: Int) {
        val audio = pendingAudio
        pendingAudio = null
        if (audio == null || uploadExecutor.isShutdown) {
            mainHandler.post { run(controller.uploadFinished(token, CloudOutcome.Server("No audio"))) }
            return
        }
        uploadExecutor.execute {
            val outcome = transcriber.transcribe(
                audio,
                "audio/mp4",
                language = prefs.languageHint,
                polish = prefs.flowPolish && cloudPolishAllowed,
                appCategory = cloudAppCategory,
                style = cloudStyle,
            )
            cloudAppCategory = null
            cloudStyle = null
            mainHandler.post { run(controller.uploadFinished(token, outcome)) }
        }
    }

    // --- Recognizer events (main thread) ---

    override fun onPartial(text: String) {
        run(controller.recognizerPartial(text))
    }

    override fun onFinal(text: String) {
        run(controller.recognizerFinal(text))
    }

    override fun onError(failure: RecognizerFailure) {
        // A busy recognizer instance is wedged; drop it so the retry binds a fresh one.
        if (failure == RecognizerFailure.BUSY) dictation?.recreate()
        run(controller.recognizerError(failure))
    }

    // --- Recorder events (recorder thread) ---

    override fun onMaxDuration() {
        mainHandler.post { run(controller.maxDurationReached()) }
    }

    override fun onRecorderError() {
        mainHandler.post { run(controller.recordingError()) }
    }

    // --- Text output ---

    private fun showPreview(text: String) {
        val ic = currentInputConnection ?: return
        ic.setComposingText(CommitFormatter.format(segmentBefore, text), 1)
        composingActive = true
    }

    private fun commitSegment(text: String) {
        val ic = currentInputConnection ?: return
        // With an active composing region the live before-cursor text would include the
        // preview itself — use the snapshot from the segment start instead.
        val basis = if (composingActive) segmentBefore else ic.getTextBeforeCursor(64, 0)
        val formatted = CommitFormatter.format(basis, text)
        ic.beginBatchEdit()
        ic.commitText(formatted, 1)
        ic.endBatchEdit()
        lastCommittedText = formatted
        composingActive = false
        reportStatus(DictateStatusEvent.SUCCESS)
    }

    private fun undoLastSegment() {
        val ic = currentInputConnection ?: return
        val inserted = lastCommittedText ?: return
        val before = ic.getTextBeforeCursor(inserted.length, 0)?.toString().orEmpty()
        if (before == inserted) {
            ic.deleteSurroundingText(inserted.length, 0)
            lastCommittedText = null
        }
    }

    private fun deleteLastSentence() {
        val ic = currentInputConnection ?: return
        val before = ic.getTextBeforeCursor(2_000, 0)?.toString().orEmpty()
        val edit = DictationEdits.deleteLastSentence(before, before.length) ?: return
        ic.deleteSurroundingText(edit.deletedChars, 0)
        lastCommittedText = null
    }

    private fun clearPreview() {
        if (!composingActive) return
        currentInputConnection?.apply {
            setComposingText("", 1)
            finishComposingText()
        }
        composingActive = false
    }

    /**
     * A manual key while dictation is active: keep the spoken partial (finalize the composing
     * preview as committed text), cancel the recognizer/recording so nothing arrives on top,
     * then let the key act. Without this, commitText from a key would silently REPLACE the
     * composing region and the later final result would format against a stale snapshot.
     */
    private fun interruptDictationForManualKey() {
        if (composingActive) {
            currentInputConnection?.finishComposingText()
            composingActive = false
        }
        run(controller.interrupted())
    }

    private fun commitRaw(text: String) {
        panel?.performHapticFeedback(HapticFeedbackConstants.KEYBOARD_TAP)
        interruptDictationForManualKey()
        currentInputConnection?.commitText(text, 1)
    }

    private fun pressEnter() {
        panel?.performHapticFeedback(HapticFeedbackConstants.KEYBOARD_TAP)
        interruptDictationForManualKey()
        val info = currentInputEditorInfo
        val multiline = info != null &&
            (info.inputType and InputType.TYPE_MASK_CLASS) == InputType.TYPE_CLASS_TEXT &&
            (info.inputType and InputType.TYPE_TEXT_FLAG_MULTI_LINE) != 0
        val action = info?.let { it.imeOptions and EditorInfo.IME_MASK_ACTION } ?: EditorInfo.IME_ACTION_NONE
        val noEnterAction = info != null && (info.imeOptions and EditorInfo.IME_FLAG_NO_ENTER_ACTION) != 0
        // IME convention: an explicit editor action wins unless the editor opted out via
        // IME_FLAG_NO_ENTER_ACTION; only then does multiline mean "insert a newline".
        when {
            !noEnterAction && action != EditorInfo.IME_ACTION_NONE && action != EditorInfo.IME_ACTION_UNSPECIFIED ->
                currentInputConnection?.performEditorAction(action)
            multiline -> currentInputConnection?.commitText("\n", 1)
            else -> sendDownUpKeyEvents(KeyEvent.KEYCODE_ENTER)
        }
    }

    private fun wireBackspace(key: View) {
        val repeat = object : Runnable {
            override fun run() {
                sendDownUpKeyEvents(KeyEvent.KEYCODE_DEL)
                mainHandler.postDelayed(this, 55)
            }
        }
        key.setOnTouchListener { v, event ->
            when (event.actionMasked) {
                MotionEvent.ACTION_DOWN -> {
                    v.isPressed = true
                    v.performHapticFeedback(HapticFeedbackConstants.KEYBOARD_TAP)
                    interruptDictationForManualKey()
                    sendDownUpKeyEvents(KeyEvent.KEYCODE_DEL)
                    mainHandler.postDelayed(repeat, 350)
                    true
                }
                MotionEvent.ACTION_UP, MotionEvent.ACTION_CANCEL -> {
                    v.isPressed = false
                    mainHandler.removeCallbacks(repeat)
                    if (event.actionMasked == MotionEvent.ACTION_UP) v.performClick()
                    true
                }
                else -> false
            }
        }
    }

    private fun switchAway() {
        // Back to the previously used keyboard; if there is none, offer the system picker.
        if (!switchToPreviousInputMethod()) {
            (getSystemService(INPUT_METHOD_SERVICE) as InputMethodManager).showInputMethodPicker()
        }
    }

    // --- Panel state ---

    private fun applyStatus(status: UiStatus) {
        mainHandler.removeCallbacks(statusResetRunnable)
        when (status) {
            UiStatus.Idle -> showStatus(getString(R.string.status_idle), error = false)
            UiStatus.Listening -> showStatus(getString(R.string.status_listening), error = false)
            UiStatus.Recording -> showStatus(getString(R.string.status_recording), error = false)
            UiStatus.Uploading -> showStatus(getString(R.string.status_uploading), error = false)
            is UiStatus.CloudDone -> {
                val label = status.provider
                    ?.let { getString(R.string.status_cloud_done_provider, it) }
                    ?: getString(R.string.status_cloud_done)
                showStatus(label, error = false)
                mainHandler.postDelayed(statusResetRunnable, 2_500)
            }
            is UiStatus.Failed -> {
                reportStatus(DictateStatusEvent.FAILURE, error = status.kind)
                showStatus(getString(errorText(status.kind)), error = true)
                mainHandler.postDelayed(statusResetRunnable, 5_000)
            }
        }
        refreshMicVisual()
    }

    private fun reportStatus(event: DictateStatusEvent, error: ErrorKind? = null) {
        // The existing cloud switch is the user's explicit network opt-in. Metadata must not
        // create a new outbound channel while privacy-first on-device mode is selected.
        if (!prefs.cloudEnabled || !::statusExecutor.isInitialized || statusExecutor.isShutdown) return
        val latency = if (event == DictateStatusEvent.SUCCESS) {
            dictationStartedAtMs?.let { (SystemClock.elapsedRealtime() - it).coerceAtLeast(0) }
        } else null
        if (event == DictateStatusEvent.SUCCESS || event == DictateStatusEvent.FAILURE) {
            dictationStartedAtMs = null
        }
        val snapshot = DictateStatusSnapshot(
            appVersion = DictateAppVersion.current(this),
            engine = if (controller.mode == Mode.CLOUD) "cloud" else "on_device",
            language = prefs.languageMode.name.lowercase(),
            style = prefs.styleOverride,
            surface = "ime",
            microphonePermission = ContextCompat.checkSelfPermission(this, Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED,
            serviceEnabled = true,
            event = event,
            latencyMs = latency,
            lastError = error?.name?.lowercase(),
        )
        statusExecutor.execute { statusReporter.report(snapshot) }
    }

    private fun errorText(kind: ErrorKind): Int = when (kind) {
        ErrorKind.NO_SPEECH -> R.string.err_no_speech
        ErrorKind.LANGUAGE_UNAVAILABLE -> R.string.err_language_unavailable
        ErrorKind.RECOGNIZER_UNAVAILABLE -> R.string.err_recognizer_unavailable
        ErrorKind.RECOGNIZER_BUSY -> R.string.err_recognizer_busy
        ErrorKind.RECOGNIZER_OTHER -> R.string.err_recognizer_other
        ErrorKind.MIC_PERMISSION -> R.string.err_mic_permission
        ErrorKind.RECORDING_FAILED -> R.string.err_recording_failed
        ErrorKind.CLOUD_AUTH -> R.string.err_cloud_auth
        ErrorKind.CLOUD_NETWORK -> R.string.err_cloud_network
        ErrorKind.CLOUD_SERVER -> R.string.err_cloud_server
        ErrorKind.CLOUD_TOO_LARGE -> R.string.err_cloud_too_large
        ErrorKind.CLOUD_EMPTY -> R.string.err_cloud_empty
        // Not reachable from the IME path (InputConnection commits directly), but the when
        // must stay exhaustive.
        ErrorKind.INSERT_FAILED -> R.string.err_insert_failed
    }

    private fun showStatus(text: String, error: Boolean) {
        statusView?.text = text
        statusView?.setTextColor(
            ContextCompat.getColor(this, if (error) R.color.status_error else R.color.text_dim),
        )
    }

    private fun refreshModeChip() {
        val chip = cloudChip ?: return
        val cloud = controller.mode == Mode.CLOUD
        chip.isSelected = cloud
        chip.text = getString(if (cloud) R.string.chip_cloud else R.string.chip_on_device)
        chip.setTextColor(
            ContextCompat.getColor(this, if (cloud) R.color.cloud else R.color.text_dim),
        )
    }

    private fun refreshMicVisual() {
        val mic = micButton ?: return
        val active = controller.phase == DictationController.Phase.LISTENING ||
            controller.phase == DictationController.Phase.STOPPING ||
            controller.phase == DictationController.Phase.RECORDING ||
            controller.phase == DictationController.Phase.WAITING_FILE
        val uploading = controller.phase == DictationController.Phase.UPLOADING

        mic.setImageResource(if (active) R.drawable.ic_stop else R.drawable.ic_mic)
        val color = when {
            active -> R.color.listening
            uploading -> R.color.key_bg
            controller.mode == Mode.CLOUD -> R.color.cloud
            else -> R.color.accent
        }
        mic.backgroundTintList = ColorStateList.valueOf(ContextCompat.getColor(this, color))
        mic.alpha = if (uploading) 0.5f else 1f

        if (active && micPulse == null) {
            micPulse = ObjectAnimator.ofPropertyValuesHolder(
                mic,
                PropertyValuesHolder.ofFloat(View.SCALE_X, 1f, 1.08f),
                PropertyValuesHolder.ofFloat(View.SCALE_Y, 1f, 1.08f),
            ).apply {
                duration = 600
                repeatCount = ObjectAnimator.INFINITE
                repeatMode = ObjectAnimator.REVERSE
                interpolator = LinearInterpolator()
                start()
            }
        } else if (!active) {
            micPulse?.cancel()
            micPulse = null
            mic.scaleX = 1f
            mic.scaleY = 1f
        }
    }
}
