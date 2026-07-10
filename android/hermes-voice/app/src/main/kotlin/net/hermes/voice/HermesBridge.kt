package net.hermes.voice

import android.net.Uri
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

    @Volatile
    private var replyProxy: JavaScriptReplyProxy? = null

    fun isAlive(): Boolean = replyProxy != null

    fun detach() {
        replyProxy = null
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

        replyProxy = proxy
        val raw = message.data ?: return null
        return BridgeProtocol.parseWebToNative(raw)
    }

    /** Sends a native -> web message through the captured reply proxy, if any is attached. */
    fun send(message: NativeToWebMessage) {
        val proxy = replyProxy ?: return
        proxy.postMessage(BridgeProtocol.serializeNativeToWeb(message))
    }
}
