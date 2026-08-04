package net.hermes.dictate

import java.util.Locale

/** Deterministic German number/date cleanup for common dictation forms. */
object GermanDictationNormalizer {
    private val months = listOf(
        "Januar", "Februar", "März", "April", "Mai", "Juni",
        "Juli", "August", "September", "Oktober", "November", "Dezember",
    )
    private val monthPattern = months.joinToString("|") { Regex.escape(it) }
    private val word = Regex("(?iu)\\b[\\p{L}-]+\\b")
    private val date = Regex("(?iu)\\b([\\p{L}-]+)\\s+($monthPattern)\\b")
    private val time = Regex("(?iu)\\b([\\p{L}-]+|\\d{1,2})\\s+Uhr\\s+([\\p{L}-]+|\\d{1,2})\\b")
    private val digitSequence = Regex(
        "(?iu)\\b(Port|PIN|Code)(\\s+(?:ist|lautet))?\\s+" +
            "((?:null|eins?|zwei|drei|vier|fünf|fuenf|sechs|sieben|acht|neun)" +
            "(?:\\s+(?:null|eins?|zwei|drei|vier|fünf|fuenf|sechs|sieben|acht|neun))+)",
    )
    // Colloquial clock forms. All require a leading "um" so ordinary prose ("zehn nach
    // Feierabend", "die halb fertige Liste") is never rewritten into a time.
    private val halfHour = Regex("(?iu)\\b(um)\\s+halb\\s+([\\p{L}]+|\\d{1,2})\\b")
    private val quarterHour = Regex("(?iu)\\b(um)\\s+viertel\\s+(nach|vor)\\s+([\\p{L}]+|\\d{1,2})\\b")
    private val relativeMinute = Regex("(?iu)\\b(um)\\s+([\\p{L}]+|\\d{1,2})\\s+(nach|vor)\\s+([\\p{L}]+|\\d{1,2})\\b")
    private val bareHour = Regex("(?iu)\\b([\\p{L}]+)\\s+Uhr\\b")
    private val decimal = Regex("(?iu)\\b([\\p{L}]+|\\d+),\\s*([\\p{L}]+|\\d+)\\b")

    fun apply(text: String): String {
        var result = digitSequence.replace(text) { match ->
            val digits = match.groupValues[3].split(Regex("\\s+")).mapNotNull(::digit).joinToString("")
            "${match.groupValues[1]}${match.groupValues[2]} $digits"
        }
        result = quarterHour.replace(result) { match ->
            val anchor = clockHour(match.groupValues[3]) ?: return@replace match.value
            if (match.groupValues[2].equals("nach", ignoreCase = true)) {
                "${match.groupValues[1]} ${clock(anchor, 15)}"
            } else {
                "${match.groupValues[1]} ${clock(previousHour(anchor), 45)}"
            }
        }
        result = halfHour.replace(result) { match ->
            val anchor = clockHour(match.groupValues[2]) ?: return@replace match.value
            "${match.groupValues[1]} ${clock(previousHour(anchor), 30)}"
        }
        result = relativeMinute.replace(result) { match ->
            val minute = number(match.groupValues[2]) ?: return@replace match.value
            val anchor = clockHour(match.groupValues[4]) ?: return@replace match.value
            if (minute !in 1..59) return@replace match.value
            if (match.groupValues[3].equals("nach", ignoreCase = true)) {
                "${match.groupValues[1]} ${clock(anchor, minute)}"
            } else {
                "${match.groupValues[1]} ${clock(previousHour(anchor), 60 - minute)}"
            }
        }
        result = time.replace(result) { match ->
            val hour = number(match.groupValues[1])
            val minute = number(match.groupValues[2])
            if (hour != null && minute != null && hour in 0..23 && minute in 0..59) {
                clock(hour, minute)
            } else {
                match.value
            }
        }
        result = bareHour.replace(result) { match ->
            val hour = number(match.groupValues[1])
            if (hour != null && hour in 0..23) "$hour Uhr" else match.value
        }
        result = date.replace(result) { match ->
            val day = ordinal(match.groupValues[1])
            if (day != null && day in 1..31) "$day. ${canonicalMonth(match.groupValues[2])}" else match.value
        }
        result = decimal.replace(result) { match ->
            val whole = number(match.groupValues[1])
            val fraction = number(match.groupValues[2])
            if (whole != null && fraction != null) "$whole,$fraction" else match.value
        }
        return word.replace(result) { match ->
            val value = number(match.value)
            // Single-digit words are too ambiguous in prose; compounds and teens are useful.
            if (value != null && value >= 13) value.toString() else match.value
        }
    }

    private fun clock(hour: Int, minute: Int): String =
        String.format(Locale.ROOT, "%d:%02d Uhr", hour, minute)

    /** "halb drei" and "viertel vor sechs" name the hour they run up to. */
    private fun previousHour(hour: Int): Int = if (hour <= 1) hour + 11 else hour - 1

    private fun clockHour(value: String): Int? =
        (value.toIntOrNull() ?: number(value))?.takeIf { it in 0..23 }

    private fun canonicalMonth(value: String): String =
        months.firstOrNull { it.equals(value, ignoreCase = true) } ?: value

    /** German ordinals whose stem is not the cardinal ("ersten", "dritten", "siebten", "achten"). */
    private val irregularOrdinals = mapOf(
        "erst" to 1, "dritt" to 3, "siebt" to 7, "siebent" to 7, "acht" to 8, "sechst" to 6,
    )
    private val ordinalSuffixes = listOf("sten", "stes", "ster", "ste", "ten", "tes", "ter", "tem", "te")

    private fun ordinal(value: String): Int? {
        val normalized = normalize(value)
        for (suffix in ordinalSuffixes) {
            if (!normalized.endsWith(suffix)) continue
            val base = normalized.dropLast(suffix.length)
            if (base.isEmpty()) continue
            irregularOrdinals[base]?.let { return it }
            number(base)?.let { return it }
        }
        // "achte"/"achten" strip to "ach"; the irregular table is keyed on the full stem instead.
        return irregularOrdinals[normalized.removeSuffix("en").removeSuffix("e")]
    }

    private fun digit(value: String): Int? = when (normalize(value)) {
        "null" -> 0
        "ein", "eins" -> 1
        "zwei" -> 2
        "drei" -> 3
        "vier" -> 4
        "funf" -> 5
        "sechs" -> 6
        "sieben" -> 7
        "acht" -> 8
        "neun" -> 9
        else -> null
    }

    private fun number(value: String): Int? {
        val normalized = normalize(value)
        digit(normalized)?.let { return it }
        val direct = mapOf(
            "zehn" to 10, "elf" to 11, "zwolf" to 12, "dreizehn" to 13,
            "vierzehn" to 14, "funfzehn" to 15, "sechzehn" to 16,
            "siebzehn" to 17, "achtzehn" to 18, "neunzehn" to 19,
            "zwanzig" to 20, "dreissig" to 30, "vierzig" to 40,
            "funfzig" to 50, "sechzig" to 60, "siebzig" to 70,
            "achtzig" to 80, "neunzig" to 90,
        )
        direct[normalized]?.let { return it }
        magnitude(normalized, "tausend", 1_000)?.let { return it }
        magnitude(normalized, "hundert", 100)?.let { return it }
        val and = normalized.indexOf("und")
        if (and <= 0) return null
        val unit = digit(normalized.substring(0, and)) ?: return null
        val tens = direct[normalized.substring(and + 3)] ?: return null
        return (unit + tens).takeIf { it in 21..99 }
    }

    /** "zweihundertfünfzig" = 2 x 100 + 50; a bare "hundert"/"tausend" means one of them. */
    private fun magnitude(normalized: String, marker: String, factor: Int): Int? {
        val index = normalized.indexOf(marker)
        if (index < 0) return null
        val head = normalized.substring(0, index)
        val tail = normalized.substring(index + marker.length)
        val count = if (head.isEmpty()) 1 else number(head) ?: return null
        val rest = if (tail.isEmpty()) 0 else number(tail) ?: return null
        return count * factor + rest
    }

    private fun normalize(value: String): String = value.lowercase(Locale.ROOT)
        .replace("ä", "a")
        .replace("ö", "o")
        .replace("ü", "u")
        .replace("ß", "ss")
        .replace("-", "")
}

/** Sentence casing is useful for previews; terminal punctuation is final-result-only. */
object FlowTextFormatter {
    fun preview(text: String): String = capitalizeSentences(text.trim())

    fun finalize(text: String): String {
        val styled = Regex("(?u)\\b([\\p{L}])-([\\p{L}])\\b").replace(preview(text)) { match ->
            "${match.groupValues[1]}-${match.groupValues[2].uppercase(Locale.ROOT)}"
        }
        if (styled.isEmpty()) return styled
        val wordCount = styled.split(Regex("\\s+")).count(String::isNotBlank)
        return if (wordCount >= 2 && styled.last() !in ".!?:;,\n") "$styled." else styled
    }

    private fun capitalizeSentences(text: String): String {
        val result = StringBuilder(text.length)
        var sentenceStart = true
        var terminalSeen = false
        for (char in text) {
            val output = if (sentenceStart && char.isLetter()) char.uppercaseChar() else char
            result.append(output)
            when {
                char == '\n' -> {
                    sentenceStart = true
                    terminalSeen = false
                }
                char == '.' || char == '!' || char == '?' -> terminalSeen = true
                char.isWhitespace() && terminalSeen -> sentenceStart = true
                !char.isWhitespace() -> {
                    if (char.isLetterOrDigit()) sentenceStart = false
                    terminalSeen = false
                }
            }
        }
        return result.toString()
    }
}
