package net.hermes.dictate

import android.text.InputType
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class SensitiveFieldPolicyTest {
    @Test
    fun `password numeric and phone fields are sensitive`() {
        assertTrue(
            SensitiveFieldPolicy.isSensitive(
                InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_PASSWORD,
            ),
        )
        assertTrue(SensitiveFieldPolicy.isSensitive(InputType.TYPE_CLASS_NUMBER))
        assertTrue(SensitiveFieldPolicy.isSensitive(InputType.TYPE_CLASS_PHONE))
        assertTrue(SensitiveFieldPolicy.isSensitive(InputType.TYPE_CLASS_TEXT, passwordNode = true))
    }

    @Test
    fun `ordinary text and email fields remain eligible`() {
        assertFalse(SensitiveFieldPolicy.isSensitive(InputType.TYPE_CLASS_TEXT))
        assertFalse(
            SensitiveFieldPolicy.isSensitive(
                InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_EMAIL_ADDRESS,
            ),
        )
    }

    @Test
    fun `banking package policy is fail closed but narrowly scoped`() {
        assertTrue(BankingAppPolicy.isBlocked("com.n26.android"))
        assertTrue(BankingAppPolicy.isBlocked("de.dkb.portalapp"))
        assertFalse(BankingAppPolicy.isBlocked("com.google.android.gm"))
        assertFalse(BankingAppPolicy.isBlocked(null))
    }
}
