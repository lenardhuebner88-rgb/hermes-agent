package net.hermes.dictate

/** A bounded, deterministic replacement rule entered as `spoken => written`. */
data class PersonalizationRule(val spoken: String, val written: String)

/**
 * Local personal dictionary for names, acronyms and project terms. Rules never leave the phone.
 * Matching is case-insensitive, Unicode-aware and phrase-bounded so `Piet` does not rewrite a
 * substring inside another word. Longer phrases win over shorter overlapping entries.
 */
object TextPersonalizer {
    private const val MAX_RULES = 250
    private const val MAX_TRIGGER_CHARS = 120
    private const val MAX_REPLACEMENT_CHARS = 2_000

    fun parse(raw: String): List<PersonalizationRule> = raw.lineSequence()
        .map(String::trim)
        .filter { it.isNotEmpty() && !it.startsWith("#") }
        .mapNotNull { line ->
            val separator = line.indexOf("=>")
            if (separator <= 0) return@mapNotNull null
            val spoken = line.substring(0, separator).trim()
            val written = line.substring(separator + 2).trim()
            if (spoken.isEmpty() || written.isEmpty() ||
                spoken.length > MAX_TRIGGER_CHARS || written.length > MAX_REPLACEMENT_CHARS
            ) {
                null
            } else {
                PersonalizationRule(spoken, written)
            }
        }
        .distinctBy { it.spoken.lowercase() }
        .sortedByDescending { it.spoken.length }
        .take(MAX_RULES)
        .toList()

    fun applyDictionary(text: String, rawRules: String): String {
        val rules = parse(rawRules)
        if (rules.isEmpty()) return text
        val byTrigger = rules.associateBy { it.spoken.lowercase() }
        val alternatives = rules.joinToString("|") { Regex.escape(it.spoken) }
        val phrase = Regex(
            "(?iu)(?<![\\p{L}\\p{N}_])(?:$alternatives)(?![\\p{L}\\p{N}_])",
        )
        // One pass prevents a shorter rule from rewriting text produced by a longer rule.
        return phrase.replace(text) { match ->
            byTrigger.getValue(match.value.lowercase()).written
        }
    }

    /**
     * Expands a snippet only when the whole finished segment equals its spoken cue. This mirrors
     * Flow's voice-triggered snippets without surprising the user by expanding an ordinary phrase
     * in the middle of a longer sentence. `\\n` in a stored replacement becomes a real line break.
     */
    fun expandSnippet(text: String, rawRules: String): String {
        val normalized = text.trim().trimEnd('.', ',', '!', '?', ':', ';').trim()
        val rule = parse(rawRules).firstOrNull { it.spoken.equals(normalized, ignoreCase = true) }
            ?: return text
        return rule.written.replace("\\n", "\n")
    }
}

/** Shared text path for IME and accessibility overlay. */
class DictationTextPipeline(
    private val dictionaryRules: () -> String,
    private val snippetRules: () -> String = { "" },
    private val languageTag: () -> String? = { null },
    private val localRefine: () -> Boolean = { true },
) {
    fun process(raw: String): DictationTransform {
        val transformed = if (localRefine()) {
            LocalFlowRefiner.transform(raw, languageTag())
        } else {
            DictationTransform.Text(raw)
        }
        if (transformed !is DictationTransform.Text) return transformed
        val personalized = TextPersonalizer.applyDictionary(
            PunctuationMapper.map(transformed.value),
            dictionaryRules(),
        )
        return DictationTransform.Text(TextPersonalizer.expandSnippet(personalized, snippetRules()))
    }
}
