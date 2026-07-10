package net.hermes.voice

import org.json.JSONException
import org.json.JSONObject

/**
 * Bridge protocol v1 between the Hermes voice web page and the native shell.
 *
 * Pure parsing/serialization logic — no android.* imports so it stays JVM-testable.
 * Unknown/invalid input never throws; callers get null and decide what to do.
 */
const val BRIDGE_PROTOCOL_VERSION = 1

/** Messages the web page sends to native. */
sealed class WebToNativeMessage {
    object BridgeReady : WebToNativeMessage()
    object StartScreenCapture : WebToNativeMessage()
    object StopScreenCapture : WebToNativeMessage()
}

/** Messages native sends to the web page. */
sealed class NativeToWebMessage {
    object NativeCapabilities : NativeToWebMessage()
    object ScreenCaptureStarted : NativeToWebMessage()
    data class ScreenFrame(val base64Jpeg: String) : NativeToWebMessage()
    data class ScreenCaptureStopped(val reason: String) : NativeToWebMessage()
    data class ScreenCaptureError(val code: String, val message: String) : NativeToWebMessage()
}

object BridgeProtocol {

    private const val KEY_VERSION = "v"
    private const val KEY_TYPE = "type"

    private const val TYPE_BRIDGE_READY = "bridge_ready"
    private const val TYPE_START_SCREEN_CAPTURE = "start_screen_capture"
    private const val TYPE_STOP_SCREEN_CAPTURE = "stop_screen_capture"

    private const val TYPE_NATIVE_CAPABILITIES = "native_capabilities"
    private const val TYPE_SCREEN_CAPTURE_STARTED = "screen_capture_started"
    private const val TYPE_SCREEN_FRAME = "screen_frame"
    private const val TYPE_SCREEN_CAPTURE_STOPPED = "screen_capture_stopped"
    private const val TYPE_SCREEN_CAPTURE_ERROR = "screen_capture_error"

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
            else -> null
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
            }
            is NativeToWebMessage.ScreenCaptureStarted -> {
                json.put(KEY_TYPE, TYPE_SCREEN_CAPTURE_STARTED)
            }
            is NativeToWebMessage.ScreenFrame -> {
                json.put(KEY_TYPE, TYPE_SCREEN_FRAME)
                json.put("data", message.base64Jpeg)
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
        }
        return json.toString()
    }
}
