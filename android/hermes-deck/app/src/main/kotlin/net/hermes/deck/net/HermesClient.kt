package net.hermes.deck.net

import java.io.IOException
import java.util.concurrent.TimeUnit
import net.hermes.deck.model.BuzzAgent
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
    }
}
