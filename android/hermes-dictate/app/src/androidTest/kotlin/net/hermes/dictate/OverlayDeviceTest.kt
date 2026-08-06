package net.hermes.dictate

import android.content.Intent
import android.os.Build
import androidx.test.espresso.Espresso.onView
import androidx.test.espresso.assertion.ViewAssertions.matches
import androidx.test.espresso.matcher.ViewMatchers.withId
import androidx.test.espresso.matcher.ViewMatchers.withText
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import androidx.test.uiautomator.By
import androidx.test.uiautomator.UiDevice
import androidx.test.uiautomator.Until
import java.io.FileInputStream
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
class OverlayDeviceTest {
    private val instrumentation = InstrumentationRegistry.getInstrumentation()
    private val targetContext = instrumentation.targetContext
    private val device = UiDevice.getInstance(instrumentation)

    @Before
    fun prepareEmulatorState() {
        assertTrue(
            "device tests are emulator-only",
            Build.FINGERPRINT.contains("generic", ignoreCase = true) ||
                Build.MODEL.contains("sdk", ignoreCase = true),
        )
        shell("pm grant $APP_ID android.permission.RECORD_AUDIO")
        shell("ime enable $APP_ID/.DictateInputMethodService")
        shell("ime set $APP_ID/.DictateInputMethodService")
        shell("settings put secure enabled_accessibility_services $A11Y_ID")
        shell("settings put secure accessibility_enabled 1")
    }

    @Test
    fun preparedDeviceExposesReadableSetupState() {
        // This is the only test that ever renders the settings screen at all, so it is also the
        // only proof that the Compose rewrite starts without throwing. Keep it anchored on
        // testTags, not on German display text: the previous version asserted "erteilt ✓" /
        // "ausgewählt ✓" / "aktiv ✓" and broke the moment the four setup rows were replaced by
        // one readiness card — a wording change should not read as a broken app.
        open(SettingsActivity::class.java)
        assertNotNull(device.wait(Until.findObject(By.text("Hermes Diktat")), 5_000))

        // prepareEmulatorState() grants all four steps, so the card must collapse to its
        // single "ready" line. Asserting the collapse is the point: it is the behaviour that
        // replaced the four per-step rows.
        assertNotNull(
            "readiness card did not collapse although all four steps are granted",
            device.wait(
                // Single-arg By.res: testTagsAsResourceId writes the tag RAW into
                // viewIdResourceName, with no "pkg:id/" prefix — the two-arg overload
                // builds exactly that prefix and can never match.
                Until.findObject(By.res(SettingsTestTags.READINESS_ALL_DONE)),
                5_000,
            ),
        )
        assertNull(
            "granular step rows must be gone once everything is done",
            device.findObject(By.res(SettingsTestTags.READINESS_PROGRESS)),
        )
    }

    @Test
    fun exactPillLayoutShowsGrowingPartialsLevelAndExplicitStates() {
        open(OverlayHarnessActivity::class.java)
        val harness = waitForHarness()

        drive(harness, UiStatus.Listening, partial = "Hermes", level = 28)
        val first = snapshot(harness)
        assertEquals("Hermes", first.text)
        assertEquals(28, first.level)
        assertTrue(first.cancelEnabled)
        assertTrue(first.confirmEnabled)
        assertEquals(targetContext.getString(R.string.overlay_cancel_desc), first.cancelDescription)
        assertEquals(targetContext.getString(R.string.overlay_stop_desc), first.confirmDescription)
        onView(withId(R.id.pill_text)).check(matches(withText("Hermes")))

        drive(harness, UiStatus.Listening, partial = "Hermes erstellt eine PlanSpec", level = 76)
        val growing = snapshot(harness)
        assertTrue(growing.text.startsWith(first.text))
        assertTrue(growing.text.length > first.text.length)
        assertEquals(76, growing.level)
        onView(withId(R.id.pill_text)).check(matches(withText("Hermes erstellt eine PlanSpec")))

        drive(harness, UiStatus.Processing)
        val processing = snapshot(harness)
        assertEquals(targetContext.getString(R.string.status_processing), processing.status)
        assertFalse(processing.confirmEnabled)

        drive(harness, UiStatus.Done, partial = "Hermes erstellt eine PlanSpec")
        val done = snapshot(harness)
        assertEquals(targetContext.getString(R.string.status_done), done.status)
        assertFalse(done.cancelEnabled)
        // The confirm slot only ever shows the plain "done" checkmark now and stays inactive —
        // no status word, no echoed text (done.messageVisible is false).
        assertFalse(done.confirmEnabled)
        assertEquals(targetContext.getString(R.string.overlay_done_desc), done.confirmDescription)
        assertFalse(done.messageVisible)
        // Undo is not lost: it moved to its own icon-only button, visible during the undo window.
        assertTrue(done.undoVisible)
        assertEquals(targetContext.getString(R.string.overlay_undo_desc), done.undoDescription)
        // The wave replaces the visible status word, so it must announce the state itself.
        assertEquals(targetContext.getString(R.string.status_listening), first.waveDescription)

        drive(
            harness,
            UiStatus.Failed(ErrorKind.CLOUD_NETWORK),
            retryAvailable = true,
        )
        val retry = snapshot(harness)
        assertTrue(retry.cancelEnabled)
        assertTrue(retry.confirmEnabled)
        assertEquals(targetContext.getString(R.string.retry_cloud), retry.confirmDescription)

        val density = targetContext.resources.displayMetrics.density
        assertTrue(growing.width >= (48 * density).toInt())
        assertTrue(growing.height >= (48 * density).toInt())
    }

    private fun open(activity: Class<*>) {
        targetContext.startActivity(
            Intent(targetContext, activity)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TOP),
        )
        device.waitForIdle()
    }

    private fun waitForHarness(): OverlayHarnessActivity {
        val deadline = System.currentTimeMillis() + 5_000
        while (System.currentTimeMillis() < deadline) {
            OverlayHarnessActivity.instance?.let { return it }
            Thread.sleep(50)
        }
        throw AssertionError("debug overlay harness did not start")
    }

    private fun drive(
        harness: OverlayHarnessActivity,
        status: UiStatus,
        partial: String? = null,
        level: Int = 0,
        retryAvailable: Boolean = false,
    ) {
        instrumentation.runOnMainSync { harness.drive(status, partial, level, retryAvailable) }
        device.waitForIdle()
    }

    private fun snapshot(harness: OverlayHarnessActivity): OverlayHarnessActivity.Snapshot {
        var value: OverlayHarnessActivity.Snapshot? = null
        instrumentation.runOnMainSync { value = harness.snapshot() }
        return checkNotNull(value)
    }

    private fun shell(command: String): String {
        val descriptor = instrumentation.uiAutomation.executeShellCommand(command)
        return descriptor.use {
            FileInputStream(it.fileDescriptor).bufferedReader().use { reader -> reader.readText() }
        }
    }

    companion object {
        private const val APP_ID = "net.hermes.dictate"
        private const val A11Y_ID = "$APP_ID/$APP_ID.DictateOverlayService"
    }
}
