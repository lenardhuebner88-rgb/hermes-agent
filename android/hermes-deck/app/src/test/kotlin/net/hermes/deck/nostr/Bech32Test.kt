package net.hermes.deck.nostr

import org.junit.Assert.assertEquals
import org.junit.Assert.assertThrows
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * The `nsec`/`npub` pairs below are the NIP-19 specification examples, confirmed
 * against an independent reference implementation of BIP-173 before being pinned
 * here — a self-consistent codec would otherwise pass its own tests happily while
 * rejecting every key Buzz hands out.
 */
class Bech32Test {

    private val specNsec = "nsec1vl029mgpspedva04g90vltkh6fvh240zqtv9k0t9af8935ke9laqsnlfe5"
    private val specNsecHex = "67dea2ed018072d675f5415ecfaed7d2597555e202d85b3d65ea4e58d2d92ffa"
    private val specNpub = "npub180cvv07tjdrrgpa0j7j7tmnyl2yr6yr7l8j4s3evf6u64th6gkwsyjh6w6"
    private val specNpubHex = "3bf0c63fcb93463407af97a5e5ee64fa883d107ef9e558472c4eb9aaaefa459d"

    @Test
    fun `decodes the NIP-19 nsec example`() {
        assertEquals(specNsecHex, Hex.encode(Bech32.decodeNsec(specNsec)))
    }

    @Test
    fun `encodes the NIP-19 npub example`() {
        assertEquals(specNpub, Bech32.encodeNpub(specNpubHex))
    }

    @Test
    fun `decodes the NIP-19 npub example`() {
        assertEquals(specNpubHex, Bech32.decodeNpub(specNpub))
    }

    @Test
    fun `round-trips an arbitrary key`() {
        val hex = "b7e151628aed2a6abf7158809cf4f3c762e7160f38b4da56a784d9045190cfef"
        assertEquals(hex, Bech32.decodeNpub(Bech32.encodeNpub(hex)))
    }

    @Test
    fun `an nsec derives the public key the relay will see`() {
        val sk = Bech32.decodeNsec(specNsec)
        assertEquals(64, Hex.encode(Schnorr.publicKey(sk)).length)
    }

    @Test
    fun `uppercase input decodes but mixed case is rejected`() {
        assertEquals(specNsecHex, Hex.encode(Bech32.decodeNsec(specNsec.uppercase())))
        val mixed = specNsec.substring(0, 10).uppercase() + specNsec.substring(10)
        assertThrows(Bech32.InvalidBech32::class.java) { Bech32.decodeNsec(mixed) }
    }

    @Test
    fun `a single altered character fails the checksum`() {
        val chars = specNsec.toCharArray()
        chars[20] = if (chars[20] == 'q') 'p' else 'q'
        assertThrows(Bech32.InvalidBech32::class.java) { Bech32.decodeNsec(String(chars)) }
    }

    @Test
    fun `an npub is refused where an nsec is required`() {
        val error = assertThrows(Bech32.InvalidBech32::class.java) { Bech32.decodeNsec(specNpub) }
        assertTrue(error.message!!.contains("npub"))
    }

    @Test
    fun `surrounding whitespace from a paste is tolerated`() {
        assertEquals(specNsecHex, Hex.encode(Bech32.decodeNsec("  $specNsec\n")))
    }
}
