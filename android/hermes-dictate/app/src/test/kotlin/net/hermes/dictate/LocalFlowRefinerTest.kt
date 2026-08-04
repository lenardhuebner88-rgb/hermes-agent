package net.hermes.dictate

import org.junit.Assert.assertEquals
import org.junit.Test

class LocalFlowRefinerTest {
    @Test
    fun `removes German fillers and immediate repetitions`() {
        assertEquals(
            "das ist ein Test",
            LocalFlowRefiner.refine("äh das das ist ähm ein Test", "de-DE"),
        )
    }

    @Test
    fun `keeps German um because it is usually a real preposition`() {
        assertEquals("Treffen um drei", LocalFlowRefiner.refine("Treffen um drei", "de-DE"))
    }

    @Test
    fun `resolves a German spoken backtrack to the corrected word`() {
        assertEquals(
            "Treffen am Mittwoch",
            LocalFlowRefiner.refine("Treffen am Dienstag, nein, ich meine Mittwoch", "de-DE"),
        )
    }

    @Test
    fun `resolves an English spoken backtrack and English fillers`() {
        assertEquals(
            "meet on Wednesday",
            LocalFlowRefiner.refine("um meet on Tuesday, no wait, I mean Wednesday", "en-US"),
        )
    }

    @Test
    fun `recognizes only exact explicit edit commands`() {
        assertEquals(
            DictationTransform.UndoLastSegment,
            LocalFlowRefiner.transform("Nein, zurück.", "de-DE"),
        )
        assertEquals(
            DictationTransform.DeleteLastSentence,
            LocalFlowRefiner.transform("Lösche den letzten Satz", "de-DE"),
        )
        assertEquals(
            DictationTransform.Text("ich gehe nicht zurück"),
            LocalFlowRefiner.transform("ich gehe nicht zurück", "de-DE"),
        )
    }

    @Test
    fun `accepts the natural German phrasings for undo and delete`() {
        for (spoken in listOf("Streich das.", "Vergiss das", "Lösch das!", "Das war falsch", "Scratch that")) {
            assertEquals(spoken, DictationTransform.UndoLastSegment, LocalFlowRefiner.transform(spoken, "de-DE"))
        }
        for (spoken in listOf("Letzten Satz löschen", "Streich den letzten Satz", "delete last sentence")) {
            assertEquals(spoken, DictationTransform.DeleteLastSentence, LocalFlowRefiner.transform(spoken, "de-DE"))
        }
    }

    @Test
    fun `a command word inside a longer sentence stays dictated text`() {
        for (spoken in listOf(
            "streich das Meeting am Montag",
            "vergiss das nicht",
            "das war falsch berechnet",
            "lösche den letzten Satz aus dem Protokoll",
        )) {
            val transform = LocalFlowRefiner.transform(spoken, "de-DE")
            assertEquals(spoken, DictationTransform.Text::class.java, transform.javaClass)
        }
    }

    @Test
    fun `ordinary prose survives the correction and filler cleanup`() {
        for (sentence in listOf(
            "Es funktioniert besser jetzt",
            "das läuft besser als gestern",
            "wir haben nein gesagt",
            "was ich meine ist ganz einfach",
            "im Grunde genommen ist es fertig",
            "nein danke das reicht mir",
        )) {
            assertEquals(sentence, LocalFlowRefiner.refine(sentence, "de-DE"))
        }
    }

    @Test
    fun `repeated punctuation command words are not collapsed`() {
        assertEquals(
            "Absatz eins neuer Absatz Absatz zwei",
            LocalFlowRefiner.refine("Absatz eins neuer Absatz Absatz zwei", "de-DE"),
        )
    }

    @Test
    fun `does not damage URLs email addresses numbers or code fragments`() {
        val input = "mail a.b+test@example.com URL https://x.dev/a_a Wert 82,4 code foo_bar()"
        assertEquals(input, LocalFlowRefiner.refine(input, "de-DE"))
    }

    // --- Regressions from the Codex review of 2026-08-04 ---

    @Test
    fun `a leading correction marker needs an explicit qualifier`() {
        assertEquals("Ich meine das ernst", LocalFlowRefiner.refine("Ich meine das ernst", "de-DE"))
        assertEquals("Nein, danke das reicht mir", LocalFlowRefiner.refine("Nein, danke das reicht mir", "de-DE"))
        // The unmistakable backtrack still loses its opener.
        assertEquals("Kanban Board öffnen", LocalFlowRefiner.refine("nein, halt, eher Kanban Board öffnen", "de-DE"))
    }

    @Test
    fun `a correction after a demonstrative still resolves`() {
        assertEquals("Nimm jenes", LocalFlowRefiner.refine("Nimm das nein jenes", "de-DE"))
    }

    @Test
    fun `causal also is kept and filler also is dropped`() {
        assertEquals("Also folgt daraus der Beweis", LocalFlowRefiner.refine("Also folgt daraus der Beweis", "de-DE"))
        assertEquals("wir prüfen den Build", LocalFlowRefiner.refine("also wir prüfen den Build", "de-DE"))
        assertEquals("Okay dann starten wir", LocalFlowRefiner.refine("Okay also dann starten wir", "de-DE"))
    }
}
