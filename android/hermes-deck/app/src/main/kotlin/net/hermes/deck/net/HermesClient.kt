package net.hermes.deck.net

import java.io.IOException
import java.util.concurrent.TimeUnit
import net.hermes.deck.model.AccountUsage
import net.hermes.deck.model.BuzzAgent
import net.hermes.deck.model.DeckPulse
import okhttp3.Cookie
import okhttp3.CookieJar
import okhttp3.HttpUrl
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONArray
import org.json.JSONObject

/**
 * Talks to the Hermes dashboard's Buzz model-control endpoints.
 *
 * The dashboard authenticates native clients with the same session cookie the
 * browser login mints — there is no API token for these routes — so this client
 * keeps a cookie jar and re-logs in when a call comes back 401. Requests are
 * pinned to the configured origin: a redirect to somewhere else would otherwise
 * carry the session cookie off-host.
 */
class HermesClient(
    private val baseUrl: String,
    private val username: String,
    private val password: String,
) {

    class NotAuthorised(message: String) : IOException(message)
    class Unreachable(message: String) : IOException(message)

    private val cookies = mutableMapOf<String, MutableList<Cookie>>()

    private val http: OkHttpClient = OkHttpClient.Builder()
        .connectTimeout(15, TimeUnit.SECONDS)
        .readTimeout(60, TimeUnit.SECONDS)
        // Following a redirect would replay the session cookie against whatever
        // host it points at; the dashboard never legitimately needs one here.
        .followRedirects(false)
        .followSslRedirects(false)
        .cookieJar(object : CookieJar {
            override fun saveFromResponse(url: HttpUrl, cookieList: List<Cookie>) {
                cookies.getOrPut(url.host) { mutableListOf() }.apply {
                    cookieList.forEach { fresh -> removeAll { it.name == fresh.name } }
                    addAll(cookieList)
                }
            }

            override fun loadForRequest(url: HttpUrl): List<Cookie> =
                cookies[url.host]?.filter { it.expiresAt > System.currentTimeMillis() } ?: emptyList()
        })
        .build()

    val isConfigured: Boolean
        get() = baseUrl.isNotBlank() && username.isNotBlank() && password.isNotBlank()

    fun login() {
        val body = JSONObject().apply {
            put("provider", "basic")
            put("username", username)
            put("password", password)
            put("next", "")
        }
        val request = Request.Builder()
            .url("${baseUrl.trimEnd('/')}/auth/password-login")
            .post(body.toString().toRequestBody(JSON))
            .build()
        http.newCall(request).execute().use { response ->
            // A 302 counts as success here: the dashboard redirects after minting
            // the cookie, and redirects are deliberately not followed.
            if (response.code !in 200..399) {
                throw NotAuthorised("Anmeldung am Dashboard fehlgeschlagen (HTTP ${response.code})")
            }
        }
    }

    fun agents(): HermesPayloads.AgentsSnapshot {
        val json = getJson("/api/buzz/agents")
        if (!json.has("agents")) {
            // Older dashboards answered with a bare array, which `getJson`
            // parks under `_raw`; it carries no heartbeat, and saying so beats
            // rendering every agent as quiet.
            val array = JSONArray(json.optString("_raw", "[]"))
            return HermesPayloads.AgentsSnapshot(
                agents = (0 until array.length()).mapNotNull {
                    array.optJSONObject(it)?.let(BuzzAgent::fromJson)
                },
                windowSeconds = 3600,
                recentSeconds = 300,
                heartbeatError = "Das Dashboard meldet noch keine Aktivität.",
            )
        }
        return HermesPayloads.agentsSnapshot(json)
    }

    /** Subscription budgets per provider; empty when the dashboard reports none. */
    fun accountUsage(): List<AccountUsage> =
        AccountUsage.listFromJson(getJson("/api/account-usage"))

    /**
     * Everything the live screens poll, in one request.
     *
     * `receivedAt` is stamped here, at the moment the bytes land, and not in the
     * view model: the freshness the operator sees has to be the age of the data,
     * not the age of the last recomposition.
     */
    fun pulse(limit: Int = 12, board: String? = null): DeckPulse {
        val query = buildString {
            append("/api/deck/pulse?limit=").append(limit)
            if (!board.isNullOrBlank()) append("&board=").append(board)
        }
        return DeckPulse.fromJson(getJson(query), receivedAt = System.currentTimeMillis())
    }

    /**
     * The three interventions the operator can make from the phone.
     *
     * They live behind the Kanban plugin's own prefix rather than the
     * dashboard's own namespace — plugin backends mount under
     * `/api/plugins/<name>/`, and a call to the bare path returns the SPA's
     * index.html with a cheerful 200, which would read as success.
     */
    fun terminateRun(runId: Long): String = postForMessage("$KANBAN/runs/$runId/terminate")

    fun cancelChain(taskId: String): String = postForMessage("$KANBAN/tasks/$taskId/cancel-chain")

    fun releaseGate(taskId: String): String = postForMessage("$KANBAN/tasks/$taskId/flow-release")

    /**
     * Posts and returns what the server said about it.
     *
     * Deliberately not optimistic: these calls stop real work, and a button that
     * reports success before the server has agreed is how an operator ends up
     * believing a runaway run was killed when it was not.
     */
    private fun postForMessage(path: String): String {
        val request = Request.Builder()
            .url("${baseUrl.trimEnd('/')}$path")
            .post(EMPTY_JSON.toRequestBody(JSON))
            .build()
        val json = executeJson(request, retryOnUnauthorised = true)
        return HermesPayloads.actionOutcome(json)
    }

    fun availableModels(stem: String): HermesPayloads.ModelChoices =
        HermesPayloads.modelChoices(getJson("/api/buzz/agents/$stem/models"))

    fun setModel(stem: String, model: String): HermesPayloads.ModelChange {
        val payload = JSONObject().put("model", model)
        val request = Request.Builder()
            .url("${baseUrl.trimEnd('/')}/api/buzz/agents/$stem/model")
            .post(payload.toString().toRequestBody(JSON))
            .build()
        return HermesPayloads.modelChange(executeJson(request, retryOnUnauthorised = true), model)
    }

    private fun getJson(path: String): JSONObject {
        val request = Request.Builder().url("${baseUrl.trimEnd('/')}$path").get().build()
        return executeJson(request, retryOnUnauthorised = true)
    }

    private fun executeJson(request: Request, retryOnUnauthorised: Boolean): JSONObject {
        val response = runCatching { http.newCall(request).execute() }
            .getOrElse { throw Unreachable("Dashboard nicht erreichbar: ${it.message}") }
        response.use {
            if (it.code == 401 || it.code == 403) {
                if (!retryOnUnauthorised) throw NotAuthorised("Sitzung abgelehnt (HTTP ${it.code})")
                login()
                return executeJson(request.newBuilder().build(), retryOnUnauthorised = false)
            }
            if (it.code == 302 || it.code == 303) {
                // The auth gate answers unauthenticated browsers with a redirect
                // to the login page; for us that is a 401 in disguise.
                if (!retryOnUnauthorised) throw NotAuthorised("Nicht angemeldet")
                login()
                return executeJson(request.newBuilder().build(), retryOnUnauthorised = false)
            }
            val text = it.body?.string().orEmpty()
            if (!it.isSuccessful) throw Unreachable("HTTP ${it.code}: ${text.take(200)}")
            val trimmed = text.trim()
            return if (trimmed.startsWith("[")) {
                JSONObject().put("_raw", trimmed)
            } else {
                runCatching { JSONObject(trimmed) }
                    .getOrElse { throw Unreachable("Unerwartete Antwort vom Dashboard") }
            }
        }
    }

    private companion object {
        val JSON = "application/json; charset=utf-8".toMediaType()
        const val EMPTY_JSON = "{}"

        /** Plugin backends mount here; the bare path answers with the SPA. */
        const val KANBAN = "/api/plugins/kanban"
    }
}
