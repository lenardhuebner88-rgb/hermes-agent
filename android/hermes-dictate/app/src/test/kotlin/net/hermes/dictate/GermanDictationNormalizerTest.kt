package net.hermes.dictate

import org.junit.Assert.assertEquals
import org.junit.Test

/**
 * Focused unit coverage for the German value normalizer. `RecognitionHoldoutTest` measures the
 * corpus-level effect; these cases localize a regression to a single rule.
 */
class GermanDictationNormalizerTest {

    private fun normalize(text: String) = GermanDictationNormalizer.apply(text)

    @Test
    fun `irregular ordinals resolve to their day number`() {
        assertEquals("am 1. August", normalize("am ersten August"))
        assertEquals("der 3. Oktober", normalize("der dritte Oktober"))
        assertEquals("am 7. Juli", normalize("am siebten Juli"))
        assertEquals("am 8. Mai", normalize("am achten Mai"))
        assertEquals("am 6. Juni", normalize("am sechsten Juni"))
    }

    @Test
    fun `regular ordinals keep working`() {
        assertEquals("am 4. August", normalize("am vierten August"))
        assertEquals("am 12. Dezember", normalize("am zwölften Dezember"))
        assertEquals("am 20. September", normalize("am zwanzigsten September"))
        assertEquals("am 31. Januar", normalize("am einunddreißigsten Januar"))
    }

    @Test
    fun `magnitudes above ninety nine become digits`() {
        assertEquals("100 Aufgaben", normalize("hundert Aufgaben"))
        assertEquals("250 Euro", normalize("zweihundertfünfzig Euro"))
        assertEquals("1000 Zeilen", normalize("tausend Zeilen"))
        assertEquals("120 Sekunden", normalize("einhundertzwanzig Sekunden"))
        assertEquals("95 Prozent", normalize("fünfundneunzig Prozent"))
    }

    @Test
    fun `colloquial clock forms need a leading um`() {
        assertEquals("um 2:30 Uhr", normalize("um halb drei"))
        assertEquals("um 10:15 Uhr", normalize("um viertel nach zehn"))
        assertEquals("um 5:45 Uhr", normalize("um viertel vor sechs"))
        assertEquals("um 8:10 Uhr", normalize("um zehn nach acht"))
        assertEquals("um 11:40 Uhr", normalize("um zwanzig vor zwölf"))
        assertEquals("um 14:30 Uhr", normalize("um vierzehn Uhr dreißig"))
        assertEquals("um 8 Uhr", normalize("um acht Uhr"))
    }

    @Test
    fun `half past twelve wraps to the twelve hour reading`() {
        assertEquals("um 12:30 Uhr", normalize("um halb eins"))
    }

    @Test
    fun `clock rewriting never fires without the um anchor`() {
        assertEquals("zehn nach Feierabend gehen wir", normalize("zehn nach Feierabend gehen wir"))
        assertEquals("die halb fertige Liste", normalize("die halb fertige Liste"))
    }

    @Test
    fun `digit sequences keep the spoken copula`() {
        assertEquals("Die PIN ist 1234", normalize("Die PIN ist eins zwei drei vier"))
        assertEquals("Port 8081", normalize("Port acht null acht eins"))
    }

    @Test
    fun `decimals spoken as komma become a single number`() {
        assertEquals("Version 1,5 ist installiert", normalize("Version eins, fünf ist installiert"))
    }

    @Test
    fun `small cardinals stay words in prose`() {
        assertEquals("wir brauchen zwei Stunden", normalize("wir brauchen zwei Stunden"))
    }

    // --- Regressions from the Codex review of 2026-08-04 ---

    @Test
    fun `clock rules accept digits the regex already allows`() {
        assertEquals("um 14:30 Uhr", normalize("um 14 Uhr dreißig"))
        assertEquals("um 14:30 Uhr", normalize("um vierzehn Uhr 30"))
        assertEquals("um 8:10 Uhr", normalize("um 10 nach acht"))
    }

    @Test
    fun `midnight wraps backwards to twenty three`() {
        assertEquals("um 23:30 Uhr", normalize("um halb 0"))
        assertEquals("um 23:45 Uhr", normalize("um viertel vor 0"))
    }

    @Test
    fun `a spoken enumeration is not turned into a decimal`() {
        assertEquals("eins, zwei, drei", normalize("eins, zwei, drei"))
        assertEquals("Version 1,5 ist installiert", normalize("Version eins, fünf ist installiert"))
    }
}
