package net.hermes.dictate

import java.io.IOException
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Diktat Stufe 9 — Wörterbuch-Sync. Response shapes mirror the LIVE server contract in
 * `hermes_cli/web_server.py` (`_dictate_personalization_response`, `put_dictate_personalization`,
 * 2026-07-16): `{schema, exists, dictionary_rules, snippet_rules, revision, updated_at, updated_by}`
 * on both GET-200 and PUT-200/409.
 */
class PersonalizationSyncTest {

    /**
     * Method-pinned: the server registers this endpoint as `@app.put` (hermes_cli/web_server.py),
     * never `@app.post` — `post(...)` throws so a regression back to POST fails the test instead
     * of silently taking a live 405.
     */
    private class FakeTransport : HttpTransport {
        val getResponses = ArrayDeque<HttpResponse>()
        val putResponses = ArrayDeque<HttpResponse>()
        val putBodies = mutableListOf<String>()
        var getError: IOException? = null
        var putError: IOException? = null

        override fun get(
            url: String,
            headers: Map<String, String>,
            connectTimeoutMs: Int,
            readTimeoutMs: Int,
        ): HttpResponse {
            getError?.let { throw it }
            return getResponses.removeFirst()
        }

        override fun put(
            url: String,
            headers: Map<String, String>,
            body: ByteArray,
            connectTimeoutMs: Int,
            readTimeoutMs: Int,
        ): HttpResponse {
            putError?.let { throw it }
            putBodies += body.toString(Charsets.UTF_8)
            return putResponses.removeFirst()
        }

        override fun post(
            url: String,
            headers: Map<String, String>,
            body: ByteArray,
            connectTimeoutMs: Int,
            readTimeoutMs: Int,
        ): HttpResponse = throw AssertionError("PersonalizationSync must PUT, never POST (server is @app.put)")
    }

    private object NoCookies : SessionCookieStore {
        override fun cookieHeader(url: String): String? = null
        override fun storeResponseCookies(url: String, setCookies: List<String>) = Unit
    }

    private val url = DictateConfig.PERSONALIZATION_URL

    /** The server's real response shape (GET-200 or PUT-200/409), built via JSONObject like the client itself. */
    private fun documentJson(dictionaryRules: String, snippetRules: String, revision: Int, exists: Boolean = true): String =
        JSONObject()
            .put("schema", "hermes-dictate-personalization-v1")
            .put("exists", exists)
            .put("dictionary_rules", dictionaryRules)
            .put("snippet_rules", snippetRules)
            .put("revision", revision)
            .put("updated_at", "2026-07-16T10:00:00+00:00")
            .put("updated_by", "dashboard")
            .toString()

    // --- Pull decision table (pure, no I/O) ---

    @Test
    fun `seeds the server when it has no document and local is not empty`() {
        val local = LocalPersonalizationState("piet => Piet", "", syncLastRevision = 0, syncLastFingerprint = "")
        val remote = PersonalizationDocument(exists = false, dictionaryRules = "", snippetRules = "", revision = 0)
        assertEquals(PullDecision.SeedPush, PersonalizationSyncDecision.decide(local, remote))
    }

    @Test
    fun `no server document and empty local is a no-op`() {
        val local = LocalPersonalizationState("", "", syncLastRevision = 0, syncLastFingerprint = "")
        val remote = PersonalizationDocument(exists = false, dictionaryRules = "", snippetRules = "", revision = 0)
        assertEquals(PullDecision.NoOp, PersonalizationSyncDecision.decide(local, remote))
    }

    @Test
    fun `unchanged revision is a no-op`() {
        val local = LocalPersonalizationState("piet => Piet", "", syncLastRevision = 3, syncLastFingerprint = "stale")
        val remote = PersonalizationDocument(exists = true, dictionaryRules = "piet => Piet", snippetRules = "", revision = 3)
        assertEquals(PullDecision.NoOp, PersonalizationSyncDecision.decide(local, remote))
    }

    @Test
    fun `server-newer and local-unchanged adopts the server text 1-to-1`() {
        val local = LocalPersonalizationState(
            dictionaryRules = "piet => Piet",
            snippetRules = "signatur => Gruss",
            syncLastRevision = 2,
            syncLastFingerprint = PersonalizationFingerprint.of("piet => Piet", "signatur => Gruss"),
        )
        val remote = PersonalizationDocument(
            exists = true,
            dictionaryRules = "piet => Piet\nhermes => Hermes",
            snippetRules = "signatur => Gruss",
            revision = 3,
        )
        assertEquals(PullDecision.Adopt(remote), PersonalizationSyncDecision.decide(local, remote))
    }

    @Test
    fun `dashboard deletion is adopted when local is unchanged, even though the server is now empty`() {
        val local = LocalPersonalizationState(
            dictionaryRules = "piet => Piet",
            snippetRules = "",
            syncLastRevision = 5,
            syncLastFingerprint = PersonalizationFingerprint.of("piet => Piet", ""),
        )
        val remote = PersonalizationDocument(exists = true, dictionaryRules = "", snippetRules = "", revision = 6)
        assertEquals(PullDecision.Adopt(remote), PersonalizationSyncDecision.decide(local, remote))
    }

    @Test
    fun `both sides changed triggers a union-merge with local winning, pushed at the server revision`() {
        val local = LocalPersonalizationState(
            dictionaryRules = "piet => Piet\nhuebner => Hübner",
            snippetRules = "",
            // Marker reflects an OLDER local stand — the current local text has since changed.
            syncLastRevision = 1,
            syncLastFingerprint = PersonalizationFingerprint.of("piet => Piet", ""),
        )
        val remote = PersonalizationDocument(
            exists = true,
            dictionaryRules = "piet => PIET FALSCH\nkatze => Katze",
            snippetRules = "",
            revision = 2,
        )
        val decision = PersonalizationSyncDecision.decide(local, remote) as PullDecision.MergeAndPush
        assertEquals("piet => Piet\nhuebner => Hübner\nkatze => Katze", decision.dictionaryRules)
        assertEquals(2, decision.baseRevision)
    }

    // --- Fingerprint separator (contract Nachschaerfung F2) ---

    @Test
    fun `a field-boundary shift never collides two different dictionary-snippet pairs`() {
        // Both pairs concatenate to the exact same text under a plain-space separator.
        val first = PersonalizationFingerprint.of("a => A", "ignored b => B")
        val second = PersonalizationFingerprint.of("a => A ignored", "b => B")
        assertTrue(first != second)
    }

    // --- Union-merge (pure, real rule-line format: Umlaute, #-Kommentar, Snippet-\n) ---

    @Test
    fun `merge keeps local order and comments, local wins per key, server-exclusive line is appended`() {
        val local = "# Namen\npiet => Piet\nhuebner => Hübner"
        val server = "piet => PIET FALSCH\nkatze => Katze"
        assertEquals(
            "# Namen\npiet => Piet\nhuebner => Hübner\nkatze => Katze",
            PersonalizationRuleMerge.merge(local, server),
        )
    }

    @Test
    fun `merge preserves a snippet's literal newline escape and it still expands correctly afterwards`() {
        val local = "# Signaturen"
        val server = "signatur => Viele Grüße\\nPiet Hübner"
        val merged = PersonalizationRuleMerge.merge(local, server)
        assertEquals("# Signaturen\nsignatur => Viele Grüße\\nPiet Hübner", merged)
        assertEquals("Viele Grüße\nPiet Hübner", TextPersonalizer.expandSnippet("signatur", merged))
    }

    @Test
    fun `merging two identical stands is idempotent`() {
        val text = "# Namen\npiet => Piet\nhuebner => Hübner"
        assertEquals(text, PersonalizationRuleMerge.merge(text, text))
    }

    @Test
    fun `merge stays within the 250-rule cap, local base always wins the budget`() {
        val local = (1..200).joinToString("\n") { "local$it => Local$it" }
        val server = (1..100).joinToString("\n") { "server$it => Server$it" }

        val merged = PersonalizationRuleMerge.merge(local, server)
        val mergedRules = TextPersonalizer.parse(merged)

        assertEquals(250, mergedRules.size)
        val mergedKeys = mergedRules.map { it.spoken.lowercase() }.toSet()
        (1..200).forEach { assertTrue(mergedKeys.contains("local$it")) }
    }

    // --- CAS guard (contract Nachschaerfung F1, pure decision) ---

    @Test
    fun `CAS unchanged is false the moment either field diverges from the decision snapshot`() {
        assertTrue(PersonalizationCas.unchanged("a => A", "", "a => A", ""))
        assertFalse(PersonalizationCas.unchanged("a => A", "", "a => A\nb => B", ""))
        assertFalse(PersonalizationCas.unchanged("a => A", "", "a => A", "s => S"))
    }

    // --- Pull throttle (pure) ---

    @Test
    fun `pull throttle blocks a second attempt inside 5 minutes and allows it after`() {
        val last = 1_000_000L
        assertFalse(PersonalizationPullThrottle.shouldAttempt(last, last + 1))
        assertFalse(PersonalizationPullThrottle.shouldAttempt(last, last + 5 * 60_000L - 1))
        assertTrue(PersonalizationPullThrottle.shouldAttempt(last, last + 5 * 60_000L))
    }

    // --- I/O: pull() over the fake transport ---

    @Test
    fun `pull adopts the real server response shape, including a dashboard-emptied document`() {
        val transport = FakeTransport()
        transport.getResponses += HttpResponse(200, documentJson("", "", revision = 6), emptyList())
        val sync = PersonalizationSync(url, NoCookies, transport)
        val local = LocalPersonalizationState(
            dictionaryRules = "piet => Piet",
            snippetRules = "",
            syncLastRevision = 5,
            syncLastFingerprint = PersonalizationFingerprint.of("piet => Piet", ""),
        )

        val outcome = sync.pull(local)

        assertEquals("", outcome?.dictionaryRules)
        assertEquals(6, outcome?.revision)
        assertTrue(transport.putBodies.isEmpty())
    }

    @Test
    fun `pull network failure is a no-op, never throws`() {
        val transport = FakeTransport().apply { getError = IOException("offline") }
        val sync = PersonalizationSync(url, NoCookies, transport)
        assertNull(sync.pull(LocalPersonalizationState("piet => Piet", "", 0, "")))
    }

    // --- Strict parse (contract Nachschaerfung F3): fail-open opt* defaulting is forbidden ---

    @Test
    fun `a v2-schema response is a no-op, never a fresh-defaulted empty document`() {
        val v2Body = JSONObject()
            .put("schema", "hermes-dictate-personalization-v2")
            .put("exists", true)
            .put("dictionary_rules", "")
            .put("snippet_rules", "")
            .put("revision", 1)
            .toString()
        val transport = FakeTransport()
        transport.getResponses += HttpResponse(200, v2Body, emptyList())
        val sync = PersonalizationSync(url, NoCookies, transport)

        assertNull(sync.pull(LocalPersonalizationState("piet => Piet", "", syncLastRevision = 0, syncLastFingerprint = "")))
        assertTrue(transport.putBodies.isEmpty())
    }

    @Test
    fun `a response without the rules fields is a no-op, never defaulted to empty strings`() {
        val bareBody = JSONObject()
            .put("schema", "hermes-dictate-personalization-v1")
            .put("exists", true)
            .put("revision", 1)
            .toString()
        val transport = FakeTransport()
        transport.getResponses += HttpResponse(200, bareBody, emptyList())
        val sync = PersonalizationSync(url, NoCookies, transport)

        assertNull(sync.pull(LocalPersonalizationState("piet => Piet", "", syncLastRevision = 0, syncLastFingerprint = "")))
        assertTrue(transport.putBodies.isEmpty())
    }

    // --- I/O: push() 409 conflict path ---

    @Test
    fun `push conflict merges with the response document and re-pushes exactly once`() {
        val transport = FakeTransport()
        transport.putResponses += HttpResponse(
            409,
            documentJson("piet => Piet\nkatze => Katze", "", revision = 4),
            emptyList(),
        )
        transport.putResponses += HttpResponse(
            200,
            documentJson("piet => Piet\nhuebner => Hübner\nkatze => Katze", "", revision = 5, exists = true),
            emptyList(),
        )
        val sync = PersonalizationSync(url, NoCookies, transport)
        val local = LocalPersonalizationState(
            dictionaryRules = "piet => Piet\nhuebner => Hübner",
            snippetRules = "",
            syncLastRevision = 3,
            syncLastFingerprint = "irrelevant-for-push",
        )

        val outcome = sync.push(local)

        assertEquals(2, transport.putBodies.size)
        assertEquals(3, JSONObject(transport.putBodies[0]).getInt("base_revision"))
        assertEquals(4, JSONObject(transport.putBodies[1]).getInt("base_revision"))
        assertEquals(
            "piet => Piet\nhuebner => Hübner\nkatze => Katze",
            JSONObject(transport.putBodies[1]).getString("dictionary_rules"),
        )
        assertEquals("piet => Piet\nhuebner => Hübner\nkatze => Katze", outcome?.dictionaryRules)
        assertEquals(5, outcome?.revision)
    }

    @Test
    fun `a second conflict gives up without a third push`() {
        val transport = FakeTransport()
        transport.putResponses += HttpResponse(409, documentJson("a => A", "", revision = 2), emptyList())
        transport.putResponses += HttpResponse(409, documentJson("a => A", "", revision = 3), emptyList())
        val sync = PersonalizationSync(url, NoCookies, transport)
        val local = LocalPersonalizationState("b => B", "", syncLastRevision = 1, syncLastFingerprint = "x")

        val outcome = sync.push(local)

        assertEquals(2, transport.putBodies.size)
        assertNull(outcome)
    }

    @Test
    fun `push network failure is a no-op, never throws`() {
        val transport = FakeTransport().apply { putError = IOException("offline") }
        val sync = PersonalizationSync(url, NoCookies, transport)
        assertNull(sync.push(LocalPersonalizationState("a => A", "", 1, "x")))
    }

    @Test
    fun `push request matches the server contract`() {
        val transport = FakeTransport()
        transport.putResponses += HttpResponse(200, documentJson("a => A", "", revision = 2), emptyList())
        val sync = PersonalizationSync(url, NoCookies, transport)
        sync.push(LocalPersonalizationState("a => A", "", syncLastRevision = 1, syncLastFingerprint = "x"))

        val body = JSONObject(transport.putBodies.single())
        assertEquals("a => A", body.getString("dictionary_rules"))
        assertEquals("", body.getString("snippet_rules"))
        assertEquals(1, body.getInt("base_revision"))
        assertEquals("app", body.getString("source"))
    }
}
