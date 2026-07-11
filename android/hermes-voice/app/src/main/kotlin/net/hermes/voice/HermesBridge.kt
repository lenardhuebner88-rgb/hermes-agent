package net.hermes.voice

import android.net.Uri
import android.os.Handler
import android.os.Looper
import androidx.webkit.JavaScriptReplyProxy
import androidx.webkit.WebMessageCompat

/**
 * Origin-scoped wrapper around the addWebMessageListener reply channel, plus the shared
 * capture-session state machine. Same-process singleton so both the Activity (which owns the
 * WebView / reply proxy) and the MediaProjectionService (which produces frames) can talk to the
 * web page through one place. Never logs message contents.
 */
object HermesBridge {

    val captureState = CaptureStateMachine()

    private val replyChannel = BridgeGenerationGate<JavaScriptReplyProxy>()

    private val mainHandler by lazy { Handler(Looper.getMainLooper()) }

    fun isAlive(): Boolean = replyChannel.snapshot() != null

    fun detach() {
        replyChannel.detach()
    }

    /**
     * Validates an incoming addWebMessageListener callback and, if it passes, parses the
     * message and remembers the reply proxy for subsequent native -> web sends.
     *
     * Returns null for: non-main-frame senders, origin mismatches, or protocol-invalid
     * payloads — callers must not act on a null result.
     */
    fun handleIncoming(
        message: WebMessageCompat,
        sourceOrigin: Uri,
        isMainFrame: Boolean,
        proxy: JavaScriptReplyProxy,
    ): WebToNativeMessage? {
        if (!isMainFrame) return null
        if (!VoiceAppConfig.originMatches(sourceOrigin.toString())) return null

        val raw = message.data ?: return null
        val parsed = BridgeProtocol.parseWebToNative(raw) ?: return null
        replyChannel.attach(proxy)
        return parsed
    }

    /** Sends a native -> web message through the captured reply proxy, if any is attached. */
    fun send(message: NativeToWebMessage) {
        val (proxy, generation) = replyChannel.snapshot() ?: return
        val payload = BridgeProtocol.serializeNativeToWeb(message)
        val deliver: () -> Unit = {
            if (replyChannel.isCurrent(proxy, generation)) {
                proxy.postMessage(payload)
            }
            Unit
        }
        if (Looper.myLooper() == Looper.getMainLooper()) {
            deliver()
        } else {
            // JavaScriptReplyProxy is @UiThread. Capture frames and projection
            // callbacks originate on the service HandlerThread, so every
            // native -> web delivery must cross this single main-thread gate.
            mainHandler.post(deliver)
        }
    }
}
