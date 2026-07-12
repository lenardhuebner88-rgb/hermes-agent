package net.hermes.voice

import android.annotation.SuppressLint
import android.app.Activity
import android.content.ActivityNotFoundException
import android.content.Intent
import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import android.content.pm.PackageManager
import android.media.projection.MediaProjectionManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.provider.AlarmClock
import android.provider.Settings
import android.webkit.PermissionRequest
import android.webkit.RenderProcessGoneDetail
import android.webkit.WebChromeClient
import android.webkit.WebResourceRequest
import android.webkit.WebView
import android.webkit.WebViewClient
import androidx.activity.ComponentActivity
import androidx.activity.result.contract.ActivityResultContracts
import androidx.core.content.ContextCompat
import androidx.webkit.WebViewCompat
import androidx.webkit.WebViewFeature

class MainActivity : ComponentActivity() {

    private lateinit var webView: WebView

    /** An audio/video permission request from the web page, parked while we ask Android. */
    private var pendingWebPermissionRequest: PermissionRequest? = null

    /** True while we are asking for POST_NOTIFICATIONS ahead of a screen-capture start. */
    private var pendingCaptureStartAfterNotificationPrompt = false
    private val phoneActionGate = PhoneActionExecutionGate()
    private var pendingDictationDraft: String? = null

    private val webPermissionLauncher =
        registerForActivityResult(ActivityResultContracts.RequestMultiplePermissions()) {
            val request = pendingWebPermissionRequest
            pendingWebPermissionRequest = null
            if (request != null) resolveWebPermissionRequest(request)
        }

    private val notificationPermissionLauncher =
        registerForActivityResult(ActivityResultContracts.RequestPermission()) {
            // Proceed regardless of grant result: a denied POST_NOTIFICATIONS just means the
            // foreground-service notification may be less visible on some OEMs, it does not
            // block starting the capture.
            if (pendingCaptureStartAfterNotificationPrompt) {
                pendingCaptureStartAfterNotificationPrompt = false
                launchScreenCaptureFlow()
            }
        }

    private val screenCaptureLauncher =
        registerForActivityResult(ActivityResultContracts.StartActivityForResult()) { result ->
            val data = result.data
            if (result.resultCode == Activity.RESULT_OK && data != null &&
                HermesBridge.captureState.advanceToStarting()
            ) {
                val intent = Intent(this, MediaProjectionService::class.java).apply {
                    action = MediaProjectionService.ACTION_START
                    putExtra(MediaProjectionService.EXTRA_RESULT_CODE, result.resultCode)
                    putExtra(MediaProjectionService.EXTRA_RESULT_DATA, data)
                }
                ContextCompat.startForegroundService(this, intent)
            } else {
                // Either the user dismissed/cancelled the system capture-consent dialog, or a
                // stop already raced ahead of this result (advanceToStarting() failed because
                // the state machine moved on while the dialog was open) — either way, the
                // service must not be started for a session the state machine no longer
                // recognizes.
                HermesBridge.captureState.stop()
                HermesBridge.captureState.finishStop()
                HermesBridge.send(NativeToWebMessage.ScreenCaptureStopped("user"))
            }
        }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        webView = WebView(this)
        setContentView(webView)
        configureWebView()
        setupBridge()
        captureDictationDraft(intent)
        webView.loadUrl(VoiceAppConfig.VOICE_URL)
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        captureDictationDraft(intent)
    }

    private fun captureDictationDraft(intent: Intent?) {
        if (intent?.action != Intent.ACTION_SEND || intent.type != "text/plain") return
        pendingDictationDraft = intent.getStringExtra(Intent.EXTRA_TEXT)
            ?.trim()?.take(4_000)?.takeIf { it.isNotEmpty() }
    }

    override fun onDestroy() {
        phoneActionGate.invalidateAll()
        stopCaptureIfActive()
        HermesBridge.detach()
        super.onDestroy()
    }

    /** Reached from both normal activity teardown and a dead-renderer recovery. */
    private fun stopCaptureIfActive() {
        if (HermesBridge.captureState.state != CaptureState.IDLE) {
            startService(
                Intent(this, MediaProjectionService::class.java)
                    .setAction(MediaProjectionService.ACTION_STOP),
            )
        }
    }

    @SuppressLint("SetJavaScriptEnabled")
    private fun configureWebView() {
        webView.settings.apply {
            javaScriptEnabled = true
            domStorageEnabled = true
            mediaPlaybackRequiresUserGesture = false
        }

        webView.webViewClient = object : WebViewClient() {
            override fun shouldOverrideUrlLoading(
                view: WebView,
                request: WebResourceRequest,
            ): Boolean {
                val uri = request.url
                if (VoiceAppConfig.originIsAllowed(uri.scheme, uri.host, uri.port)) {
                    return false
                }
                openExternally(uri)
                return true
            }

            override fun onRenderProcessGone(
                view: WebView,
                detail: RenderProcessGoneDetail,
            ): Boolean {
                // The renderer process died (OOM kill, crash, ...): the WebView instance is no
                // longer usable, so any screen capture we still think is running must be torn
                // down (it would otherwise keep running with a broken/blank web UI showing
                // "off" state to nobody). Finishing the activity is the simplest safe recovery
                // — this is a single-purpose kiosk shell, so a fresh relaunch reconstructs a
                // clean WebView rather than trying to salvage the dead one in place.
                stopCaptureIfActive()
                finish()
                return true
            }
        }

        webView.webChromeClient = object : WebChromeClient() {
            override fun onPermissionRequest(request: PermissionRequest) {
                handleWebPermissionRequest(request)
            }
        }
    }

    private fun openExternally(uri: Uri) {
        try {
            startActivity(Intent(Intent.ACTION_VIEW, uri))
        } catch (_: ActivityNotFoundException) {
            // No app can handle it; nothing safe to do beyond staying blocked in-WebView.
        }
    }

    private fun handleWebPermissionRequest(request: PermissionRequest) {
        if (!VoiceAppConfig.originMatches(request.origin.toString())) {
            request.deny()
            return
        }

        val missingAndroidPermissions = request.resources.mapNotNull { resource ->
            androidPermissionFor(resource)
        }.filterNot(::hasPermission)

        if (missingAndroidPermissions.isEmpty()) {
            resolveWebPermissionRequest(request)
        } else {
            pendingWebPermissionRequest = request
            webPermissionLauncher.launch(missingAndroidPermissions.toTypedArray())
        }
    }

    private fun resolveWebPermissionRequest(request: PermissionRequest) {
        val grantable = request.resources.filter { resource ->
            val permission = androidPermissionFor(resource) ?: return@filter false
            hasPermission(permission)
        }
        if (grantable.isNotEmpty()) {
            request.grant(grantable.toTypedArray())
        } else {
            request.deny()
        }
    }

    private fun androidPermissionFor(webResource: String): String? = when (webResource) {
        PermissionRequest.RESOURCE_AUDIO_CAPTURE -> android.Manifest.permission.RECORD_AUDIO
        PermissionRequest.RESOURCE_VIDEO_CAPTURE -> android.Manifest.permission.CAMERA
        else -> null
    }

    private fun hasPermission(permission: String): Boolean =
        ContextCompat.checkSelfPermission(this, permission) == PackageManager.PERMISSION_GRANTED

    private fun setupBridge() {
        if (!WebViewFeature.isFeatureSupported(WebViewFeature.WEB_MESSAGE_LISTENER)) {
            // Plain WebView fallback: the page must not advertise screen-capture capability.
            return
        }
        WebViewCompat.addWebMessageListener(
            webView,
            VoiceAppConfig.BRIDGE_JS_OBJECT_NAME,
            setOf(VoiceAppConfig.ALLOWED_ORIGIN),
        ) { _, message, sourceOrigin, isMainFrame, replyProxy ->
            val parsed = HermesBridge.handleIncoming(message, sourceOrigin, isMainFrame, replyProxy)
                ?: return@addWebMessageListener
            onBridgeMessage(parsed)
        }
    }

    private fun onBridgeMessage(message: WebToNativeMessage) {
        when (message) {
            is WebToNativeMessage.BridgeReady -> {
                HermesBridge.send(NativeToWebMessage.NativeCapabilities)
                pendingDictationDraft?.let {
                    HermesBridge.send(NativeToWebMessage.DictationDraft(it))
                    pendingDictationDraft = null
                }
            }
            is WebToNativeMessage.StartScreenCapture -> handleStartScreenCaptureRequested()
            is WebToNativeMessage.StopScreenCapture -> handleStopScreenCaptureRequested()
            is WebToNativeMessage.CaptureDetailFrame -> handleDetailFrameRequested(message)
            is WebToNativeMessage.BeginPhoneActionSession -> phoneActionGate.begin(message.sessionId)
            is WebToNativeMessage.InvalidatePhoneActionSession -> phoneActionGate.invalidate(message.sessionId)
            is WebToNativeMessage.ExecutePhoneAction -> handlePhoneAction(message)
        }
    }

    private fun handlePhoneAction(message: WebToNativeMessage.ExecutePhoneAction) {
        if (message.expiresAtMs <= System.currentTimeMillis()) {
            HermesBridge.send(NativeToWebMessage.PhoneActionResult(message.requestId, "failed"))
            return
        }
        val ticket = phoneActionGate.stage(message.sessionId, message.requestId)
        if (ticket == null) {
            HermesBridge.send(NativeToWebMessage.PhoneActionResult(message.requestId, "failed"))
            return
        }
        // WebMessage callbacks and lifecycle invalidations share the main queue.
        // Defer the actual side effect briefly, then atomically consume the
        // still-current authorization immediately before touching Android.
        webView.postDelayed({ executePhoneActionIfAuthorized(message, ticket) }, 75L)
    }

    private fun executePhoneActionIfAuthorized(
        message: WebToNativeMessage.ExecutePhoneAction,
        ticket: Long,
    ) {
        if (message.expiresAtMs <= System.currentTimeMillis()) {
            // Retire the staged generation even though no side effect is
            // allowed, otherwise the gate would remain permanently busy.
            phoneActionGate.consume(message.sessionId, message.requestId, ticket)
            HermesBridge.send(NativeToWebMessage.PhoneActionResult(message.requestId, "timeout"))
            return
        }
        if (!phoneActionGate.consume(message.sessionId, message.requestId, ticket)) {
            HermesBridge.send(NativeToWebMessage.PhoneActionResult(message.requestId, "cancelled"))
            return
        }
        val status = try {
            when (message.action) {
                "copy_text" -> {
                    val clipboard = getSystemService(Context.CLIPBOARD_SERVICE) as ClipboardManager
                    clipboard.setPrimaryClip(ClipData.newPlainText("Hermes", message.payload))
                    "executed"
                }
                "open_url" -> launchPhoneIntent(
                    Intent(Intent.ACTION_VIEW, Uri.parse(message.payload)).addCategory(Intent.CATEGORY_BROWSABLE),
                )
                "share_text" -> launchShareIntent(message.payload)
                "open_app" -> launchAllowlistedApp(message.payload)
                else -> "unsupported"
            }
        } catch (_: SecurityException) {
            "failed"
        } catch (_: RuntimeException) {
            "failed"
        }
        HermesBridge.send(NativeToWebMessage.PhoneActionResult(message.requestId, status))
    }

    private fun launchPhoneIntent(intent: Intent): String {
        if (intent.resolveActivity(packageManager) == null) return "unsupported"
        return try {
            startActivity(intent)
            "executed"
        } catch (_: ActivityNotFoundException) {
            "unsupported"
        }
    }

    private fun launchShareIntent(text: String): String {
        val sendIntent = Intent(Intent.ACTION_SEND).apply {
            type = "text/plain"
            putExtra(Intent.EXTRA_TEXT, text)
        }
        // Resolve the underlying narrow ACTION_SEND intent, not the system chooser
        // wrapper (which may exist even when it has no actual share target).
        if (sendIntent.resolveActivity(packageManager) == null) return "unsupported"
        return launchPhoneIntent(Intent.createChooser(sendIntent, "Text teilen"))
    }

    private fun launchAllowlistedApp(target: String): String {
        val intent = when (target) {
            "settings" -> Intent(Settings.ACTION_SETTINGS)
            "wifi" -> Intent(Settings.ACTION_WIFI_SETTINGS)
            "bluetooth" -> Intent(Settings.ACTION_BLUETOOTH_SETTINGS)
            "calendar" -> Intent.makeMainSelectorActivity(
                Intent.ACTION_MAIN,
                Intent.CATEGORY_APP_CALENDAR,
            )
            "alarms" -> Intent(AlarmClock.ACTION_SHOW_ALARMS)
            else -> return "unsupported"
        }
        return launchPhoneIntent(intent)
    }

    private fun handleStartScreenCaptureRequested() {
        if (!HermesBridge.captureState.start()) {
            HermesBridge.send(
                NativeToWebMessage.ScreenCaptureError(
                    code = "busy",
                    message = "A screen capture session is already active.",
                ),
            )
            return
        }

        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU &&
            !hasPermission(android.Manifest.permission.POST_NOTIFICATIONS)
        ) {
            pendingCaptureStartAfterNotificationPrompt = true
            notificationPermissionLauncher.launch(android.Manifest.permission.POST_NOTIFICATIONS)
        } else {
            launchScreenCaptureFlow()
        }
    }

    private fun launchScreenCaptureFlow() {
        val manager = getSystemService(MediaProjectionManager::class.java)
        screenCaptureLauncher.launch(manager.createScreenCaptureIntent())
    }

    private fun handleStopScreenCaptureRequested() {
        if (HermesBridge.captureState.state == CaptureState.IDLE) return
        startService(
            Intent(this, MediaProjectionService::class.java)
                .setAction(MediaProjectionService.ACTION_STOP),
        )
    }

    private fun handleDetailFrameRequested(message: WebToNativeMessage.CaptureDetailFrame) {
        if (HermesBridge.captureState.state != CaptureState.CAPTURING) {
            HermesBridge.send(NativeToWebMessage.DetailScreenFrameUnavailable(message.requestId))
            return
        }
        startService(
            Intent(this, MediaProjectionService::class.java).apply {
                action = MediaProjectionService.ACTION_CAPTURE_DETAIL
                putExtra(MediaProjectionService.EXTRA_REQUEST_ID, message.requestId)
                putExtra(MediaProjectionService.EXTRA_MAX_EDGE, message.maxEdge)
                putExtra(MediaProjectionService.EXTRA_QUALITY, message.quality)
            },
        )
    }
}
