package net.hermes.deck.data

import android.content.Context
import android.content.SharedPreferences
import androidx.security.crypto.EncryptedSharedPreferences
import androidx.security.crypto.MasterKey
import net.hermes.deck.nostr.Bech32
import net.hermes.deck.nostr.Hex
import net.hermes.deck.nostr.Schnorr

/**
 * Everything the deck needs to reach the outside world, kept in
 * [EncryptedSharedPreferences].
 *
 * The Nostr secret key is a full identity on Piet's relay — it can post as him
 * and upload under his name — so it never touches plain SharedPreferences, and
 * neither does the dashboard password.
 */
class DeckSettings(context: Context) {

    private val prefs: SharedPreferences = run {
        val masterKey = MasterKey.Builder(context)
            .setKeyScheme(MasterKey.KeyScheme.AES256_GCM)
            .build()
        EncryptedSharedPreferences.create(
            context,
            "deck-secure",
            masterKey,
            EncryptedSharedPreferences.PrefKeyEncryptionScheme.AES256_SIV,
            EncryptedSharedPreferences.PrefValueEncryptionScheme.AES256_GCM,
        )
    }

    var relayBaseUrl: String
        get() = prefs.getString(KEY_RELAY, DEFAULT_RELAY) ?: DEFAULT_RELAY
        set(value) = prefs.edit().putString(KEY_RELAY, value.trim().trimEnd('/')).apply()

    var hermesBaseUrl: String
        get() = prefs.getString(KEY_HERMES, DEFAULT_HERMES) ?: DEFAULT_HERMES
        set(value) = prefs.edit().putString(KEY_HERMES, value.trim().trimEnd('/')).apply()

    var hermesUsername: String
        get() = prefs.getString(KEY_HERMES_USER, "") ?: ""
        set(value) = prefs.edit().putString(KEY_HERMES_USER, value).apply()

    var hermesPassword: String
        get() = prefs.getString(KEY_HERMES_PASS, "") ?: ""
        set(value) = prefs.edit().putString(KEY_HERMES_PASS, value).apply()

    /** The channel new quick captures land in until sorted. */
    var inboxChannelId: String
        get() = prefs.getString(KEY_INBOX_CHANNEL, "") ?: ""
        set(value) = prefs.edit().putString(KEY_INBOX_CHANNEL, value).apply()

    val secretKey: ByteArray?
        get() = prefs.getString(KEY_SECRET, null)?.let { runCatching { Hex.decode(it) }.getOrNull() }

    val pubkeyHex: String?
        get() = secretKey?.let { Hex.encode(Schnorr.publicKey(it)) }

    val hasIdentity: Boolean get() = secretKey != null

    /**
     * Accepts either an `nsec1…` or a bare 64-char hex key and stores the raw
     * bytes. Throws with a readable message rather than storing something that
     * would later fail as an unexplained auth rejection.
     */
    fun importKey(input: String) {
        val cleaned = input.trim()
        val bytes = when {
            cleaned.startsWith("nsec", ignoreCase = true) -> Bech32.decodeNsec(cleaned)
            cleaned.length == 64 && cleaned.all { it.isDigit() || it.lowercaseChar() in 'a'..'f' } ->
                Hex.decode(cleaned)
            else -> throw IllegalArgumentException(
                "Das sieht weder nach einem nsec1…-Schlüssel noch nach 64 Hex-Zeichen aus.",
            )
        }
        // Derive once now: an out-of-range key would otherwise only surface as a
        // failed AUTH much later, with nothing pointing back at the import.
        Schnorr.publicKey(bytes)
        prefs.edit().putString(KEY_SECRET, Hex.encode(bytes)).apply()
    }

    fun forgetIdentity() {
        prefs.edit().remove(KEY_SECRET).apply()
    }

    companion object {
        const val DEFAULT_RELAY = "https://huebners.tail50819a.ts.net:9444"
        const val DEFAULT_HERMES = "https://huebners.tail50819a.ts.net:9443"

        private const val KEY_RELAY = "relay_base_url"
        private const val KEY_HERMES = "hermes_base_url"
        private const val KEY_HERMES_USER = "hermes_username"
        private const val KEY_HERMES_PASS = "hermes_password"
        private const val KEY_INBOX_CHANNEL = "inbox_channel"
        private const val KEY_SECRET = "nostr_secret_hex"
    }
}
