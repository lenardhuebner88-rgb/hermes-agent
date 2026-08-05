package net.hermes.deck.data

import java.net.URLDecoder
import net.hermes.deck.nostr.Bech32
import net.hermes.deck.nostr.Hex
import net.hermes.deck.nostr.Schnorr

/**
 * One-tap provisioning: `hermes-deck://setup?relay=…&key=…&hermes=…&user=…&pass=…`
 *
 * Typing a 64-character key on a phone is the kind of task that gets abandoned,
 * so the link carries everything. That also makes it dangerous — a link is
 * attacker-supplied input, and one that quietly swapped the relay would send
 * every future note to someone else's server. Parsing therefore never applies
 * anything; it produces a [SetupPayload] the UI must show and the user must
 * confirm, with the relay host and the resulting npub spelled out.
 *
 * Deliberately free of `android.net.Uri` so the whole parser is covered by
 * plain JVM tests.
 */
object SetupLink {

    const val SCHEME = "hermes-deck"
    const val HOST = "setup"

    data class SetupPayload(
        val relayUrl: String?,
        val secretKeyHex: String?,
        val hermesUrl: String?,
        val hermesUser: String?,
        val hermesPassword: String?,
    ) {
        /** `npub1…` for the key in this link, so the user can recognise it. */
        val npub: String?
            get() = secretKeyHex?.let {
                runCatching { Bech32.encodeNpub(Hex.encode(Schnorr.publicKey(Hex.decode(it)))) }.getOrNull()
            }

        /** Host only — the part that decides where the data actually goes. */
        val relayHost: String?
            get() = relayUrl?.substringAfter("://", relayUrl)?.substringBefore('/')

        val isEmpty: Boolean
            get() = relayUrl == null && secretKeyHex == null && hermesUrl == null &&
                hermesUser == null && hermesPassword == null
    }

    class InvalidLink(message: String) : IllegalArgumentException(message)

    /**
     * Parses a setup link. Throws [InvalidLink] with a readable reason rather
     * than returning a half-filled payload — a link that is wrong in one field
     * is not a link worth half-applying.
     */
    fun parse(raw: String): SetupPayload {
        val trimmed = raw.trim()
        val prefix = "$SCHEME://$HOST"
        if (!trimmed.startsWith(prefix, ignoreCase = true)) {
            throw InvalidLink("Kein Einrichtungs-Link für Hermes Deck.")
        }
        val query = trimmed.substringAfter('?', "")
        val params = mutableMapOf<String, String>()
        for (part in query.split('&')) {
            if (part.isBlank()) continue
            val name = decode(part.substringBefore('='))
            val value = decode(part.substringAfter('=', ""))
            if (name.isNotBlank() && value.isNotBlank()) params[name] = value
        }

        val key = params["key"]?.let { normaliseKey(it) }
        val payload = SetupPayload(
            relayUrl = params["relay"]?.let { requireHttps(it, "Relay") },
            secretKeyHex = key,
            hermesUrl = params["hermes"]?.let { requireHttps(it, "Dashboard") },
            hermesUser = params["user"],
            hermesPassword = params["pass"],
        )
        if (payload.isEmpty) throw InvalidLink("Der Link enthält keine Einstellungen.")
        return payload
    }

    /** Accepts `nsec1…` or bare hex, and always yields hex. */
    private fun normaliseKey(input: String): String {
        val cleaned = input.trim()
        val bytes = when {
            cleaned.startsWith("nsec", ignoreCase = true) ->
                runCatching { Bech32.decodeNsec(cleaned) }
                    .getOrElse { throw InvalidLink("Der nsec im Link ist beschädigt.") }
            cleaned.length == 64 && cleaned.all { it.isDigit() || it.lowercaseChar() in 'a'..'f' } ->
                Hex.decode(cleaned)
            else -> throw InvalidLink("Der Schlüssel im Link ist weder nsec1… noch 64 Hex-Zeichen.")
        }
        // Reject an out-of-range key here rather than letting it surface much
        // later as an unexplained AUTH rejection.
        runCatching { Schnorr.publicKey(bytes) }
            .getOrElse { throw InvalidLink("Der Schlüssel im Link ist kein gültiger Nostr-Schlüssel.") }
        return Hex.encode(bytes)
    }

    /**
     * Plain http would send the key over the wire in the clear, and the app
     * sets `usesCleartextTraffic=false` anyway — so reject it loudly instead of
     * storing a URL that can only fail later.
     */
    private fun requireHttps(url: String, label: String): String {
        val cleaned = url.trim().trimEnd('/')
        if (!cleaned.startsWith("https://", ignoreCase = true) &&
            !cleaned.startsWith("wss://", ignoreCase = true)
        ) {
            throw InvalidLink("$label-Adresse im Link ist nicht verschlüsselt (https/wss).")
        }
        return cleaned
    }

    private fun decode(value: String): String =
        runCatching { URLDecoder.decode(value, "UTF-8") }.getOrDefault(value)
}
