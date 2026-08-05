package net.hermes.deck.nostr

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Verified against events pulled verbatim from the live Buzz relay's Postgres
 * (`real-relay-events.jsonl`). Hand-written vectors would only prove that our
 * serializer agrees with itself; these prove it agrees with the server that has
 * to accept our events. The fixture deliberately contains umlauts, newlines and
 * four-element tags — the exact places where a JSON escaper drifts.
 */
class NostrEventTest {

    private fun realEvents(): List<NostrEvent> {
        val stream = checkNotNull(javaClass.classLoader?.getResourceAsStream("real-relay-events.jsonl")) {
            "real-relay-events.jsonl is missing from test resources"
        }
        return stream.bufferedReader().readLines()
            .filter { it.isNotBlank() }
            .map { NostrEvent.fromJson(JSONObject(it)) }
    }

    @Test
    fun `the fixture actually loaded and covers the interesting shapes`() {
        val events = realEvents()
        assertTrue("expected real relay events, got ${events.size}", events.size >= 6)
        assertTrue("fixture has no non-ASCII content", events.any { e -> e.content.any { it.code > 127 } })
        assertTrue("fixture has no multi-line content", events.any { it.content.contains("\n") })
        assertTrue("fixture has no marked e-tag", events.any { e -> e.tags.any { it.size >= 4 } })
    }

    @Test
    fun `recomputing the id reproduces what the relay stored`() {
        for (event in realEvents()) {
            val computed = NostrEvent.computeId(
                event.pubkey, event.createdAt, event.kind, event.tags, event.content,
            )
            assertEquals("kind ${event.kind} event ${event.id}", event.id, computed)
        }
    }

    @Test
    fun `every real signature verifies`() {
        for (event in realEvents()) {
            assertTrue("kind ${event.kind} event ${event.id} failed to verify", event.verify())
        }
    }

    @Test
    fun `a single flipped content character breaks the id`() {
        val event = realEvents().first { it.content.isNotEmpty() }
        val tampered = event.copy(content = event.content + " ")
        assertFalse(tampered.verify())
    }

    @Test
    fun `escaping follows NIP-01 and not the org json conventions`() {
        val serialized = NostrEvent.serializeForId(
            pubkey = "a".repeat(64),
            createdAt = 1,
            kind = 9,
            tags = emptyList(),
            content = "</a> ümlaut\n\ttab \"quote\" back\\slash",
        )
        // org.json would emit <\/a> and ü here; both would change the hash.
        assertTrue("forward slash must stay bare", serialized.contains("</a>"))
        assertTrue("non-ASCII must stay literal", serialized.contains("ümlaut"))
        assertTrue(serialized.contains("\\n"))
        assertTrue(serialized.contains("\\t"))
        assertTrue(serialized.contains("\\\"quote\\\""))
        assertTrue(serialized.contains("back\\\\slash"))
    }

    @Test
    fun `signing produces an event that verifies and carries our tags`() {
        val sk = Hex.decode("b7e151628aed2a6abf7158809cf4f3c762e7160f38b4da56a784d9045190cfef")
        val event = NostrEvent.signed(
            secretKey = sk,
            kind = Kind.CHANNEL_MESSAGE,
            content = "Testaufgabe mit Ümläuten\n- Punkt",
            tags = listOf(
                listOf("h", "ccd1bdf8-54b1-44ae-abee-b372b82b5196"),
                listOf("t", "deck-task"),
                listOf("status", "open"),
            ),
            createdAt = 1785880559,
        )
        assertTrue(event.verify())
        assertEquals("ccd1bdf8-54b1-44ae-abee-b372b82b5196", event.channelId)
        assertEquals("deck-task", event.tag("t"))
        assertEquals("open", event.tag("status"))
    }

    @Test
    fun `thread markers are read from position three`() {
        val event = NostrEvent(
            id = "", pubkey = "", createdAt = 0, kind = 9,
            tags = listOf(
                listOf("h", "chan"),
                listOf("e", "rootid", "", "root"),
                listOf("e", "parentid", "", "reply"),
                listOf("p", "somepubkey"),
            ),
            content = "", sig = "",
        )
        assertEquals("rootid", event.threadRoot)
        assertEquals("parentid", event.replyTo)
        assertEquals(listOf("somepubkey"), event.tagValues("p"))
    }

    @Test
    fun `a real channel metadata event exposes its name`() {
        val channels = realEvents().filter { it.kind == Kind.CHANNEL_METADATA }
        assertTrue("fixture has no channel metadata", channels.isNotEmpty())
        assertNotNull(channels.first().tag("d"))
    }
}
