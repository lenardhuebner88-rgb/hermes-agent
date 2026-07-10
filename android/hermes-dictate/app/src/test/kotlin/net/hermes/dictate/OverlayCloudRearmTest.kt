package net.hermes.dictate

import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class OverlayCloudRearmTest {

    @Test
    fun `rearms when idle, on-device, preferred, enabled and logged in`() {
        assertTrue(
            OverlayCloudRearm.shouldRearm(
                DictationController.Phase.IDLE, Mode.ON_DEVICE,
                cloudPreferred = true, cloudEnabled = true, loggedIn = true,
            ),
        )
    }

    @Test
    fun `does not rearm mid-dictation`() {
        assertFalse(
            OverlayCloudRearm.shouldRearm(
                DictationController.Phase.LISTENING, Mode.ON_DEVICE,
                cloudPreferred = true, cloudEnabled = true, loggedIn = true,
            ),
        )
    }

    @Test
    fun `does not rearm when already cloud`() {
        assertFalse(
            OverlayCloudRearm.shouldRearm(
                DictationController.Phase.IDLE, Mode.CLOUD,
                cloudPreferred = true, cloudEnabled = true, loggedIn = true,
            ),
        )
    }

    @Test
    fun `does not rearm when preference is off`() {
        assertFalse(
            OverlayCloudRearm.shouldRearm(
                DictationController.Phase.IDLE, Mode.ON_DEVICE,
                cloudPreferred = false, cloudEnabled = true, loggedIn = true,
            ),
        )
    }

    @Test
    fun `does not rearm when the master cloud switch is off`() {
        assertFalse(
            OverlayCloudRearm.shouldRearm(
                DictationController.Phase.IDLE, Mode.ON_DEVICE,
                cloudPreferred = true, cloudEnabled = false, loggedIn = true,
            ),
        )
    }

    @Test
    fun `does not rearm when not logged in`() {
        assertFalse(
            OverlayCloudRearm.shouldRearm(
                DictationController.Phase.IDLE, Mode.ON_DEVICE,
                cloudPreferred = true, cloudEnabled = true, loggedIn = false,
            ),
        )
    }
}
