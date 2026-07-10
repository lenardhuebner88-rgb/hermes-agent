package net.hermes.dictate

import android.Manifest
import android.accessibilityservice.AccessibilityService
import android.content.Intent
import android.content.pm.PackageManager
import android.content.res.ColorStateList
import android.graphics.PixelFormat
import android.os.Handler
import android.os.Looper
import android.util.DisplayMetrics
import android.view.Gravity
import android.view.HapticFeedbackConstants
import android.view.MotionEvent
import android.view.View
import android.view.WindowManager
import android.view.accessibility.AccessibilityEvent
import android.view.accessibility.AccessibilityNodeInfo
import android.widget.ImageButton
import android.widget.TextView
import androidx.core.content.ContextCompat
import java.util.concurrent.Executors

/**
 * Wispr-Flow-style overlay: a small draggable mic bubble floats above every app; tapping it
 * drives the SAME [DictationController] the IME uses, but writes results into the currently
 * focused field via [AccessibilityNodeInfo] actions ([AccessibilityNodeCommitter]) instead of an
 * InputConnection. The IME stays selectable as a fallback; this is the new primary UX.
 *
 * Privacy: no window content, dictated text, or audio is ever logged — only used transiently to
 * find the focused editable node and to render the pill's own live preview.
 */
class DictateOverlayService :
    AccessibilityService(),
    OnDeviceDictation.Callbacks,
    CloudRecorder.Events {

    private val controller = DictationController()
    private val mainHandler = Handler(Looper.getMainLooper())
    private lateinit var prefs: DictatePrefs
    private lateinit var windowManager: WindowManager
    private lateinit var committer: AccessibilityNodeCommitter
    private val uploadExecutor = Executors.newSingleThreadExecutor()
    private val probeExecutor = Executors.newSingleThreadExecutor()

    private val transcriber by lazy {
        CloudTranscriber(DictateConfig.TRANSCRIBE_URL, WebViewCookieStore(), UrlConnectionTransport())
    }

    private var dictation: OnDeviceDictation? = null
    private var recorder: CloudRecorder? = null
    private var pendingAudio: ByteArray? = null

    private var focusedNode: AccessibilityNodeInfo? = null
    private var overlayView: View? = null
    private var overlayParams: WindowManager.LayoutParams? = null
    private var expanded = false
    private var destroyed = false
    private var cloudLoggedIn = false

    private val statusResetRunnable = Runnable { applyStatus(UiStatus.Idle) }

    override fun onServiceConnected() {
        super.onServiceConnected()
        prefs = DictatePrefs(this)
        windowManager = getSystemService(WINDOW_SERVICE) as WindowManager
        committer = AccessibilityNodeCommitter(this)
        CloudRecorder.cleanupStale(this)
        addBubbleWindow()
        refreshLoginState()
    }

    override fun onUnbind(intent: Intent?): Boolean {
        destroyed = true
        dictation?.destroy()
        recorder?.abort()
        uploadExecutor.shutdownNow()
        probeExecutor.shutdownNow()
        mainHandler.removeCallbacksAndMessages(null)
        removeOverlayWindow()
        focusedNode = null
        return super.onUnbind(intent)
    }

    // --- Accessibility events: track the focused editable field ---

    override fun onAccessibilityEvent(event: AccessibilityEvent?) {
        when (event?.eventType) {
            AccessibilityEvent.TYPE_VIEW_FOCUSED,
            AccessibilityEvent.TYPE_WINDOWS_CHANGED,
            AccessibilityEvent.TYPE_WINDOW_STATE_CHANGED,
            AccessibilityEvent.TYPE_VIEW_TEXT_SELECTION_CHANGED,
            -> refreshFocus()
        }
    }

    override fun onInterrupt() {}

    private fun refreshFocus() {
        val node = rootInActiveWindow?.findFocus(AccessibilityNodeInfo.FOCUS_INPUT)
        val nowEditable = node != null && node.isEditable
        if (nowEditable) {
            mainHandler.removeCallbacks(focusLossRunnable)
            focusedNode = node
            updateBubbleVisibility()
        } else {
            // Our own pill/bubble window swaps fire TYPE_WINDOWS_CHANGED, during which focus can
            // transiently read as lost — killing the dictation we just started. Only hard-stop
            // (like the IME's onFinishInput) once the loss survives a short confirmation delay.
            mainHandler.removeCallbacks(focusLossRunnable)
            mainHandler.postDelayed(focusLossRunnable, FOCUS_LOSS_CONFIRM_MS)
        }
    }

    private val focusLossRunnable = Runnable {
        val node = rootInActiveWindow?.findFocus(AccessibilityNodeInfo.FOCUS_INPUT)
        val nowEditable = node != null && node.isEditable
        if (!nowEditable && controller.phase != DictationController.Phase.IDLE) {
            run(controller.hidden())
        }
        focusedNode = if (nowEditable) node else null
        updateBubbleVisibility()
    }

    // --- Overlay window ---

    private fun addBubbleWindow() {
        val bubble = layoutInflater().inflate(R.layout.overlay_bubble, null)
        overlayView = bubble
        val metrics = DisplayMetrics().also { windowManager.defaultDisplay.getMetrics(it) }
        val onRight = prefs.overlayBubbleOnRight
        val y = prefs.overlayBubbleY.takeIf { it >= 0 } ?: (metrics.heightPixels / 2)
        val params = WindowManager.LayoutParams(
            WindowManager.LayoutParams.WRAP_CONTENT,
            WindowManager.LayoutParams.WRAP_CONTENT,
            WindowManager.LayoutParams.TYPE_ACCESSIBILITY_OVERLAY,
            WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE or WindowManager.LayoutParams.FLAG_LAYOUT_NO_LIMITS,
            PixelFormat.TRANSLUCENT,
        ).apply {
            gravity = Gravity.TOP or (if (onRight) Gravity.END else Gravity.START)
            x = 0
            this.y = y
        }
        overlayParams = params
        wireBubbleTouch(bubble, params)
        windowManager.addView(bubble, params)
        updateBubbleVisibility()
    }

    private fun removeOverlayWindow() {
        overlayView?.let { runCatching { windowManager.removeView(it) } }
        overlayView = null
        overlayParams = null
    }

    private fun updateBubbleVisibility() {
        // While actively dictating the pill must stay visible even if focus tracking races
        // (rotation, transient window changes) — only hide the idle bubble on no-focus.
        val visible = focusedNode != null || controller.phase != DictationController.Phase.IDLE
        overlayView?.visibility = if (visible) View.VISIBLE else View.GONE
    }

    /** Drag-to-move with edge snap; a plain tap (no meaningful drag) starts/stops dictation. */
    private fun wireBubbleTouch(view: View, params: WindowManager.LayoutParams) {
        var startY = 0
        var startRawY = 0f
        var startRawX = 0f
        var moved = false
        view.setOnTouchListener { v, event ->
            when (event.actionMasked) {
                MotionEvent.ACTION_DOWN -> {
                    startY = params.y
                    startRawX = event.rawX
                    startRawY = event.rawY
                    moved = false
                    true
                }
                MotionEvent.ACTION_MOVE -> {
                    val dy = (event.rawY - startRawY).toInt()
                    val dx = event.rawX - startRawX
                    if (kotlin.math.abs(dx) > DRAG_SLOP || kotlin.math.abs(dy) > DRAG_SLOP) moved = true
                    if (moved) {
                        params.y = (startY + dy).coerceAtLeast(0)
                        runCatching { windowManager.updateViewLayout(v, params) }
                    }
                    true
                }
                MotionEvent.ACTION_UP, MotionEvent.ACTION_CANCEL -> {
                    if (moved) {
                        snapToEdge(v, params, event.rawX)
                        prefs.overlayBubbleY = params.y
                    } else if (event.actionMasked == MotionEvent.ACTION_UP) {
                        v.performHapticFeedback(HapticFeedbackConstants.KEYBOARD_TAP)
                        onMicTapped()
                    }
                    true
                }
                else -> false
            }
        }
    }

    private fun snapToEdge(v: View, params: WindowManager.LayoutParams, lastRawX: Float) {
        val metrics = DisplayMetrics().also { windowManager.defaultDisplay.getMetrics(it) }
        val onRight = lastRawX >= metrics.widthPixels / 2f
        prefs.overlayBubbleOnRight = onRight
        params.gravity = Gravity.TOP or (if (onRight) Gravity.END else Gravity.START)
        params.x = 0
        runCatching { windowManager.updateViewLayout(v, params) }
    }

    /** Swaps the collapsed bubble layout for the expanded pill layout, or back. */
    private fun setExpanded(expand: Boolean) {
        if (expand == expanded) return
        expanded = expand
        val current = overlayView ?: return
        val params = overlayParams ?: return
        removeOverlayWindow()
        val layout = if (expand) R.layout.overlay_pill else R.layout.overlay_bubble
        val view = layoutInflater().inflate(layout, null)
        overlayView = view
        if (expand) {
            view.findViewById<ImageButton>(R.id.pill_cancel).setOnClickListener {
                it.performHapticFeedback(HapticFeedbackConstants.KEYBOARD_TAP)
                run(controller.interrupted())
                clearPillPreview()
            }
            view.findViewById<ImageButton>(R.id.pill_confirm).setOnClickListener {
                it.performHapticFeedback(HapticFeedbackConstants.KEYBOARD_TAP)
                run(controller.micTapped())
            }
        } else {
            wireBubbleTouch(view, params)
        }
        overlayParams = params
        windowManager.addView(view, params)
        applyPillPreview(lastPreview)
        updateBubbleVisibility()
    }

    private fun layoutInflater() = android.view.LayoutInflater.from(this)

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
        if (OverlayCloudRearm.shouldRearm(controller.phase, controller.mode, prefs.cloudPreferred, prefs.cloudEnabled, cloudLoggedIn)) {
            run(controller.cloudToggleTapped(true))
        }
        run(controller.micTapped())
    }

    private fun refreshLoginState() {
        if (probeExecutor.isShutdown) return
        probeExecutor.execute {
            val signedIn = SessionProbe.check() == true
            mainHandler.post {
                if (destroyed) return@post
                cloudLoggedIn = signedIn
                mainHandler.postDelayed(::refreshLoginState, LOGIN_PROBE_INTERVAL_MS)
            }
        }
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
                Cmd.ClearPreview -> clearPillPreview()
                is Cmd.Status -> applyStatus(cmd.status)
                Cmd.ModeChanged -> {}
            }
        }
    }

    private fun startRecognizerSegment() {
        val d = dictation ?: run {
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
        if (!d.startSegment(prefs.languageTag ?: "")) {
            d.recreate()
            if (!d.startSegment(prefs.languageTag ?: "")) {
                mainHandler.post { run(controller.recognizerError(RecognizerFailure.BUSY)) }
            }
        }
    }

    private fun startRecording() {
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
                polish = prefs.flowPolish,
            )
            mainHandler.post { run(controller.uploadFinished(token, outcome)) }
        }
    }

    // --- Recognizer / recorder events ---

    override fun onPartial(text: String) = run(controller.recognizerPartial(text))
    override fun onFinal(text: String) = run(controller.recognizerFinal(text))
    override fun onError(failure: RecognizerFailure) {
        if (failure == RecognizerFailure.BUSY) dictation?.recreate()
        run(controller.recognizerError(failure))
    }

    override fun onMaxDuration() {
        mainHandler.post { run(controller.maxDurationReached()) }
    }

    override fun onRecorderError() {
        mainHandler.post { run(controller.recordingError()) }
    }

    // --- Text output: preview stays inside the pill, only CommitSegment writes to the field ---

    private var lastPreview: String = ""

    private fun showPreview(text: String) {
        lastPreview = text
        applyPillPreview(text)
    }

    private fun clearPillPreview() {
        lastPreview = ""
        applyPillPreview("")
    }

    private fun applyPillPreview(text: String) {
        if (!expanded) return
        overlayView?.findViewById<TextView>(R.id.pill_text)?.text =
            text.ifEmpty { getString(R.string.status_listening) }
    }

    private fun commitSegment(text: String) {
        // The cached node can be stale (recycled) by commit time — re-query live focus first.
        val live = rootInActiveWindow?.findFocus(AccessibilityNodeInfo.FOCUS_INPUT)
            ?.takeIf { it.isEditable }
        val node = live ?: focusedNode
        if (node == null || !committer.commit(node, text)) {
            // Dictated text would be silently lost — surface it in the pill instead.
            applyStatus(UiStatus.Failed(ErrorKind.INSERT_FAILED))
        }
    }

    // --- Panel state ---

    private fun applyStatus(status: UiStatus) {
        mainHandler.removeCallbacks(statusResetRunnable)
        val active = status is UiStatus.Listening || status is UiStatus.Recording ||
            status is UiStatus.Uploading
        setExpanded(active || status is UiStatus.Failed)
        when (status) {
            is UiStatus.Failed -> {
                overlayView?.findViewById<TextView>(R.id.pill_text)?.apply {
                    text = getString(errorText(status.kind))
                    setTextColor(ContextCompat.getColor(context, R.color.status_error))
                }
                mainHandler.postDelayed(statusResetRunnable, 2_500)
            }
            UiStatus.Idle, is UiStatus.CloudDone -> setExpanded(false)
            else -> {}
        }
        updateBubbleVisibility()
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
        ErrorKind.INSERT_FAILED -> R.string.err_insert_failed
    }

    companion object {
        private const val DRAG_SLOP = 12
        private const val LOGIN_PROBE_INTERVAL_MS = 60_000L
        private const val FOCUS_LOSS_CONFIRM_MS = 300L
    }
}
