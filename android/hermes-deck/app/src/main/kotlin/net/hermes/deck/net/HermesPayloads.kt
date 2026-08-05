package net.hermes.deck.net

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
}
