package net.hermes.dictate

import java.text.Normalizer
import java.util.Locale

/**
 * Conservative second-line correction for canonical terms in the personal dictionary.
 *
 * Exact `spoken => written` rules remain authoritative. This corrector only generalizes when the
 * user has already confirmed at least two genuinely different spoken forms for the same written
 * target. A candidate must then be within a tiny edit radius of one of those learned forms.
 * Ambiguous matches and text next to URL, email, code or numeric syntax are left untouched.
 */
object CanonicalTermCorrector {
    private const val MIN_LEARNED_FORMS = 2
    private const val MAX_WINDOW_WORDS = 4
    private const val MAX_NORMALIZED_CHARS = 80

    private val word = Regex("[\\p{L}\\p{M}]+")
    private val combiningMark = Regex("\\p{M}+")

    private data class LearnedTerm(
        val written: String,
        val signatures: List<String>,
    )

    private data class WordSpan(val start: Int, val endExclusive: Int, val value: String)

    private data class TermScore(
        val term: LearnedTerm,
        val distance: Int,
        val signatureLength: Int,
    ) {
        val scaledDistance: Int
            get() = distance * 1_000 / signatureLength.coerceAtLeast(1)
    }

    private data class Replacement(
        val start: Int,
        val endExclusive: Int,
        val written: String,
        val distance: Int,
        val scaledDistance: Int,
        val wordCount: Int,
    )

    fun apply(text: String, rawRules: String): String {
        if (text.isBlank() || rawRules.isBlank()) return text
        val terms = learnedTerms(rawRules)
        if (terms.isEmpty()) return text
        val words = word.findAll(text).map {
            WordSpan(it.range.first, it.range.last + 1, it.value)
        }.toList()
        if (words.isEmpty()) return text

        val candidates = mutableListOf<Replacement>()
        for (startIndex in words.indices) {
            val startWord = words[startIndex]
            for (endIndex in startIndex until minOf(words.size, startIndex + MAX_WINDOW_WORDS)) {
                val endWord = words[endIndex]
                if (endIndex > startIndex) {
                    val separator = text.substring(words[endIndex - 1].endExclusive, endWord.start)
                    if (separator.any { !it.isWhitespace() }) break
                }
                if (isUnsafeContext(text, startWord.start, endWord.endExclusive)) continue
                val rawCandidate = text.substring(startWord.start, endWord.endExclusive)
                val signature = normalize(rawCandidate)
                if (signature.length !in 5..MAX_NORMALIZED_CHARS) continue

                val scores = terms.mapNotNull { score(signature, it) }
                    .sortedWith(compareBy(TermScore::scaledDistance, TermScore::distance))
                val best = scores.firstOrNull() ?: continue
                val second = scores.getOrNull(1)
                if (second != null && second.scaledDistance == best.scaledDistance) continue

                val selectedWords = words.subList(startIndex, endIndex + 1)
                if (best.distance >= 2 && selectedWords.size > 1 &&
                    selectedWords.any { it.value.firstOrNull()?.isUpperCase() != true }
                ) {
                    // Avoid broadening an explicit alias such as "plan speck" into an ordinary
                    // sentence fragment such as "Plan steckt".
                    continue
                }
                candidates += Replacement(
                    start = startWord.start,
                    endExclusive = endWord.endExclusive,
                    written = best.term.written,
                    distance = best.distance,
                    scaledDistance = best.scaledDistance,
                    wordCount = selectedWords.size,
                )
            }
        }
        if (candidates.isEmpty()) return text

        val selected = mutableListOf<Replacement>()
        var consumedUntil = -1
        for (candidate in candidates.sortedWith(
            compareBy<Replacement> { it.start }
                .thenBy { it.scaledDistance }
                .thenBy { it.distance }
                .thenByDescending { it.wordCount }
                .thenByDescending { it.endExclusive - it.start },
        )) {
            if (candidate.start < consumedUntil) continue
            selected += candidate
            consumedUntil = candidate.endExclusive
        }

        val result = StringBuilder(text)
        for (replacement in selected.asReversed()) {
            result.replace(replacement.start, replacement.endExclusive, replacement.written)
        }
        return result.toString()
    }

    private fun learnedTerms(rawRules: String): List<LearnedTerm> = TextPersonalizer.parse(rawRules)
        .groupBy { it.written.lowercase(Locale.ROOT) }
        .mapNotNull { (_, rules) ->
            val learned = rules.map { normalize(it.spoken) }
                .filter { it.length >= 5 }
                .distinct()
            if (learned.size < MIN_LEARNED_FORMS) return@mapNotNull null
            val written = rules.first().written
            LearnedTerm(
                written = written,
                signatures = (learned + normalize(written)).filter { it.isNotEmpty() }.distinct(),
            )
        }

    private fun score(candidate: String, term: LearnedTerm): TermScore? {
        val closest = term.signatures.asSequence()
            .filter { signature ->
                signature.firstOrNull() == candidate.firstOrNull() &&
                    kotlin.math.abs(signature.length - candidate.length) <= 2
            }
            .map { signature ->
                TermScore(term, editDistance(candidate, signature), signature.length)
            }
            .minWithOrNull(compareBy(TermScore::scaledDistance, TermScore::distance))
            ?: return null
        val longest = maxOf(candidate.length, closest.signatureLength)
        val maximum = when {
            longest <= 6 -> 1
            longest <= 14 -> 2
            else -> 3
        }
        if (closest.distance > maximum || closest.scaledDistance > 250) return null
        return closest
    }

    private fun normalize(value: String): String {
        val decomposed = Normalizer.normalize(value.lowercase(Locale.ROOT), Normalizer.Form.NFD)
        return combiningMark.replace(decomposed, "")
            .replace("ß", "ss")
            .filter(Char::isLetter)
            .take(MAX_NORMALIZED_CHARS + 1)
    }

    private fun isUnsafeContext(text: String, start: Int, endExclusive: Int): Boolean {
        val before = text.getOrNull(start - 1)
        val after = text.getOrNull(endExclusive)
        if (before != null && (before.isLetterOrDigit() || before in TECHNICAL_BOUNDARIES)) return true
        if (after != null && (after.isLetterOrDigit() || after in TECHNICAL_BOUNDARIES)) return true
        if (before == '.' && text.getOrNull(start - 2)?.isLetterOrDigit() == true) return true
        if (after == '.' && text.getOrNull(endExclusive + 1)?.isLetterOrDigit() == true) return true
        val candidate = text.substring(start, endExclusive)
        return candidate.any(Char::isDigit) || candidate.any { it in TECHNICAL_BOUNDARIES }
    }

    /** Damerau-Levenshtein with adjacent transpositions, bounded by short dictionary phrases. */
    private fun editDistance(left: String, right: String): Int {
        if (left == right) return 0
        if (left.isEmpty()) return right.length
        if (right.isEmpty()) return left.length
        val matrix = Array(left.length + 1) { IntArray(right.length + 1) }
        for (i in 0..left.length) matrix[i][0] = i
        for (j in 0..right.length) matrix[0][j] = j
        for (i in 1..left.length) {
            for (j in 1..right.length) {
                val cost = if (left[i - 1] == right[j - 1]) 0 else 1
                var value = minOf(
                    matrix[i - 1][j] + 1,
                    matrix[i][j - 1] + 1,
                    matrix[i - 1][j - 1] + cost,
                )
                if (i > 1 && j > 1 && left[i - 1] == right[j - 2] &&
                    left[i - 2] == right[j - 1]
                ) {
                    value = minOf(value, matrix[i - 2][j - 2] + cost)
                }
                matrix[i][j] = value
            }
        }
        return matrix[left.length][right.length]
    }

    private const val TECHNICAL_BOUNDARIES = "_@/:.+-#\\"
}
