package net.hermes.dictate

/**
 * Maps spoken punctuation ("Punkt", "comma", "neue Zeile", ...) inside a recognizer transcript
 * to the actual marks. v0 scope is deliberately "Basis-Satzzeichen" per PlanSpec: sentence
 * marks, line/paragraph breaks and hyphen — no voice commands, no formatting (v1).
 *
 * Known v0 tradeoff (same as every dictation keyboard): a genuinely spoken word that equals a
 * punctuation word ("der Punkt ist wichtig") is converted too.
 */
object PunctuationMapper {

    private enum class Kind {
        /** Attaches to the preceding text without a space: `. , ? ! : ;` */
        ATTACH,

        /** Joins the surrounding words without spaces: `-` */
        HYPHEN,

        /** Line/paragraph break; the following word starts the new line. */
        BREAK,
    }

    private class Rule(phrase: String, val mark: String, val kind: Kind) {
        val words: List<String> = phrase.split(' ')
    }

    // Ordered longest-phrase-first so multi-word rules ("neuer absatz", "question mark") win
    // before any single-word rule could consume their first word.
    private val rules: List<Rule> = listOf(
        Rule("neuer absatz", "\n\n", Kind.BREAK),
        Rule("neue zeile", "\n", Kind.BREAK),
        Rule("new paragraph", "\n\n", Kind.BREAK),
        Rule("new line", "\n", Kind.BREAK),
        Rule("question mark", "?", Kind.ATTACH),
        Rule("exclamation mark", "!", Kind.ATTACH),
        Rule("exclamation point", "!", Kind.ATTACH),
        Rule("full stop", ".", Kind.ATTACH),
        Rule("punkt", ".", Kind.ATTACH),
        Rule("komma", ",", Kind.ATTACH),
        Rule("fragezeichen", "?", Kind.ATTACH),
        Rule("ausrufezeichen", "!", Kind.ATTACH),
        Rule("doppelpunkt", ":", Kind.ATTACH),
        Rule("semikolon", ";", Kind.ATTACH),
        Rule("strichpunkt", ";", Kind.ATTACH),
        Rule("bindestrich", "-", Kind.HYPHEN),
        Rule("period", ".", Kind.ATTACH),
        Rule("comma", ",", Kind.ATTACH),
        Rule("colon", ":", Kind.ATTACH),
        Rule("semicolon", ";", Kind.ATTACH),
        Rule("hyphen", "-", Kind.HYPHEN),
    ).sortedByDescending { it.words.size }

    private val sentenceEnders = setOf('.', '!', '?')

    // "Punkt", "Komma" and "Bindestrich" are also ordinary German nouns. After a determiner —
    // directly or across one inflected adjective — the noun reading is the only sensible one,
    // so "wir bringen es auf den Punkt" keeps its last word.
    private val nounCapable = setOf("punkt", "komma", "bindestrich", "period", "comma", "hyphen")
    private val determiners = setOf(
        "der", "die", "das", "den", "dem", "des", "ein", "eine", "einen", "einem", "eines",
        "kein", "keine", "keinen", "keinem", "mein", "meine", "meinen", "dein", "deine",
        "sein", "seine", "ihr", "ihre", "unser", "unsere", "euer", "eure", "jeder", "jede",
        "jeden", "dieser", "diese", "dieses", "diesen", "jener", "jene", "am", "zum", "zur",
        "beim", "vom", "im", "ans", "aufs",
    )
    // Deliberately without "-en": that ending belongs to infinitives and plurals far more often
    // than to a pre-nominal adjective, and "das machen Komma wenn du zustimmst" must still get
    // its comma.
    private val inflectedAdjective = Regex("(?iu)^[\\p{L}]{3,}(?:er|es|em|e)$")

    private fun isSpokenNoun(words: List<String>, index: Int, rule: Rule): Boolean {
        if (rule.words.size != 1 || rule.words[0] !in nounCapable) return false
        if (index == 0) return false
        val previous = words[index - 1].lowercase()
        if (previous in determiners) return true
        return index >= 2 &&
            inflectedAdjective.matches(previous) &&
            words[index - 2].lowercase() in determiners
    }

    fun map(raw: String): String {
        if (raw.isBlank()) return ""
        val words = raw.trim().split(Regex("\\s+"))
        val out = StringBuilder()
        var capitalizeNext = false
        var joinNext = false
        var i = 0
        while (i < words.size) {
            val rule = matchAt(words, i)?.takeUnless { isSpokenNoun(words, i, it) }
            if (rule != null) {
                when (rule.kind) {
                    Kind.ATTACH -> {
                        out.append(rule.mark)
                        if (rule.mark.first() in sentenceEnders) capitalizeNext = true
                        joinNext = false
                    }
                    Kind.HYPHEN -> {
                        out.append('-')
                        joinNext = true
                    }
                    Kind.BREAK -> {
                        out.append(rule.mark)
                        capitalizeNext = true
                        joinNext = true
                    }
                }
                i += rule.words.size
            } else {
                var word = words[i]
                if (capitalizeNext) {
                    word = capitalizeFirstLetter(word)
                    capitalizeNext = false
                }
                if (out.isNotEmpty() && !joinNext) out.append(' ')
                out.append(word)
                joinNext = false
                i += 1
            }
        }
        return out.toString()
    }

    private fun matchAt(words: List<String>, index: Int): Rule? {
        for (rule in rules) {
            if (index + rule.words.size > words.size) continue
            var matches = true
            for (j in rule.words.indices) {
                if (!words[index + j].equals(rule.words[j], ignoreCase = true)) {
                    matches = false
                    break
                }
            }
            if (matches) return rule
        }
        return null
    }
}

/** Uppercases the first character if it is a letter; leaves everything else untouched. */
internal fun capitalizeFirstLetter(text: String): String {
    if (text.isEmpty()) return text
    val first = text.first()
    if (!first.isLetter() || first.isUpperCase()) return text
    return first.uppercaseChar() + text.substring(1)
}
