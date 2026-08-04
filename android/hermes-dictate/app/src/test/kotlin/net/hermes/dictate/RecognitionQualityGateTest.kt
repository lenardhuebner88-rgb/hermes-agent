package net.hermes.dictate

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Privacy-safe, text-only quality gate. The inputs model plausible German recognizer output;
 * neither audio nor operator dictation is stored here. WER deliberately keeps case and attached
 * punctuation significant because capitalization and punctuation are part of the product promise.
 */
class RecognitionQualityGateTest {
    private data class QualityCase(
        val category: String,
        val recognized: String,
        val expected: String,
        val generalization: Boolean = false,
    )

    private val dictionary = """
        plansback => PlanSpec
        planspeak => PlanSpec
        plan speck => PlanSpec
        plansweg => PlanSpec
        plan spec => PlanSpec
        planspec => PlanSpec
        kanban bord => Kanban Board
        kamernboot => Kanban Board
        kanban board => Kanban Board
        hades track => Health Track
        heldsweg => Health Track
        health track => Health Track
        healthtrack => Health Track
        her mess => Hermes
        hermes => Hermes
        tail scale => Tailscale
        tails cale => Tailscale
        tale scale => Tailscale
        tailscale => Tailscale
        pytest lauf => pytest-Lauf
        connected debug android test => connectedDebugAndroidTest
        build config debug => BuildConfig.DEBUG
    """.trimIndent()

    private val pipeline = DictationTextPipeline(
        dictionaryRules = { dictionary },
        languageTag = { "de-DE" },
    )

    private val corpus = listOf(
        QualityCase("fillers", "ähm wir starten jetzt", "Wir starten jetzt.", true),
        QualityCase("fillers", "also, wir prüfen den Build", "Wir prüfen den Build.", true),
        QualityCase("fillers", "wir müssen hm die Tests ausführen", "Wir müssen die Tests ausführen.", true),
        QualityCase("fillers", "mhm das sieht gut aus", "Das sieht gut aus.", true),
        QualityCase("fillers", "äh die Freigabe fehlt noch", "Die Freigabe fehlt noch.", true),
        QualityCase("fillers", "wir sind sozusagen fertig", "Wir sind fertig.", true),
        QualityCase("fillers", "das ist im Grunde bereit", "Das ist bereit.", true),
        QualityCase("fillers", "wir können gewissermaßen starten", "Wir können starten.", true),
        QualityCase("fillers", "wir prüfen ähm noch einmal", "Wir prüfen noch einmal.", true),
        QualityCase("fillers", "der Build ist quasi grün", "Der Build ist grün.", true),
        QualityCase("fillers", "ähm PlanSpec ist bereit", "PlanSpec ist bereit.", true),
        QualityCase("fillers", "also, Tailscale ist verbunden", "Tailscale ist verbunden.", true),

        QualityCase("corrections", "Montag, nein ich meine Dienstag", "Dienstag"),
        QualityCase("corrections", "rot, nein besser grün", "Grün"),
        QualityCase("corrections", "vier, nein ich meine fünf", "Fünf"),
        QualityCase("corrections", "wir starten Montag, nein, halt, eher Dienstag", "Wir starten Dienstag.", true),
        QualityCase("corrections", "der Port ist 8080, nein, halt, eher 8081", "Der Port ist 8081.", true),
        QualityCase("corrections", "schicke es an Anna, nein, an Piet", "Schicke es an Piet.", true),
        QualityCase("corrections", "das Meeting ist heute, genauer gesagt morgen", "Das Meeting ist morgen.", true),
        QualityCase("corrections", "wir deployen direkt, besser nach dem Gate", "Wir deployen nach dem Gate.", true),
        QualityCase("corrections", "PlanSpec PlanSpec öffnen", "PlanSpec öffnen.", true),
        QualityCase("corrections", "bitte bitte den Test starten", "Bitte den Test starten.", true),
        QualityCase("corrections", "nein, halt, eher Kanban Board öffnen", "Kanban Board öffnen.", true),
        QualityCase("corrections", "der Status ist offen, ich meine blockiert", "Der Status ist blockiert.", true),

        QualityCase("punctuation", "hallo welt punkt", "Hallo welt."),
        QualityCase("punctuation", "wir starten jetzt komma danach prüfen wir punkt", "Wir starten jetzt, danach prüfen wir."),
        QualityCase("punctuation", "ist der Build grün fragezeichen", "Ist der Build grün?"),
        QualityCase("punctuation", "achtung ausrufezeichen", "Achtung!"),
        QualityCase("punctuation", "erste zeile neue zeile zweite zeile", "Erste zeile\nZweite zeile."),
        QualityCase("punctuation", "einleitung doppelpunkt test läuft", "Einleitung: test läuft."),
        QualityCase("punctuation", "a bindestrich b", "A-B"),
        QualityCase("punctuation", "erster satz punkt zweiter satz punkt", "Erster satz. Zweiter satz."),
        QualityCase("punctuation", "frage eins fragezeichen frage zwei fragezeichen", "Frage eins? Frage zwei?"),
        QualityCase("punctuation", "ja komma aber später punkt", "Ja, aber später."),
        QualityCase("punctuation", "wert semikolon nächster wert punkt", "Wert; nächster wert."),
        QualityCase("punctuation", "ein absatz neuer absatz nächster absatz", "Ein absatz\n\nNächster absatz."),

        QualityCase("numbers_dates", "Termin am 4. August 2026", "Termin am 4. August 2026."),
        QualityCase("numbers_dates", "wir treffen uns am vierten August", "Wir treffen uns am 4. August.", true),
        QualityCase("numbers_dates", "der Termin ist am einundzwanzigsten September", "Der Termin ist am 21. September.", true),
        QualityCase("numbers_dates", "Beginn um vierzehn Uhr dreißig", "Beginn um 14:30 Uhr.", true),
        QualityCase("numbers_dates", "Port acht null acht null", "Port 8080.", true),
        QualityCase("numbers_dates", "Version 1.5 ist installiert", "Version 1.5 ist installiert."),
        QualityCase("numbers_dates", "zweiundvierzig Aufgaben sind offen", "42 Aufgaben sind offen.", true),
        QualityCase("numbers_dates", "am 31. Dezember 2026 endet die Frist", "Am 31. Dezember 2026 endet die Frist."),

        QualityCase("terms_code", "Hermes Diktat soll Plansback erkennen", "Hermes Diktat soll PlanSpec erkennen."),
        QualityCase("terms_code", "Hermes Diktat soll Plansbek erkennen", "Hermes Diktat soll PlanSpec erkennen.", true),
        QualityCase("terms_code", "öffne plan speck", "Öffne PlanSpec."),
        QualityCase("terms_code", "öffne das Kanban Bord", "Öffne das Kanban Board."),
        QualityCase("terms_code", "öffne das Kameranboot", "Öffne das Kanban Board.", true),
        QualityCase("terms_code", "der health track ist grün", "Der Health Track ist grün."),
        QualityCase("terms_code", "der Hades Träck ist grün", "Der Health Track ist grün.", true),
        QualityCase("terms_code", "her mess startet", "Hermes startet."),
        QualityCase("terms_code", "hermis startet", "Hermes startet.", true),
        QualityCase("terms_code", "tails cale verbindet das Gerät", "Tailscale verbindet das Gerät."),
        QualityCase("terms_code", "tail skale verbindet das Gerät", "Tailscale verbindet das Gerät.", true),
        QualityCase("terms_code", "öffne die PlanSpec im Kanban Board", "Öffne die PlanSpec im Kanban Board."),
        QualityCase("terms_code", "der Health Track läuft über Tailscale", "Der Health Track läuft über Tailscale."),
        QualityCase("terms_code", "Hermes erstellt eine PlanSpec", "Hermes erstellt eine PlanSpec."),
        QualityCase("terms_code", "starte den pytest lauf", "Starte den pytest-Lauf."),
        QualityCase("terms_code", "nutze git diff und git status", "Nutze git diff und git status."),
        QualityCase("terms_code", "die Klasse DictationController bleibt stabil", "Die Klasse DictationController bleibt stabil."),
        QualityCase("terms_code", "rufe connected Debug Android Test auf", "Rufe connectedDebugAndroidTest auf."),
        QualityCase("terms_code", "prüfe Build Config Debug", "Prüfe BuildConfig.DEBUG."),
        QualityCase("terms_code", "die Datei AGENTS.md bleibt unverändert", "Die Datei AGENTS.md bleibt unverändert."),
    )

    @Test
    fun `corpus word error rate stays below quality threshold`() {
        assertTrue("quality corpus must contain at least 60 cases", corpus.size >= 60)
        assertTrue(
            "at least one third of the corpus must be unseen generalization cases",
            corpus.count(QualityCase::generalization) * 3 >= corpus.size,
        )

        var edits = 0
        var referenceWords = 0
        val misses = mutableListOf<String>()
        for (case in corpus) {
            val actual = pipeline.process(case.recognized).text()
            val expectedWords = words(case.expected)
            val actualWords = words(actual)
            edits += editDistance(expectedWords, actualWords)
            referenceWords += expectedWords.size
            if (actual != case.expected) {
                misses += "${case.category}: '${case.recognized}' -> '$actual' (want '${case.expected}')"
            }
        }
        val wer = edits.toDouble() / referenceWords
        println(
            "RecognitionQualityGate corpus=${corpus.size} generalization=" +
                "${corpus.count(QualityCase::generalization)} edits=$edits words=$referenceWords " +
                "WER=${"%.4f".format(java.util.Locale.ROOT, wer)} exact=${corpus.size - misses.size}/${corpus.size}",
        )
        misses.forEach { println("RecognitionQualityMiss $it") }
        println(
            "RecognitionQualityStages " + Stage.entries.joinToString(" ") { stage ->
                "${stage.name.lowercase()}=${"%.4f".format(java.util.Locale.ROOT, corpusWer(stage))}"
            },
        )
        assertTrue(
            "WER ${"%.4f".format(java.util.Locale.ROOT, wer)} exceeds $MAX_WER; " +
                misses.take(12).joinToString(" | "),
            wer <= MAX_WER,
        )
    }

    @Test
    fun `all personal terms reach both biasing and post correction paths`() {
        val biasing = BiasingVocabulary.fromRules(dictionary, "")
        assertTrue(biasing.containsAll(listOf("PlanSpec", "Kanban Board", "Health Track", "Hermes", "Tailscale")))

        assertEquals("PlanSpec", pipeline.processPartial("Plansbek").text())
        assertEquals("Kanban Board", pipeline.processPartial("Kameranboot").text())
        assertEquals("Health Track", pipeline.processPartial("Hades Träck").text())
        assertEquals("Hermes", pipeline.processPartial("hermis").text())
        assertEquals("Tailscale", pipeline.processPartial("tail skale").text())
    }

    private fun words(value: String): List<String> = value.trim().split(Regex("\\s+")).filter(String::isNotEmpty)

    private fun corpusWer(stage: Stage): Double {
        var edits = 0
        var referenceWords = 0
        for (case in corpus) {
            val expected = words(case.expected)
            edits += editDistance(expected, words(outputAt(case.recognized, stage)))
            referenceWords += expected.size
        }
        return edits.toDouble() / referenceWords
    }

    private fun outputAt(raw: String, stage: Stage): String {
        var text = raw
        if (stage >= Stage.LOCAL_REFINE) {
            text = (LocalFlowRefiner.transform(text, "de-DE") as DictationTransform.Text).value
        }
        if (stage >= Stage.PUNCTUATION) text = PunctuationMapper.map(text)
        if (stage >= Stage.NUMBERS_DATES) text = GermanDictationNormalizer.apply(text)
        if (stage >= Stage.EXACT_DICTIONARY) text = TextPersonalizer.applyDictionary(text, dictionary)
        if (stage >= Stage.CANONICAL_GENERALIZATION) text = CanonicalTermCorrector.apply(text, dictionary)
        if (stage >= Stage.SENTENCE_FORMAT) text = FlowTextFormatter.finalize(text)
        return text
    }

    private fun editDistance(left: List<String>, right: List<String>): Int {
        val previous = IntArray(right.size + 1) { it }
        for (i in left.indices) {
            var diagonal = previous[0]
            previous[0] = i + 1
            for (j in right.indices) {
                val above = previous[j + 1]
                previous[j + 1] = minOf(
                    previous[j + 1] + 1,
                    previous[j] + 1,
                    diagonal + if (left[i] == right[j]) 0 else 1,
                )
                diagonal = above
            }
        }
        return previous[right.size]
    }

    private fun DictationTransform.text(): String = (this as DictationTransform.Text).value

    companion object {
        private const val MAX_WER = 0.10
    }

    private enum class Stage {
        RAW,
        LOCAL_REFINE,
        PUNCTUATION,
        NUMBERS_DATES,
        EXACT_DICTIONARY,
        CANONICAL_GENERALIZATION,
        SENTENCE_FORMAT,
    }
}
