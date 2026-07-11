package net.hermes.voice

import org.json.JSONException
import org.json.JSONObject
import java.net.URI

/**
 * Bridge protocol v1 between the Hermes voice web page and the native shell.
 *
 * Pure parsing/serialization logic — no android.* imports so it stays JVM-testable.
 * Unknown/invalid input never throws; callers get null and decide what to do.
 */
const val BRIDGE_PROTOCOL_VERSION = 1

/** One Activity/WebView lifetime, correlation ids only; payloads are never retained. */
class PhoneActionReplayGuard {
    private val handled = mutableSetOf<String>()
    @Synchronized fun accept(requestId: String): Boolean = handled.add(requestId)
}

/** Messages the web page sends to native. */
sealed class WebToNativeMessage {
    object BridgeReady : WebToNativeMessage()
    object StartScreenCapture : WebToNativeMessage()
    object StopScreenCapture : WebToNativeMessage()
    data class CaptureDetailFrame(
        val requestId: String,
        val maxEdge: Int,
        val quality: Double,
    ) : WebToNativeMessage()
    data class ExecutePhoneAction(
        val requestId: String,
        val action: String,
        val payload: String,
        val expiresAtMs: Long,
    ) : WebToNativeMessage()
}

/** Messages native sends to the web page. */
sealed class NativeToWebMessage {
    object NativeCapabilities : NativeToWebMessage()
    object ScreenCaptureStarted : NativeToWebMessage()
    data class ScreenFrame(val base64Jpeg: String) : NativeToWebMessage()
    data class DetailScreenFrame(val requestId: String, val base64Jpeg: String) : NativeToWebMessage()
    data class DetailScreenFrameUnavailable(val requestId: String) : NativeToWebMessage()
    data class ScreenCaptureStopped(val reason: String) : NativeToWebMessage()
    data class ScreenCaptureError(val code: String, val message: String) : NativeToWebMessage()
    data class PhoneActionResult(val requestId: String, val status: String) : NativeToWebMessage()
}

object BridgeProtocol {

    private const val KEY_VERSION = "v"
    private const val KEY_TYPE = "type"

    private const val TYPE_BRIDGE_READY = "bridge_ready"
    private const val TYPE_START_SCREEN_CAPTURE = "start_screen_capture"
    private const val TYPE_STOP_SCREEN_CAPTURE = "stop_screen_capture"
    private const val TYPE_CAPTURE_DETAIL_FRAME = "capture_detail_frame"
    private const val TYPE_EXECUTE_PHONE_ACTION = "execute_phone_action"

    private const val TYPE_NATIVE_CAPABILITIES = "native_capabilities"
    private const val TYPE_SCREEN_CAPTURE_STARTED = "screen_capture_started"
    private const val TYPE_SCREEN_FRAME = "screen_frame"
    private const val TYPE_DETAIL_SCREEN_FRAME = "detail_screen_frame"
    private const val TYPE_DETAIL_SCREEN_FRAME_UNAVAILABLE = "detail_screen_frame_unavailable"
    private const val TYPE_SCREEN_CAPTURE_STOPPED = "screen_capture_stopped"
    private const val TYPE_SCREEN_CAPTURE_ERROR = "screen_capture_error"
    private const val TYPE_PHONE_ACTION_RESULT = "phone_action_result"
    private val REQUEST_ID = Regex("^[A-Za-z0-9_-]{32}$")

    /**
     * Parses a raw JSON string received from the web page.
     * Returns null on: malformed JSON, missing/wrong version, missing/unknown type.
     */
    fun parseWebToNative(raw: String): WebToNativeMessage? {
        val json = try {
            JSONObject(raw)
        } catch (_: JSONException) {
            return null
        }

        if (!json.has(KEY_VERSION) || json.optInt(KEY_VERSION, -1) != BRIDGE_PROTOCOL_VERSION) {
            return null
        }

        val type = json.optString(KEY_TYPE, "")
        return when (type) {
            TYPE_BRIDGE_READY -> WebToNativeMessage.BridgeReady
            TYPE_START_SCREEN_CAPTURE -> WebToNativeMessage.StartScreenCapture
            TYPE_STOP_SCREEN_CAPTURE -> WebToNativeMessage.StopScreenCapture
            TYPE_CAPTURE_DETAIL_FRAME -> {
                val requestId = json.optString("request_id", "")
                if (!requestId.matches(Regex("^[a-f0-9]{32}$"))) return null
                WebToNativeMessage.CaptureDetailFrame(
                    requestId = requestId,
                    maxEdge = json.optInt("max_edge", 2048).coerceIn(1024, 2048),
                    quality = json.optDouble("quality", 0.9).coerceIn(0.65, 0.92),
                )
            }
            TYPE_EXECUTE_PHONE_ACTION -> parsePhoneAction(json)
            else -> null
        }
    }

    private fun parsePhoneAction(json: JSONObject): WebToNativeMessage.ExecutePhoneAction? {
        val requestId = json.optString("request_id", "")
        if (!REQUEST_ID.matches(requestId)) return null
        val expiresAtMs = json.optLong("expires_at_ms", -1)
        val now = System.currentTimeMillis()
        if (expiresAtMs <= now || expiresAtMs > now + 60_000) return null
        return when (val action = json.optString("action", "")) {
            "copy_text", "share_text" -> {
                val text = json.opt("text") as? String ?: return null
                val limit = if (action == "copy_text") 4096 else 8192
                if (text.isBlank() || text.length > limit || hasUnsafeControl(text)) return null
                WebToNativeMessage.ExecutePhoneAction(requestId, action, text, expiresAtMs)
            }
            "open_url" -> {
                val url = json.opt("url") as? String ?: return null
                if (!isAllowedHttpsUrl(url)) return null
                WebToNativeMessage.ExecutePhoneAction(requestId, action, url, expiresAtMs)
            }
            else -> null
        }
    }

    private fun hasUnsafeControl(text: String): Boolean =
        text.any { it.code == 0 || it.code < 32 && it != '\t' && it != '\n' && it != '\r' }

    internal fun isAllowedHttpsUrl(raw: String): Boolean {
        if (raw.isBlank() || raw.length > 2048 || raw.any(Char::isWhitespace)) return false
        return try {
            val uri = URI(raw)
            uri.scheme == "https" && !uri.host.isNullOrBlank() && uri.userInfo == null
        } catch (_: Exception) {
            false
        }
    }

    /** Serializes a native → web message to its JSON string wire form. */
    fun serializeNativeToWeb(message: NativeToWebMessage): String {
        val json = JSONObject()
        json.put(KEY_VERSION, BRIDGE_PROTOCOL_VERSION)
        when (message) {
            is NativeToWebMessage.NativeCapabilities -> {
                json.put(KEY_TYPE, TYPE_NATIVE_CAPABILITIES)
                json.put("screen_capture", true)
                json.put("phone_action", true)
            }
            is NativeToWebMessage.ScreenCaptureStarted -> {
                json.put(KEY_TYPE, TYPE_SCREEN_CAPTURE_STARTED)
            }
            is NativeToWebMessage.ScreenFrame -> {
                json.put(KEY_TYPE, TYPE_SCREEN_FRAME)
                json.put("data", message.base64Jpeg)
            }
            is NativeToWebMessage.DetailScreenFrame -> {
                json.put(KEY_TYPE, TYPE_DETAIL_SCREEN_FRAME)
                json.put("request_id", message.requestId)
                json.put("data", message.base64Jpeg)
            }
            is NativeToWebMessage.DetailScreenFrameUnavailable -> {
                json.put(KEY_TYPE, TYPE_DETAIL_SCREEN_FRAME_UNAVAILABLE)
                json.put("request_id", message.requestId)
            }
            is NativeToWebMessage.ScreenCaptureStopped -> {
                json.put(KEY_TYPE, TYPE_SCREEN_CAPTURE_STOPPED)
                json.put("reason", message.reason)
            }
            is NativeToWebMessage.ScreenCaptureError -> {
                json.put(KEY_TYPE, TYPE_SCREEN_CAPTURE_ERROR)
                json.put("code", message.code)
                json.put("message", message.message)
            }
            is NativeToWebMessage.PhoneActionResult -> {
                json.put(KEY_TYPE, TYPE_PHONE_ACTION_RESULT)
                json.put("request_id", message.requestId)
                json.put("status", message.status)
            }
        }
        return json.toString()
    }
}
