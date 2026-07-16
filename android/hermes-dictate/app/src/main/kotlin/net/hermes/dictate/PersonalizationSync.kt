package net.hermes.dictate

import java.io.IOException
import java.security.MessageDigest
import org.json.JSONObject

/**
 * Wire document for `GET`/`PUT /api/dictate/personalization` (schema
 * `hermes-dictate-personalization-v1`). `schema`/`updated_at`/`updated_by` are not modeled here —
 * the sync decision never needs them.
 */
data class PersonalizationDocument(
    val exists: Boolean,
    val dictionaryRules: String,
    val snippetRules: String,
    val revision: Int,
)

/** Local sync-relevant state, decoupled from [DictatePrefs]/Android so the decision table is a pure function. */
data class LocalPersonalizationState(
    val dictionaryRules: String,
    val snippetRules: String,
    val syncLastRevision: Int,
    val syncLastFingerprint: String,
)

/** SHA-256 fingerprint of a local dictionary+snippet pair, per the App-Sync-Kontrakt. */
object PersonalizationFingerprint {
    fun of(dictionaryRules: String, snippetRules: String): String {
        // NUL separator (contract Nachschaerfung F2), written as an explicit Kotlin escape
        // sequence — a plain space (or any character that can occur inside a rule line) lets a
        // field-boundary shift collide two different (dictionary, snippet) pairs on the same hash.
        val bytes = MessageDigest.getInstance("SHA-256")
            .digest((dictionaryRules + "\u0000" + snippetRules).toByteArray(Charsets.UTF_8))
        return bytes.joinToString("") { "%02x".format(it) }
    }
}

/** Outcome of the pull decision table (contract "Pull-Entscheidung"). */
sealed class PullDecision {
    /** `revision == sync_last_revision`: nothing to do. */
    object NoOp : PullDecision()

    /** `exists:false` and local not entirely empty: seed the server with the local text. */
    object SeedPush : PullDecision()

    /** Server is newer and local is unchanged since the last sync: adopt the server 1:1. */
    data class Adopt(val document: PersonalizationDocument) : PullDecision()

    /** Both sides changed: union-merge (local wins per key), then push with the server revision. */
    data class MergeAndPush(val dictionaryRules: String, val snippetRules: String, val baseRevision: Int) :
        PullDecision()
}

/** The deterministic pull decision table from the App-Sync-Kontrakt — no I/O, no Android. */
object PersonalizationSyncDecision {
    fun decide(local: LocalPersonalizationState, remote: PersonalizationDocument): PullDecision {
        if (!remote.exists) {
            val localNonEmpty = local.dictionaryRules.isNotBlank() || local.snippetRules.isNotBlank()
            return if (localNonEmpty) PullDecision.SeedPush else PullDecision.NoOp
        }
        if (remote.revision <= local.syncLastRevision) return PullDecision.NoOp
        val localFingerprint = PersonalizationFingerprint.of(local.dictionaryRules, local.snippetRules)
        if (localFingerprint == local.syncLastFingerprint) return PullDecision.Adopt(remote)
        return PullDecision.MergeAndPush(
            dictionaryRules = PersonalizationRuleMerge.merge(local.dictionaryRules, remote.dictionaryRules),
            snippetRules = PersonalizationRuleMerge.merge(local.snippetRules, remote.snippetRules),
            baseRevision = remote.revision,
        )
    }
}

/** Union-merge of one rule field (dictionary or snippets), per the contract's conflict rule. */
object PersonalizationRuleMerge {
    /** Mirrors TextPersonalizer's private MAX_RULES: the server enforces the same 250-rule cap
     * per field, so a merge that pushed past it would just 400 forever (contract Nachschaerfung F6). */
    private const val MAX_MERGED_RULES = 250

    /**
     * Local text is the base: its line order and comments are untouched, and it wins per key
     * (`trigger.lowercase()`) on any conflict. Rule lines that exist only on the server side are
     * appended at the end, reconstructed as `trigger => replacement`, up to the combined 250-rule
     * cap — local rules always have priority, server-exclusive overflow is discarded rather than
     * producing an unpushable (400) merge. Merging against an identical server field is
     * idempotent (nothing to append).
     */
    fun merge(localText: String, serverText: String): String {
        val localRules = TextPersonalizer.parse(localText)
        val localKeys = localRules.map { it.spoken.lowercase() }.toSet()
        val capacity = (MAX_MERGED_RULES - localRules.size).coerceAtLeast(0)
        val serverExclusive = TextPersonalizer.parse(serverText)
            .filter { it.spoken.lowercase() !in localKeys }
            .take(capacity)
        if (serverExclusive.isEmpty()) return localText
        val appended = serverExclusive.joinToString("\n") { "${it.spoken} => ${it.written}" }
        val base = localText.trimEnd('\n')
        return if (base.isEmpty()) appended else "$base\n$appended"
    }
}

/** Throttle for the IME/overlay pull hooks: at most one attempt per [MIN_INTERVAL_MS]. */
object PersonalizationPullThrottle {
    private const val MIN_INTERVAL_MS = 5 * 60 * 1000L

    fun shouldAttempt(lastAttemptAtMs: Long, nowMs: Long): Boolean = nowMs - lastAttemptAtMs >= MIN_INTERVAL_MS
}

/** Result of a successful pull or push: the caller applies this to [DictatePrefs]. */
data class PersonalizationSyncOutcome(val dictionaryRules: String, val snippetRules: String, val revision: Int) {
    val fingerprint: String get() = PersonalizationFingerprint.of(dictionaryRules, snippetRules)
}

/**
 * Client for `GET`/`PUT /api/dictate/personalization` (Diktat Stufe 9). Follows the same seam
 * and cookie-handling pattern as [DictateStatusReporter]/[CloudTranscriber]; every network/parse
 * failure resolves to `null` — the caller's existing local state is the fallback, sync is never
 * allowed to block dictation or the settings UI (fail-closed).
 */
class PersonalizationSync(
    private val url: String,
    private val cookies: SessionCookieStore,
    private val transport: HttpTransport,
) {
    /**
     * Runs the pull decision table against the live server document. `null` means "no change" —
     * either a genuine no-op, or a network/parse failure the caller should ignore.
     */
    fun pull(local: LocalPersonalizationState): PersonalizationSyncOutcome? {
        val remote = fetchDocument() ?: return null
        return when (val decision = PersonalizationSyncDecision.decide(local, remote)) {
            PullDecision.NoOp -> null
            PullDecision.SeedPush ->
                pushWithConflictRetry(local.dictionaryRules, local.snippetRules, baseRevision = 0)?.toOutcome()
            is PullDecision.Adopt -> decision.document.toOutcome()
            is PullDecision.MergeAndPush ->
                pushWithConflictRetry(decision.dictionaryRules, decision.snippetRules, decision.baseRevision)
                    ?.toOutcome()
        }
    }

    /**
     * Push-trigger after a local edit: `base_revision = local.syncLastRevision`. On a 409, merges
     * with the response document (local wins) and re-pushes exactly once; a second 409 gives up
     * until the next trigger (no retry loop).
     */
    fun push(local: LocalPersonalizationState): PersonalizationSyncOutcome? =
        pushWithConflictRetry(local.dictionaryRules, local.snippetRules, local.syncLastRevision)?.toOutcome()

    private fun fetchDocument(): PersonalizationDocument? {
        val headers = buildMap {
            put("Accept", "application/json")
            cookies.cookieHeader(url)?.let { put("Cookie", it) }
        }
        val response = try {
            transport.get(url, headers, CONNECT_TIMEOUT_MS, READ_TIMEOUT_MS)
        } catch (_: IOException) {
            return null
        }
        if (response.setCookies.isNotEmpty()) cookies.storeResponseCookies(url, response.setCookies)
        if (response.status != 200) return null
        return parseDocument(response.body)
    }

    private fun pushWithConflictRetry(
        dictionaryRules: String,
        snippetRules: String,
        baseRevision: Int,
    ): PersonalizationDocument? {
        val first = pushRaw(dictionaryRules, snippetRules, baseRevision) ?: return null
        if (first.status == 200) return parseDocument(first.body)
        if (first.status != 409) return null
        val conflict = parseDocument(first.body) ?: return null
        val mergedDictionary = PersonalizationRuleMerge.merge(dictionaryRules, conflict.dictionaryRules)
        val mergedSnippets = PersonalizationRuleMerge.merge(snippetRules, conflict.snippetRules)
        val second = pushRaw(mergedDictionary, mergedSnippets, conflict.revision) ?: return null
        if (second.status != 200) return null
        return parseDocument(second.body)
    }

    private fun pushRaw(dictionaryRules: String, snippetRules: String, baseRevision: Int): HttpResponse? {
        val body = JSONObject()
            .put("dictionary_rules", dictionaryRules)
            .put("snippet_rules", snippetRules)
            .put("base_revision", baseRevision)
            .put("source", "app")
            .toString()
            .toByteArray(Charsets.UTF_8)
        val headers = buildMap {
            put("Content-Type", "application/json")
            put("Accept", "application/json")
            cookies.cookieHeader(url)?.let { put("Cookie", it) }
        }
        return try {
            // The server registers this endpoint as `@app.put` (hermes_cli/web_server.py) — POST
            // would 405.
            transport.put(url, headers, body, CONNECT_TIMEOUT_MS, READ_TIMEOUT_MS).also {
                if (it.setCookies.isNotEmpty()) cookies.storeResponseCookies(url, it.setCookies)
            }
        } catch (_: IOException) {
            null
        }
    }

    /**
     * Strict parse (contract Nachschaerfung F3): `opt*` defaulting a missing field would turn a
     * v2-schema or foreign response into a "valid empty document" that can wipe the local state.
     * `schema` must match exactly and every required field must be present with the exact
     * expected type — any mismatch is a parse failure (No-op), never a silent default.
     */
    private fun parseDocument(body: String): PersonalizationDocument? {
        return try {
            val json = JSONObject(body)
            val required = listOf("schema", "exists", "dictionary_rules", "snippet_rules", "revision")
            if (required.any { !json.has(it) }) return null
            if (json.get("schema") != SCHEMA) return null
            val exists = json.get("exists") as? Boolean ?: return null
            val dictionaryRules = json.get("dictionary_rules") as? String ?: return null
            val snippetRules = json.get("snippet_rules") as? String ?: return null
            val revision = json.get("revision") as? Int ?: return null
            PersonalizationDocument(exists, dictionaryRules, snippetRules, revision)
        } catch (_: Exception) {
            null
        }
    }

    private fun PersonalizationDocument.toOutcome() =
        PersonalizationSyncOutcome(dictionaryRules, snippetRules, revision)

    companion object {
        private const val CONNECT_TIMEOUT_MS = 5_000
        private const val READ_TIMEOUT_MS = 5_000
        private const val SCHEMA = "hermes-dictate-personalization-v1"
    }
}

/**
 * The compare-and-swap check behind contract Nachschaerfung F1: whether the live local state
 * still exactly matches the snapshot a sync decision was computed against. `false` means an edit
 * raced the async sync and the outcome must be discarded wholesale rather than clobbering it.
 */
object PersonalizationCas {
    fun unchanged(
        snapshotDictionaryRules: String,
        snapshotSnippetRules: String,
        currentDictionaryRules: String,
        currentSnippetRules: String,
    ): Boolean =
        currentDictionaryRules == snapshotDictionaryRules && currentSnippetRules == snapshotSnippetRules
}
