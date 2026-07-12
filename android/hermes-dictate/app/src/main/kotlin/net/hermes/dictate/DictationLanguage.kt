package net.hermes.dictate

import java.util.Locale

enum class LanguageMode { SYSTEM, GERMAN, ENGLISH, AUTO }

object DictationLanguage {
    fun recognitionTag(mode: LanguageMode, systemTag: String = Locale.getDefault().toLanguageTag()): String =
        when (mode) {
            LanguageMode.SYSTEM -> validSystemTag(systemTag)
            LanguageMode.GERMAN -> "de-DE"
            LanguageMode.ENGLISH -> "en-US"
            LanguageMode.AUTO -> ""
        }

    fun cloudHint(mode: LanguageMode, systemTag: String = Locale.getDefault().toLanguageTag()): String? =
        recognitionTag(mode, systemTag).takeIf { it.isNotBlank() }?.substringBefore('-')?.lowercase()

    private fun validSystemTag(tag: String): String {
        val locale = Locale.forLanguageTag(tag)
        return if (locale.language.matches(Regex("[a-zA-Z]{2,3}"))) locale.toLanguageTag() else "de-DE"
    }
}
