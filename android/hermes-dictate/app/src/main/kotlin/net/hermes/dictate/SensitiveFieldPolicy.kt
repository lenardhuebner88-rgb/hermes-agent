package net.hermes.dictate

import android.text.InputType

object SensitiveFieldPolicy {
    fun isSensitive(inputType: Int, passwordNode: Boolean = false): Boolean {
        if (passwordNode) return true
        val klass = inputType and InputType.TYPE_MASK_CLASS
        val variation = inputType and InputType.TYPE_MASK_VARIATION
        return when (klass) {
            InputType.TYPE_CLASS_PHONE, InputType.TYPE_CLASS_NUMBER -> true
            InputType.TYPE_CLASS_TEXT -> variation in setOf(
                InputType.TYPE_TEXT_VARIATION_PASSWORD,
                InputType.TYPE_TEXT_VARIATION_VISIBLE_PASSWORD,
                InputType.TYPE_TEXT_VARIATION_WEB_PASSWORD,
            )
            else -> false
        }
    }
}

/** Small fail-closed list for apps where a floating dictation affordance is inappropriate. */
object BankingAppPolicy {
    private val blockedPrefixes = listOf(
        "com.n26.",
        "de.dkb.",
        "de.comdirect.",
        "de.commerzbanking.",
        "com.ing.diba.",
        "com.starfinanz.",
        "de.fiduciagad.banking.",
    )

    fun isBlocked(packageName: CharSequence?): Boolean {
        val value = packageName?.toString()?.lowercase().orEmpty()
        return blockedPrefixes.any(value::startsWith)
    }
}
