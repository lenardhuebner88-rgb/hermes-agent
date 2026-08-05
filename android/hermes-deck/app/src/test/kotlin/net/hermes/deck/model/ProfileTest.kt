package net.hermes.deck.model

import net.hermes.deck.nostr.Kind
import net.hermes.deck.nostr.NostrEvent
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

class ProfileTest {

    private val key = "7d122ae6eff098ccb06b5a472ec6e702cb28110a66049bec7b2bc93cb09f5daf"

    private fun profileEvent(content: String, createdAt: Long = 1000, author: String = key) =
        NostrEvent(
            id = "id-$createdAt",
            pubkey = author,
            createdAt = createdAt,
            kind = Kind.PROFILE,
            tags = emptyList(),
            content = content,
            sig = "",
        )

    @Test
    fun `display name wins over name, matching what the chat shows`() {
        val profile = Profile.fromEvent(
            profileEvent("""{"name":"claude-acp","display_name":"Claude","picture":"https://x/y.png"}"""),
        )

        assertEquals("Claude", profile?.name)
        assertEquals("https://x/y.png", profile?.picture)
    }

    @Test
    fun `plain name is used when there is no display name`() {
        assertEquals("kimi", Profile.fromEvent(profileEvent("""{"name":"kimi"}"""))?.name)
    }

    @Test
    fun `malformed content yields null instead of taking the roster down`() {
        assertNull(Profile.fromEvent(profileEvent("not json")))
        assertNull(Profile.fromEvent(profileEvent("")))
    }

    @Test
    fun `a non-profile event is not a profile`() {
        val message = NostrEvent("i", key, 1, Kind.CHANNEL_MESSAGE, emptyList(), """{"name":"X"}""", "")

        assertNull(Profile.fromEvent(message))
    }

    @Test
    fun `the newest profile per author wins regardless of arrival order`() {
        // Relays return every historical kind:0 and do not promise an order;
        // taking the last one seen would let a superseded name win at random.
        val events = listOf(
            profileEvent("""{"name":"Alt"}""", createdAt = 100),
            profileEvent("""{"name":"Neu"}""", createdAt = 900),
            profileEvent("""{"name":"Mittel"}""", createdAt = 500),
        )

        assertEquals("Neu", Profile.newestByPubkey(events)[key]?.name)
    }

    @Test
    fun `an empty name falls back to the short key rather than a blank row`() {
        val profile = Profile.fromEvent(profileEvent("""{"name":"  "}"""))!!

        assertNull(profile.name)
        assertEquals("7d122ae6", profile.label)
    }

    @Test
    fun `labelFor falls back to the short key for an unknown pubkey`() {
        assertEquals("7d122ae6", Profile.labelFor(key, emptyMap()))
        assertEquals(
            "Claude",
            Profile.labelFor(key, mapOf(key to Profile(key, "Claude", null, null))),
        )
    }
}
