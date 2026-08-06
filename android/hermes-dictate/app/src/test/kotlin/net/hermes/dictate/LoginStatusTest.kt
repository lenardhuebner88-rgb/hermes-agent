package net.hermes.dictate

import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Audit defect 2: the old screen could show "angemeldet ✓" and an active "ANMELDEN" button at
 * once, because `refreshCloudRows()` mutated only the status text and never touched the button.
 * [LoginStatus.showsPrimaryLoginButton] is the single rule the rebuilt login row renders from —
 * these tests are the guard against that state recurring.
 */
class LoginStatusTest {

    @Test
    fun `signed in shows no primary login button`() {
        assertFalse(LoginStatus.SignedIn.showsPrimaryLoginButton())
    }

    @Test
    fun `signed out shows exactly one primary login button`() {
        assertTrue(LoginStatus.SignedOut.showsPrimaryLoginButton())
    }

    @Test
    fun `unreachable server shows a primary login button too`() {
        // SessionProbe.check() == null (server unreachable) must not be mistaken for signed in.
        assertTrue(LoginStatus.Unreachable.showsPrimaryLoginButton())
    }

    @Test
    fun `still checking shows a primary login button until the probe resolves`() {
        assertTrue(LoginStatus.Checking.showsPrimaryLoginButton())
    }

    @Test
    fun `fromProbe maps SessionProbe's tri-state contract`() {
        assertTrue(LoginStatus.fromProbe(true) == LoginStatus.SignedIn)
        assertTrue(LoginStatus.fromProbe(false) == LoginStatus.SignedOut)
        assertTrue(LoginStatus.fromProbe(null) == LoginStatus.Unreachable)
    }
}
