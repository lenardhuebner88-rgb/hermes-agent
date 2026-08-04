package net.hermes.deck.model

import net.hermes.deck.nostr.Kind
import net.hermes.deck.nostr.NostrEvent
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Test

/** Parsed against the real `kind:39000`/`39002` events in the fixture. */
class ChannelTest {

    private fun fixture(): List<NostrEvent> =
        checkNotNull(javaClass.classLoader?.getResourceAsStream("real-relay-events.jsonl"))
            .bufferedReader().readLines().filter { it.isNotBlank() }
            .map { NostrEvent.fromJson(JSONObject(it)) }

    @Test
    fun `real channel metadata parses into a usable project`() {
        val channels = fixture().filter { it.kind == Kind.CHANNEL_METADATA }
            .mapNotNull { Channel.fromMetadata(it) }
        assertTrue("fixture carried no channel metadata", channels.isNotEmpty())
        channels.forEach {
            assertTrue("channel ${it.id} has no id", it.id.isNotBlank())
            assertTrue("channel ${it.id} has no display name", it.displayName.isNotBlank())
        }
    }

    @Test
    fun `topic and purpose survive because the deck shows them when filing`() {
        val withTopic = fixture().filter { it.kind == Kind.CHANNEL_METADATA }
            .mapNotNull { Channel.fromMetadata(it) }
            .firstOrNull { !it.topic.isNullOrBlank() }
        assertNotNull("no real channel carried a topic — fixture is stale", withTopic)
        assertTrue(withTopic!!.topic!!.isNotBlank())
    }

    @Test
    fun `an archived channel is not offered as a project`() {
        val archived = fixture().filter { it.kind == Kind.CHANNEL_METADATA }
            .mapNotNull { Channel.fromMetadata(it) }
            .firstOrNull { it.archived }
        assertNotNull("fixture no longer contains an archived channel", archived)
        assertFalse(archived!!.usableAsProject)
    }

    @Test
    fun `membership events yield the channel and its members`() {
        val memberships = fixture().filter { it.kind == Kind.CHANNEL_MEMBERS }
        assertTrue(memberships.isNotEmpty())
        memberships.forEach {
            assertNotNull(Channel.membershipChannelId(it))
            assertTrue("membership without members", Channel.membersOf(it).isNotEmpty())
        }
    }

    @Test
    fun `a direct message channel is kept out of the project list`() {
        val dm = Channel(
            id = "x", name = "DM", type = "dm", topic = null, about = null,
            purpose = null, archived = false, memberPubkeys = emptyList(),
        )
        assertFalse(dm.usableAsProject)
        assertTrue(dm.isDirectMessage)
    }

    @Test
    fun `a channel without a name still renders`() {
        val nameless = Channel(
            id = "abcdef1234", name = "", type = "stream", topic = null, about = null,
            purpose = null, archived = false, memberPubkeys = emptyList(),
        )
        assertEquals("abcdef12", nameless.displayName)
        assertTrue(nameless.usableAsProject)
    }
}
