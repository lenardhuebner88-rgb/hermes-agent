package net.hermes.dictate

import android.Manifest
import android.accessibilityservice.AccessibilityService
import android.content.Intent
import android.content.ClipData
import android.content.ClipboardManager
import android.content.pm.PackageManager
import android.content.res.ColorStateList
import android.graphics.PixelFormat
import android.os.Build
import android.os.Handler
import android.os.Looper
import android.os.SystemClock
import android.provider.Settings
import android.util.DisplayMetrics
import android.view.Gravity
import android.view.HapticFeedbackConstants
import android.view.MotionEvent
import android.view.View
import android.view.ViewConfiguration
import android.view.WindowManager
import android.view.WindowInsets
import android.view.animation.DecelerateInterpolator
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

    private lateinit var controller: DictationController
    private val mainHandler = Handler(Looper.getMainLooper())
    private lateinit var prefs: DictatePrefs
    private lateinit var windowManager: WindowManager
    private lateinit var committer: AccessibilityNodeCommitter
    private val uploadExecutor = Executors.newSingleThreadExecutor()
    private val probeExecutor = Executors.newSingleThreadExecutor()
    private val statusExecutor = Executors.newSingleThreadExecutor()

    private val transcriber by lazy {
        CloudTranscriber(DictateConfig.TRANSCRIBE_URL, WebViewCookieStore(), UrlConnectionTransport())
    }
    private val statusReporter by lazy {
        DictateStatusReporter(DictateConfig.STATUS_URL, WebViewCookieStore(), UrlConnectionTransport())
    }
    private val personalizationSync by lazy {
        PersonalizationSync(DictateConfig.PERSONALIZATION_URL, WebViewCookieStore(), UrlConnectionTransport())
    }

    private var dictation: OnDeviceDictation? = null
    private var recorder: CloudRecorder? = null
    private var pendingAudio: RecordedAudio? = null
    private var cloudAppCategory: String? = null
    private var cloudStyle: String? = null
    private var cloudPolishAllowed = true
    private var lastCommittedText: String? = null
    // The exact node the last commit went into. A visible undo must never delete from a
    // different field just because its text happens to end the same way.
    private var lastCommitNode: AccessibilityNodeInfo? = null
    private var retryUsed = false
    private var dictationStartedAtMs: Long? = null

    private var focusedNode: AccessibilityNodeInfo? = null
    private var overlayView: View? = null
    private var overlayParams: WindowManager.LayoutParams? = null
    private var expanded = false
    private var destroyed = false
    private var cloudLoggedIn = false
    private var currentStatus: UiStatus = UiStatus.Idle

    private val statusResetRunnable = Runnable { applyStatus(UiStatus.Idle) }

    override fun onServiceConnected() {
        super.onServiceConnected()
        prefs = DictatePrefs(this)
        val pipeline = DictationTextPipeline(
            dictionaryRules = { prefs.dictionaryRules },
            snippetRules = { prefs.snippetRules },
            languageTag = { prefs.recognitionLanguageTag.takeIf(String::isNotBlank) },
            localRefine = { prefs.localRefine },
        )
        controller = DictationController(
            previewTransform = pipeline::processPartial,
            transform = pipeline::process,
        )
        windowManager = getSystemService(WINDOW_SERVICE) as WindowManager
        committer = AccessibilityNodeCommitter(this)
        CloudRecorder.cleanupStale(this)
        addBubbleWindow()
        refreshLoginState()
        reportStatus(DictateStatusEvent.CONTACT)
        attemptPersonalizationPull()
    }

    override fun onUnbind(intent: Intent?): Boolean {
        destroyed = true
        dictation?.destroy()
        recorder?.abort()
        uploadExecutor.shutdownNow()
        probeExecutor.shutdownNow()
        statusExecutor.shutdownNow()
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
        val nowEditable = eligibleNode(node)
        if (nowEditable) {
            mainHandler.removeCallbacks(focusLossRunnable)
            // IME contract parity: a dictation started in field A must never keep writing into
            // field B. A DIFFERENT editable node taking focus mid-session hard-stops it.
            if (controller.phase != DictationController.Phase.IDLE &&
                focusedNode != null && node != focusedNode
            ) {
                run(controller.hidden())
            }
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
        val nowEditable = eligibleNode(node)
        if (!nowEditable && controller.phase != DictationController.Phase.IDLE) {
            run(controller.hidden())
        }
        focusedNode = if (nowEditable) node else null
        updateBubbleVisibility()
    }

    private fun eligibleNode(node: AccessibilityNodeInfo?): Boolean =
        node != null &&
            node.isEditable &&
            !SensitiveFieldPolicy.isSensitive(node.inputType, passwordNode = node.isPassword) &&
            !BankingAppPolicy.isBlocked(node.packageName)

    // --- Overlay window ---

    private fun addBubbleWindow() {
        val bubble = layoutInflater().inflate(R.layout.overlay_bubble, null)
        overlayView = bubble
        val screen = currentScreen()
        val density = resources.displayMetrics.density
        val sizePx = (BubbleAppearance.sizeDp(prefs.overlayBubbleSize, prefs.overlayShrinkIdle) * density).toInt()
        val marginPx = edgeMarginPx()
        val x = initialBubbleX(screen, sizePx, marginPx)
        val y = prefs.overlayBubbleY.takeIf { it >= 0 }
            ?.let { BubblePlacement.clampY(it, sizePx, screen, marginPx) }
            ?: BubblePlacement.centeredY(sizePx, screen, marginPx)
        val params = WindowManager.LayoutParams(
            WindowManager.LayoutParams.WRAP_CONTENT,
            WindowManager.LayoutParams.WRAP_CONTENT,
            WindowManager.LayoutParams.TYPE_ACCESSIBILITY_OVERLAY,
            WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE or WindowManager.LayoutParams.FLAG_LAYOUT_NO_LIMITS,
            PixelFormat.TRANSLUCENT,
        ).apply {
            // Gravity is fixed from here on: with X free to drag, START vs END would just make
            // the same coordinates mean different things depending on which edge was last docked.
            gravity = Gravity.TOP or Gravity.START
            this.x = x
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
        val visible = OverlaySafety.shouldShow(focusedNode != null, controller.phase)
        overlayView?.visibility = if (visible) View.VISIBLE else View.GONE
        if (!expanded) applyBubbleAppearance(active = controller.phase != DictationController.Phase.IDLE)
    }

    private fun applyBubbleAppearance(active: Boolean) {
        val view = overlayView ?: return
        val params = overlayParams ?: return
        val density = resources.displayMetrics.density
        val sizeDp = BubbleAppearance.sizeDp(
            prefs.overlayBubbleSize,
            idleShrink = prefs.overlayShrinkIdle && !active,
        )
        params.width = (sizeDp * density).toInt()
        params.height = (sizeDp * density).toInt()
        val screen = currentScreen()
        // Size can change (idle shrink) without a drag — reclamp the dragged-to X against the
        // new width instead of resetting it to the edge.
        params.x = BubblePlacement.clampX(params.x, params.width, screen, edgeMarginPx())
        params.y = BubblePlacement.clampY(params.y, params.height, screen, edgeMarginPx())
        view.alpha = prefs.overlayBubbleOpacity / 100f
        runCatching { windowManager.updateViewLayout(view, params) }
        overlayView?.takeIf { !expanded }?.setBackgroundResource(
            if (active) R.drawable.bg_bubble_active else R.drawable.bg_bubble,
        )
    }

    /** Drag-to-move anywhere on screen; a plain tap (no meaningful drag) starts/stops dictation. */
    private fun wireBubbleTouch(view: View, params: WindowManager.LayoutParams) {
        var startX = 0
        var startY = 0
        var startRawX = 0f
        var startRawY = 0f
        var longPressed = false
        val gesture = BubbleGesture(ViewConfiguration.get(this).scaledTouchSlop.toFloat())
        val longPress = Runnable {
            if (!gesture.moved) {
                longPressed = true
                view.performHapticFeedback(HapticFeedbackConstants.LONG_PRESS)
                startActivity(
                    Intent(this, SettingsActivity::class.java)
                        .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK),
                )
            }
        }
        view.setOnTouchListener { v, event ->
            when (event.actionMasked) {
                MotionEvent.ACTION_DOWN -> {
                    startX = params.x
                    startY = params.y
                    startRawX = event.rawX
                    startRawY = event.rawY
                    gesture.begin(event.rawX, event.rawY)
                    longPressed = false
                    mainHandler.postDelayed(longPress, LONG_PRESS_MS)
                    true
                }
                MotionEvent.ACTION_MOVE -> {
                    val dx = (event.rawX - startRawX).toInt()
                    val dy = (event.rawY - startRawY).toInt()
                    val moved = gesture.move(event.rawX, event.rawY)
                    if (moved) mainHandler.removeCallbacks(longPress)
                    if (moved) {
                        // Clamp inside the screen: with FLAG_LAYOUT_NO_LIMITS and persisted
                        // position the bubble could otherwise be parked off-screen permanently.
                        val screen = currentScreen()
                        params.x = BubblePlacement.clampX(startX + dx, v.width, screen, edgeMarginPx())
                        params.y = BubblePlacement.clampY(startY + dy, v.height, screen, edgeMarginPx())
                        runCatching { windowManager.updateViewLayout(v, params) }
                    }
                    true
                }
                MotionEvent.ACTION_UP, MotionEvent.ACTION_CANCEL -> {
                    mainHandler.removeCallbacks(longPress)
                    when (gesture.finish(event.actionMasked == MotionEvent.ACTION_CANCEL, longPressed)) {
                        BubbleGesture.Finish.DRAG -> {
                            // Free placement: no more edge snap, just persist where it was let go.
                            prefs.overlayBubbleX = params.x
                            prefs.overlayBubbleY = params.y
                        }
                        BubbleGesture.Finish.TAP -> {
                            v.performHapticFeedback(HapticFeedbackConstants.KEYBOARD_TAP)
                            v.performClick()
                            onMicTapped()
                        }
                        BubbleGesture.Finish.LONG_PRESS, BubbleGesture.Finish.CANCEL -> {}
                    }
                    true
                }
                else -> false
            }
        }
    }

    private fun currentScreen(): BubbleScreen {
        if (Build.VERSION.SDK_INT >= 30) {
            val metrics = windowManager.currentWindowMetrics
            val insets = metrics.windowInsets.getInsetsIgnoringVisibility(WindowInsets.Type.systemBars())
            return BubbleScreen(
                widthPx = metrics.bounds.width(),
                heightPx = metrics.bounds.height(),
                topInsetPx = insets.top,
                bottomInsetPx = insets.bottom,
            )
        }
        @Suppress("DEPRECATION")
        val legacy = DisplayMetrics().also { windowManager.defaultDisplay.getMetrics(it) }
        return BubbleScreen(legacy.widthPixels, legacy.heightPixels, 0, 0)
    }

    private fun edgeMarginPx(): Int = (BUBBLE_EDGE_MARGIN_DP * resources.displayMetrics.density).toInt()

    /**
     * Once the user has dragged the bubble at least once, [DictatePrefs.overlayBubbleX] is real
     * and wins; before that, fall back to [BubblePlacement.migratedX] from the legacy edge flag.
     */
    private fun initialBubbleX(screen: BubbleScreen, bubbleWidthPx: Int, marginPx: Int): Int {
        val stored = prefs.overlayBubbleX
        return if (stored >= 0) {
            BubblePlacement.clampX(stored, bubbleWidthPx, screen, marginPx)
        } else {
            BubblePlacement.migratedX(prefs.overlayBubbleOnRight, bubbleWidthPx, screen, marginPx)
        }
    }

    private fun applyPillGeometry(view: View, params: WindowManager.LayoutParams) {
        val density = resources.displayMetrics.density
        val screen = currentScreen()
        params.width = OverlayGeometry.pillWidthPx(screen.widthPx, density)
        params.height = OverlayGeometry.pillHeightPx(density)
        // Free placement: reclamp the dragged-to X against the pill's (wider) width instead of
        // resetting to the edge. Gravity is inherited from the bubble's LayoutParams instance
        // (fixed TOP|START at creation) and intentionally untouched here.
        params.x = BubblePlacement.clampX(params.x, params.width, screen, edgeMarginPx())
        params.y = BubblePlacement.clampY(params.y, params.height, screen, edgeMarginPx())
        if (view.isAttachedToWindow) runCatching { windowManager.updateViewLayout(view, params) }
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
        val pillEntryDuration = if (expand) preparePillEntry(view) else 0L
        if (expand) {
            applyPillGeometry(view, params)
        } else {
            wireBubbleTouch(view, params)
        }
        overlayParams = params
        windowManager.addView(view, params)
        if (expand) {
            view.post {
                val liveParams = overlayParams ?: return@post
                liveParams.y = BubblePlacement.clampY(
                    liveParams.y,
                    view.height.takeIf { it > 0 }
                        ?: OverlayGeometry.pillHeightPx(resources.displayMetrics.density),
                    currentScreen(),
                    edgeMarginPx(),
                )
                runCatching { windowManager.updateViewLayout(view, liveParams) }
                playPillEntry(view, pillEntryDuration)
            }
        }
        if (!expand) applyBubbleAppearance(active = false)
        updateBubbleVisibility()
    }

    private fun preparePillEntry(view: View): Long {
        val animatorScale = runCatching {
            Settings.Global.getFloat(contentResolver, Settings.Global.ANIMATOR_DURATION_SCALE, 1f)
        }.getOrDefault(1f)
        val duration = OverlayMotion.durationMs(OverlayMotion.PILL_ENTER_MS, animatorScale)
        if (duration == 0L) {
            view.alpha = 1f
            view.scaleX = 1f
            view.scaleY = 1f
        } else {
            view.alpha = 0f
            view.scaleX = PILL_ENTER_SCALE
            view.scaleY = PILL_ENTER_SCALE
        }
        return duration
    }

    private fun playPillEntry(view: View, duration: Long) {
        if (duration == 0L) return
        view.animate()
            .alpha(1f)
            .scaleX(1f)
            .scaleY(1f)
            .setDuration(duration)
            .setInterpolator(DecelerateInterpolator())
            .start()
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
        dictationStartedAtMs = SystemClock.elapsedRealtime()
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
                Cmd.UndoLastSegment -> editFocusedField(undoLast = true)
                Cmd.DeleteLastSentence -> editFocusedField(undoLast = false)
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
                DefaultRecognizeIntentFactory(
                    callingPackage = packageName,
                    biasingPhrases = {
                        BiasingVocabulary.fromRules(prefs.dictionaryRules, prefs.snippetRules)
                    },
                ),
            ).also { dictation = it }
        }
        if (!d.startSegment(prefs.recognitionLanguageTag)) {
            d.recreate()
            if (!d.startSegment(prefs.recognitionLanguageTag)) {
                mainHandler.post { run(controller.recognizerError(RecognizerFailure.BUSY)) }
            }
        }
    }

    private fun startRecording() {
        retryUsed = false
        val node = focusedNode
        val packageName = node?.packageName?.toString()
        cloudAppCategory = DictationContext.category(packageName).wireName()
        cloudStyle = prefs.styleForPackage(packageName)
        cloudPolishAllowed = !SensitiveFieldPolicy.isSensitive(
            node?.inputType ?: 0,
            passwordNode = node?.isPassword == true,
        )
        val r = recorder ?: CloudRecorder(this, this).also { recorder = it }
        if (!r.start()) {
            mainHandler.post { run(controller.recordingError()) }
        }
    }

    private fun stopRecordingAndReport() {
        val audio = recorder?.stopAndRead()
        pendingAudio = audio
        mainHandler.post { run(controller.recordingReady(audio != null)) }
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
                audio.bytes,
                audio.mimeType,
                language = prefs.languageHint,
                polish = prefs.flowPolish && cloudPolishAllowed,
                appCategory = cloudAppCategory,
                style = cloudStyle,
                initialPrompt = BiasingVocabulary
                    .fromRules(prefs.dictionaryRules, prefs.snippetRules)
                    .joinToString(", ")
                    .ifBlank { null },
            )
            val canRetry =
                !retryUsed && (outcome is CloudOutcome.Network || outcome is CloudOutcome.Server)
            pendingAudio = if (canRetry) audio else null
            if (!canRetry) {
                cloudAppCategory = null
                cloudStyle = null
            }
            mainHandler.post { run(controller.uploadFinished(token, outcome)) }
        }
    }

    // --- Recognizer / recorder events ---

    override fun onPartial(text: String) = run(controller.recognizerPartial(text))
    override fun onFinal(text: String) = run(controller.recognizerFinal(text))
    override fun onLevel(rmsDb: Float) = updateWaveLevel(AudioLevel.fromRmsDb(rmsDb))
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

    // Cloud path's own level source (MediaRecorder.getMaxAmplitude polling in CloudRecorder),
    // already normalized onto the same 0..100 scale as onLevel(rmsDb) above.
    override fun onLevel(level: Int) = updateWaveLevel(level)

    private fun updateWaveLevel(level: Int) {
        overlayView?.findViewById<OverlayWaveView>(R.id.pill_wave)?.level = level
    }

    // --- Text output: preview stays inside the pill, only CommitSegment writes to the field ---

    private var lastPreview: String = ""

    private fun showPreview(text: String) {
        lastPreview = text
        renderOverlay()
    }

    private fun clearPillPreview() {
        lastPreview = ""
        renderOverlay()
    }

    private fun commitSegment(text: String) {
        // The cached node can be stale (recycled) by commit time — re-query live focus first.
        val live = rootInActiveWindow?.findFocus(AccessibilityNodeInfo.FOCUS_INPUT)
            ?.takeIf { it.isEditable }
        // Only ever commit into the field the dictation started in. Live focus on a DIFFERENT
        // node means the user moved on — fail visibly rather than write into the wrong field.
        val target = OverlaySafety.commitTarget(focusedNode, live)
        val committed = target?.let { committer.commit(it, text) }
        if (committed == null) {
            // Dictated text would be silently lost — surface it in the pill instead.
            applyStatus(UiStatus.Failed(ErrorKind.INSERT_FAILED))
        } else {
            lastCommittedText = committed
            lastCommitNode = target
            lastPreview = ""
            if (prefs.localRecoveryEnabled) prefs.lastRecoveryText = committed
            reportStatus(DictateStatusEvent.SUCCESS)
        }
    }

    private fun editFocusedField(undoLast: Boolean, requireCommitNode: Boolean = false): Boolean {
        val target = rootInActiveWindow?.findFocus(AccessibilityNodeInfo.FOCUS_INPUT)
            ?.takeIf { it.isEditable } ?: focusedNode ?: return false
        // Same invariant as the commit path: an edit belongs to exactly one field.
        if (requireCommitNode && lastCommitNode != null && target != lastCommitNode) return false
        val text = target.text?.toString().orEmpty()
        val cursor = target.textSelectionStart.takeIf { it in 0..text.length } ?: text.length
        val edit = if (undoLast) {
            DictationEdits.undoLastSegment(text, cursor, lastCommittedText)
        } else {
            DictationEdits.deleteLastSentence(text, cursor)
        } ?: return false
        if (!committer.applyEdit(target, edit)) return false
        lastCommittedText = null
        lastCommitNode = null
        return true
    }

    // --- Panel state ---

    private fun applyStatus(status: UiStatus) {
        mainHandler.removeCallbacks(statusResetRunnable)
        currentStatus = status
        if (status is UiStatus.Failed) {
            reportStatus(DictateStatusEvent.FAILURE, error = status.kind)
        }
        renderOverlay()
        when (status) {
            // A visible undo needs time to be seen and hit; without one the success state stays brief.
            UiStatus.Done -> mainHandler.postDelayed(
                statusResetRunnable,
                if (lastCommittedText != null) DONE_UNDOABLE_MS else DONE_VISIBLE_MS,
            )
            is UiStatus.Failed -> mainHandler.postDelayed(statusResetRunnable, 2_500)
            is UiStatus.CloudDone -> mainHandler.postDelayed(statusResetRunnable, 3_500)
            else -> {}
        }
        updateBubbleVisibility()
    }

    private fun renderOverlay() {
        val retryAvailable = currentStatus is UiStatus.Failed && pendingAudio != null &&
            ((currentStatus as UiStatus.Failed).kind == ErrorKind.CLOUD_NETWORK ||
                (currentStatus as UiStatus.Failed).kind == ErrorKind.CLOUD_SERVER)
        val presentation = OverlayViewState.from(
            status = currentStatus,
            previewText = lastPreview,
            committedText = lastCommittedText,
            retryAvailable = retryAvailable,
            errorText = { getString(errorText(it)) },
            cloudMode = controller.mode == Mode.CLOUD,
        )
        val semantics = OverlayActionSemantics.from(presentation)
        setExpanded(presentation.expanded)
        if (!presentation.expanded) return
        val view = overlayView ?: return
        overlayParams?.let { applyPillGeometry(view, it) }
        val toneColor = ContextCompat.getColor(this, toneColor(presentation.tone))
        val statusText = getString(labelText(presentation.label))
        // Two faces in one pill: while the microphone is open only the voice is shown; result and
        // error states swap in the short message that actually has something to say.
        val listening = presentation.confirmAction == OverlayConfirmAction.STOP ||
            presentation.label == OverlayLabel.PROCESSING ||
            presentation.label == OverlayLabel.UPLOADING
        view.findViewById<View>(R.id.pill_message)?.visibility =
            if (presentation.showMessage) View.VISIBLE else View.GONE
        view.findViewById<OverlayWaveView>(R.id.pill_wave)?.apply {
            visibility = if (listening) View.VISIBLE else View.GONE
            fillColor = toneColor
            // The wave replaces the status word visually, so it has to carry it for a screen reader.
            contentDescription = statusText
            if (!listening) reset()
        }
        view.findViewById<TextView>(R.id.pill_status)?.apply {
            text = statusText
            setTextColor(toneColor)
        }
        view.findViewById<TextView>(R.id.pill_text)?.apply {
            text = OverlayText.visibleBody(presentation) ?: getString(R.string.overlay_speak_now)
            contentDescription = presentation.body ?: getString(R.string.overlay_speak_now)
            setTextColor(ContextCompat.getColor(context, R.color.text_primary))
        }

        view.findViewById<ImageButton>(R.id.pill_cancel)?.apply {
            setActionEnabled(presentation.cancelEnabled)
            contentDescription = getString(semantics.cancelDescription)
            setOnClickListener(if (presentation.cancelEnabled) View.OnClickListener {
                it.performHapticFeedback(HapticFeedbackConstants.KEYBOARD_TAP)
                val commands = controller.interrupted()
                run(commands)
                lastPreview = ""
                if (commands.isEmpty()) applyStatus(UiStatus.Idle)
            } else null)
        }
        view.findViewById<ImageButton>(R.id.pill_confirm)?.apply {
            val action = presentation.confirmAction
            setActionEnabled(action != OverlayConfirmAction.NONE)
            contentDescription = getString(semantics.confirmDescription)
            setImageResource(semantics.confirmIcon)
            setOnClickListener(when (action) {
                OverlayConfirmAction.STOP -> View.OnClickListener {
                    it.performHapticFeedback(HapticFeedbackConstants.KEYBOARD_TAP)
                    run(controller.micTapped())
                }
                OverlayConfirmAction.RETRY -> View.OnClickListener {
                    it.performHapticFeedback(HapticFeedbackConstants.KEYBOARD_TAP)
                    retryUsed = true
                    reportStatus(DictateStatusEvent.RETRY)
                    run(controller.retryCloud())
                }
                OverlayConfirmAction.COPY -> View.OnClickListener {
                    it.performHapticFeedback(HapticFeedbackConstants.KEYBOARD_TAP)
                    lastCommittedText?.let { text ->
                        val clipboard = getSystemService(CLIPBOARD_SERVICE) as ClipboardManager
                        clipboard.setPrimaryClip(ClipData.newPlainText("hermes_dictate", text))
                    }
                    applyStatus(UiStatus.Idle)
                }
                OverlayConfirmAction.UNDO -> View.OnClickListener {
                    it.performHapticFeedback(HapticFeedbackConstants.KEYBOARD_TAP)
                    // Not every field accepts ACTION_SET_TEXT. Saying "done" when the text is
                    // still standing there would be the worse failure.
                    if (editFocusedField(undoLast = true, requireCommitNode = true)) {
                        lastPreview = ""
                        applyStatus(UiStatus.Idle)
                    } else {
                        applyStatus(UiStatus.Failed(ErrorKind.UNDO_FAILED))
                    }
                }
                OverlayConfirmAction.NONE -> null
            })
        }
        view.findViewById<ImageButton>(R.id.pill_undo)?.apply {
            val visible = semantics.secondaryVisible
            visibility = if (visible) View.VISIBLE else View.GONE
            if (visible) {
                contentDescription = getString(semantics.secondaryDescription)
                setImageResource(semantics.secondaryIcon)
                setOnClickListener {
                    it.performHapticFeedback(HapticFeedbackConstants.KEYBOARD_TAP)
                    // Not every field accepts ACTION_SET_TEXT. Saying "done" when the text is
                    // still standing there would be the worse failure.
                    if (editFocusedField(undoLast = true, requireCommitNode = true)) {
                        lastPreview = ""
                        applyStatus(UiStatus.Idle)
                    } else {
                        applyStatus(UiStatus.Failed(ErrorKind.UNDO_FAILED))
                    }
                }
            } else {
                setOnClickListener(null)
            }
        }
    }

    private fun View.setActionEnabled(enabled: Boolean) {
        isEnabled = enabled
        isClickable = enabled
        alpha = if (enabled) 1f else DISABLED_ACTION_ALPHA
    }

    private fun labelText(label: OverlayLabel): Int = when (label) {
        OverlayLabel.READY -> R.string.status_idle
        OverlayLabel.LISTENING -> R.string.status_listening
        OverlayLabel.PROCESSING -> R.string.status_processing
        OverlayLabel.DONE -> R.string.status_done
        OverlayLabel.RECORDING -> R.string.status_recording
        OverlayLabel.UPLOADING -> R.string.status_uploading
        OverlayLabel.ERROR -> R.string.overlay_status_error
        OverlayLabel.COPY -> R.string.overlay_status_copy
    }

    private fun toneColor(tone: OverlayTone): Int = when (tone) {
        OverlayTone.READY -> R.color.accent
        OverlayTone.LISTENING -> R.color.listening
        OverlayTone.PROCESSING -> R.color.processing
        OverlayTone.SUCCESS -> R.color.state_ok
        OverlayTone.ERROR -> R.color.status_error
        OverlayTone.CLOUD -> R.color.cloud
    }

    private fun reportStatus(event: DictateStatusEvent, error: ErrorKind? = null) {
        // The existing cloud switch is the user's explicit network opt-in. Metadata must not
        // create a new outbound channel while privacy-first on-device mode is selected.
        if (!prefs.cloudEnabled || statusExecutor.isShutdown) return
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
            surface = "overlay",
            microphonePermission = ContextCompat.checkSelfPermission(this, Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED,
            serviceEnabled = true,
            event = event,
            latencyMs = latency,
            lastError = error?.name?.lowercase(),
        )
        statusExecutor.execute { statusReporter.report(snapshot) }
    }

    /**
     * Throttled to at most one attempt per 5 min (atomic claim shared with the IME via
     * [DictatePrefs]). The outcome is applied via the CAS-guarded prefs method (contract
     * Nachschaerfung F1).
     */
    private fun attemptPersonalizationPull() {
        if (statusExecutor.isShutdown) return
        statusExecutor.execute {
            if (!prefs.tryClaimPersonalizationPullAttempt(System.currentTimeMillis())) return@execute
            val local = LocalPersonalizationState(
                dictionaryRules = prefs.dictionaryRules,
                snippetRules = prefs.snippetRules,
                syncLastRevision = prefs.syncLastRevision,
                syncLastFingerprint = prefs.syncLastFingerprint,
            )
            val outcome = personalizationSync.pull(local) ?: return@execute
            prefs.applyPersonalizationPullOutcome(local.dictionaryRules, local.snippetRules, outcome)
        }
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
        ErrorKind.UNDO_FAILED -> R.string.err_undo_failed
    }

    companion object {
        private const val LOGIN_PROBE_INTERVAL_MS = 60_000L
        private const val FOCUS_LOSS_CONFIRM_MS = 300L
        private const val LONG_PRESS_MS = 500L
        private const val VOICE_HANDOFF_DELAY_MS = 150L
        private const val DONE_VISIBLE_MS = 1_200L
        private const val DONE_UNDOABLE_MS = 3_500L
        private const val DISABLED_ACTION_ALPHA = 0.28f
        private const val BUBBLE_EDGE_MARGIN_DP = 8
        private const val PILL_ENTER_SCALE = 0.96f

    }
}
