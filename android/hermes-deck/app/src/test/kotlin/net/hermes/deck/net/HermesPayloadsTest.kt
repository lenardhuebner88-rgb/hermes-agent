package net.hermes.deck.net

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Pinned against responses recorded from the live dashboard on 2026-08-05 —
 * the run that showed the app and the endpoint had never been tested against
 * each other. Every literal here was copied from a real payload, not invented.
 */
class HermesPayloadsTest {

    @Test
    fun `failed probe becomes a sentence, not raw JSON`() {
        val recorded = JSONObject(
            """{"stem":"claude","current_model":"opus[1m]","models":null,"cached":false,
               "error":{"code":"models_probe_failed","detail":"buzz-acp models exited 1"}}""",
        )
        val choices = HermesPayloads.modelChoices(recorded)
        assertTrue(choices.models.isEmpty())
        assertEquals("opus[1m]", choices.current)
        val error = choices.error!!
        assertTrue("darf kein rohes JSON zeigen: $error", !error.trimStart().startsWith("{"))
        assertTrue(error.contains("nicht abrufbar"))
        assertTrue(error.contains("buzz-acp models exited 1"))
    }

    @Test
    fun `models arrive as objects and are read by value`() {
        val recorded = JSONObject(
            """{"stem":"claude","current_model":"opus[1m]","cached":false,"error":null,
               "models":[{"value":"opus[1m]","name":"Opus 5 (1M)","description":null},
                         {"value":"sonnet","name":"Sonnet 5","description":"schnell"}]}""",
        )
        val choices = HermesPayloads.modelChoices(recorded)
        assertEquals(listOf("opus[1m]", "sonnet"), choices.models)
        assertNull(choices.error)
    }

    @Test
    fun `a bare string list would still work`() {
        val json = JSONObject("""{"models":["a","b"],"current_model":"a"}""")
        assertEquals(listOf("a", "b"), HermesPayloads.modelChoices(json).models)
    }

    @Test
    fun `entries without a value are dropped rather than shown blank`() {
        val json = JSONObject("""{"models":[{"name":"nur ein Name"},{"value":"echt"}]}""")
        assertEquals(listOf("echt"), HermesPayloads.modelChoices(json).models)
    }

    @Test
    fun `current model uses the endpoint's field name`() {
        // The app previously read "model", which is not a field the endpoint
        // sends — the current model silently read as null on every call.
        val json = JSONObject("""{"current_model":"gpt-5.6-sol[high]","models":[]}""")
        assertEquals("gpt-5.6-sol[high]", HermesPayloads.modelChoices(json).current)
    }

    @Test
    fun `model change reads old and new model`() {
        val recorded = JSONObject(
            """{"stem":"kimi","old_model":"kimi-code/k3","new_model":"kimi-code/k2",
               "restart":{"restarted":true,"ready":true,"error":null},"unverified":false}""",
        )
        val change = HermesPayloads.modelChange(recorded, "kimi-code/k2")
        assertEquals("kimi-code/k3", change.previous)
        assertEquals("kimi-code/k2", change.current)
        assertTrue(change.restarted)
        assertTrue(change.ready)
        assertNull(change.error)
    }

    @Test
    fun `a restart that never became ready is not reported as ready`() {
        val recorded = JSONObject(
            """{"stem":"kimi","old_model":"a","new_model":"b",
               "restart":{"restarted":true,"ready":false,"error":"readiness_timeout"},
               "unverified":true}""",
        )
        val change = HermesPayloads.modelChange(recorded, "b")
        assertTrue(change.restarted)
        assertTrue(!change.ready)
        assertEquals("readiness_timeout", change.error)
        assertTrue(change.unverified)
    }

    @Test
    fun `null error stays null instead of becoming the string null`() {
        val json = JSONObject("""{"models":[],"error":null}""")
        assertNull(HermesPayloads.modelChoices(json).error)
    }

    @Test
    fun `an unknown error code keeps its detail`() {
        val json = JSONObject("""{"error":{"code":"etwas_neues","detail":"Sehr genaue Ursache"}}""")
        assertEquals("Sehr genaue Ursache", HermesPayloads.modelChoices(json).error)
    }
}
