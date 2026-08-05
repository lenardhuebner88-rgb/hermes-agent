package net.hermes.deck.net

import net.hermes.deck.model.BuzzAgent
import org.json.JSONArray
import org.json.JSONObject

/**
 * Reads the dashboard's Buzz model-control payloads.
 *
 * Split out of [HermesClient] because every one of these field names is a
 * contract with `hermes_cli/buzz_model_control.py`, and the first live run
 * found five of them wrong at once — the models list holds objects, not
 * strings; the current model is `current_model`, not `model`; `error` is an
 * object, not a string; and a model change reports `old_model`/`new_model`.
 * None of that could fail in a way the UI showed: the list simply stayed
 * empty. Here it is plain JSON-in, data-out, so JVM tests pin it against
 * recorded real responses.
 */
object HermesPayloads {

    data class ModelChoices(val models: List<String>, val current: String?, val error: String?)

    /**
     * `/api/buzz/agents`, whole. The windows travel with the payload so the
     * card can say "in 1 Stunde" without a second copy of the number that would
     * drift the moment the server's window is retuned.
     */
    data class AgentsSnapshot(
        val agents: List<BuzzAgent>,
        val windowSeconds: Int,
        val recentSeconds: Int,
        val heartbeatError: String?,
    )

    fun agentsSnapshot(json: JSONObject): AgentsSnapshot {
        val array = json.optJSONArray("agents") ?: JSONArray()
        return AgentsSnapshot(
            agents = (0 until array.length()).mapNotNull {
                array.optJSONObject(it)?.let(BuzzAgent::fromJson)
            },
            // Defaults match the server's, but only ever apply when the key is
            // missing — an older dashboard, not a silent disagreement.
            windowSeconds = json.optInt("heartbeat_window_seconds", 3600),
            recentSeconds = json.optInt("heartbeat_recent_seconds", 300),
            heartbeatError = explainHeartbeat(json.optString("heartbeat_error", "")),
        )
    }

    /**
     * A heartbeat failure must never be shown as calm. Zero tool calls and an
     * unreadable journal produce the same numbers, so this sentence is the only
     * thing standing between the operator and a reassuring, wrong grey dot.
     */
    private fun explainHeartbeat(code: String): String? = when {
        code.isBlank() || code == "null" -> null
        code == "journal_unavailable" ->
            "Aktivität nicht messbar — das Journal liess sich nicht lesen."
        else -> "Aktivität nicht messbar ($code)."
    }

    data class ModelChange(
        val previous: String,
        val current: String,
        val restarted: Boolean,
        val ready: Boolean,
        val unverified: Boolean,
        val error: String?,
    )

    /** `{"stem":…,"current_model":…,"models":[{"value":…,"name":…}]|null,"error":{…}|null}` */
    fun modelChoices(json: JSONObject): ModelChoices {
        val array = json.optJSONArray("models")
        val models = buildList {
            if (array != null) {
                for (i in 0 until array.length()) {
                    // Tolerate both shapes: the endpoint sends objects, but a
                    // bare string list is the obvious future simplification and
                    // must not silently produce `{"value":…}` entries.
                    val entry = array.opt(i)
                    val value = when (entry) {
                        is JSONObject -> entry.optString("value", "")
                        is String -> entry
                        else -> ""
                    }
                    if (value.isNotBlank()) add(value)
                }
            }
        }
        return ModelChoices(
            models = models,
            current = json.optString("current_model", "").ifBlank { null },
            error = readError(json.opt("error")),
        )
    }

    /** `{"old_model":…,"new_model":…,"restart":{…},"unverified":bool}` */
    fun modelChange(json: JSONObject, requested: String): ModelChange {
        val restart = json.optJSONObject("restart")
        return ModelChange(
            previous = json.optString("old_model", ""),
            current = json.optString("new_model", requested),
            restarted = restart?.optBoolean("restarted", false) ?: false,
            ready = restart?.optBoolean("ready", false) ?: false,
            unverified = json.optBoolean("unverified", false),
            error = restart?.optString("error", "")?.ifBlank { null },
        )
    }

    /**
     * Turns the endpoint's `{"code":…,"detail":…}` into something worth
     * showing. Printing the raw object — which is what happened on the first
     * live run — puts JSON braces in front of the user and hides the sentence
     * that actually explains the failure.
     */
    fun readError(value: Any?): String? = when (value) {
        null, JSONObject.NULL -> null
        is String -> value.ifBlank { null }
        is JSONObject -> {
            val detail = value.optString("detail", "")
            val code = value.optString("code", "")
            when {
                detail.isNotBlank() -> explain(code, detail)
                code.isNotBlank() -> explain(code, code)
                else -> null
            }
        }
        else -> value.toString().ifBlank { null }
    }

    /** Known failure codes get a remedy; unknown ones keep the raw detail. */
    private fun explain(code: String, detail: String): String = when (code) {
        "models_probe_failed" ->
            "Modell-Liste nicht abrufbar — der Agent liess sich nicht starten ($detail)."
        "models_probe_unparseable" ->
            "Der Agent meldet keine Modell-Auswahl — Modell lässt sich trotzdem setzen."
        "agent_not_found" -> "Diesen Agenten gibt es auf dem Server nicht."
        else -> detail
    }

    /**
     * What an intervention endpoint said it did.
     *
     * These routes answer in at least three shapes — a bare `{"ok":true}`, a
     * `{"detail":…}`, and a body describing what was terminated — and the one
     * thing the operator must never see is a silent success. So an unrecognised
     * shape falls through to a sentence that admits the ambiguity rather than
     * to "Erledigt".
     */
    fun actionOutcome(json: JSONObject): String {
        readError(json.opt("error"))?.let { return it }
        val detail = json.optString("detail", "").ifBlank { null }
        val message = json.optString("message", "").ifBlank { null }
        return when {
            message != null -> message
            detail != null -> detail
            json.optBoolean("ok", false) -> "Vom Server bestätigt."
            json.optBoolean("terminated", false) -> "Lauf beendet."
            json.optBoolean("released", false) -> "Freigabe gelöst."
            json.has("cancelled") -> "Kette abgebrochen (${json.optInt("cancelled")} Karten)."
            else -> "Angenommen — der Server hat nichts weiter gemeldet."
        }
    }
}
