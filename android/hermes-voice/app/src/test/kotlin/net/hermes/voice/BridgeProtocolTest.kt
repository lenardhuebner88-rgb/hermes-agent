package net.hermes.voice

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class BridgeProtocolTest {

    @Test
    fun `parses valid bridge_ready message`() {
        val result = BridgeProtocol.parseWebToNative("""{"v":1,"type":"bridge_ready"}""")
        assertTrue(result is WebToNativeMessage.BridgeReady)
    }

    @Test
    fun `parses valid start_screen_capture message`() {
        val result = BridgeProtocol.parseWebToNative("""{"v":1,"type":"start_screen_capture"}""")
        assertTrue(result is WebToNativeMessage.StartScreenCapture)
    }

    @Test
    fun `parses valid stop_screen_capture message`() {
        val result = BridgeProtocol.parseWebToNative("""{"v":1,"type":"stop_screen_capture"}""")
        assertTrue(result is WebToNativeMessage.StopScreenCapture)
    }

    @Test
    fun `parses bounded correlated detail capture request`() {
        val id = "a".repeat(32)
        val result = BridgeProtocol.parseWebToNative(
            """{"v":1,"type":"capture_detail_frame","request_id":"$id","max_edge":4096,"quality":1.0}""",
        ) as WebToNativeMessage.CaptureDetailFrame
        assertEquals(id, result.requestId)
        assertEquals(2048, result.maxEdge)
        assertEquals(0.92, result.quality, 0.001)
        assertNull(
            BridgeProtocol.parseWebToNative(
                """{"v":1,"type":"capture_detail_frame","request_id":"stale"}""",
            ),
        )
    }

    @Test
    fun `rejects malformed json`() {
        assertNull(BridgeProtocol.parseWebToNative("not json at all"))
        assertNull(BridgeProtocol.parseWebToNative("""{"v":1,"type":"bridge_ready""""))
        assertNull(BridgeProtocol.parseWebToNative(""))
    }

    @Test
    fun `rejects missing version`() {
        assertNull(BridgeProtocol.parseWebToNative("""{"type":"bridge_ready"}"""))
    }

    @Test
    fun `rejects wrong version`() {
        assertNull(BridgeProtocol.parseWebToNative("""{"v":2,"type":"bridge_ready"}"""))
        assertNull(BridgeProtocol.parseWebToNative("""{"v":"not-a-number","type":"bridge_ready"}"""))
    }

    @Test
    fun `rejects unknown type`() {
        assertNull(BridgeProtocol.parseWebToNative("""{"v":1,"type":"self_destruct"}"""))
    }

    @Test
    fun `rejects missing type`() {
        assertNull(BridgeProtocol.parseWebToNative("""{"v":1}"""))
    }

    // The 4 tests below assert on parsed fields rather than the raw wire string: JSON key
    // order is not part of the protocol contract (the web side just JSON.parse()s), and the
    // test-only org.json:json dependency's JSONObject does not preserve insertion order the
    // way the real Android-platform org.json.JSONObject does — an exact-string assertion here
    // would test that Map-implementation detail, not the actual serialized contract.

    @Test
    fun `serializes native_capabilities`() {
        val json = JSONObject(BridgeProtocol.serializeNativeToWeb(NativeToWebMessage.NativeCapabilities))
        assertEquals(1, json.getInt("v"))
        assertEquals("native_capabilities", json.getString("type"))
        assertTrue(json.getBoolean("screen_capture"))
    }

    @Test
    fun `serializes screen_capture_stopped with reason`() {
        val json = JSONObject(
            BridgeProtocol.serializeNativeToWeb(NativeToWebMessage.ScreenCaptureStopped("system")),
        )
        assertEquals(1, json.getInt("v"))
        assertEquals("screen_capture_stopped", json.getString("type"))
        assertEquals("system", json.getString("reason"))
    }

    @Test
    fun `serializes screen_capture_error with code and message`() {
        val json = JSONObject(
            BridgeProtocol.serializeNativeToWeb(
                NativeToWebMessage.ScreenCaptureError("busy", "already active"),
            ),
        )
        assertEquals(1, json.getInt("v"))
        assertEquals("screen_capture_error", json.getString("type"))
        assertEquals("busy", json.getString("code"))
        assertEquals("already active", json.getString("message"))
    }

    @Test
    fun `serializes screen_frame with base64 payload`() {
        val json = JSONObject(
            BridgeProtocol.serializeNativeToWeb(NativeToWebMessage.ScreenFrame("Zm9v")),
        )
        assertEquals(1, json.getInt("v"))
        assertEquals("screen_frame", json.getString("type"))
        assertEquals("Zm9v", json.getString("data"))
    }

    @Test
    fun `serializes correlated detail screen frame`() {
        val id = "b".repeat(32)
        val json = JSONObject(
            BridgeProtocol.serializeNativeToWeb(NativeToWebMessage.DetailScreenFrame(id, "Zm9v")),
        )
        assertEquals("detail_screen_frame", json.getString("type"))
        assertEquals(id, json.getString("request_id"))
        assertEquals("Zm9v", json.getString("data"))
    }

    @Test
    fun `round trips a real device payload shape`() {
        // Fixture harvested from an actual bridge_ready postMessage() call site in the web app.
        val raw = """{"v":1,"type":"bridge_ready"}"""
        val parsed = BridgeProtocol.parseWebToNative(raw)
        assertTrue(parsed is WebToNativeMessage.BridgeReady)
    }
}
