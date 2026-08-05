package net.hermes.deck.nostr

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Drives the official BIP-340 test vectors (bip-0340/test-vectors.csv, verbatim
 * copy in test resources). These are the only proof that our signatures are the
 * ones the relay will accept — a subtly wrong signer produces well-formed events
 * that are simply dropped.
 */
class SchnorrTest {

    private data class Vector(
        val index: String,
        val secretKey: String,
        val publicKey: String,
        val auxRand: String,
        val message: String,
        val signature: String,
        val shouldVerify: Boolean,
        val comment: String,
    )

    private fun vectors(): List<Vector> {
        val stream = checkNotNull(javaClass.classLoader?.getResourceAsStream("bip340-test-vectors.csv")) {
            "bip340-test-vectors.csv is missing from test resources"
        }
        val lines = stream.bufferedReader().readLines().filter { it.isNotBlank() }
        return lines.drop(1).map { line ->
            val f = line.split(",")
            Vector(
                index = f[0],
                secretKey = f[1].trim(),
                publicKey = f[2].trim(),
                auxRand = f[3].trim(),
                message = f[4].trim(),
                signature = f[5].trim(),
                shouldVerify = f[6].trim().uppercase() == "TRUE",
                comment = f.getOrElse(7) { "" },
            )
        }
    }

    @Test
    fun `the vector file actually loaded`() {
        // A silently empty resource would turn every loop below into a green
        // no-op — the control probe against exactly that.
        assertTrue("expected the official vector set, got ${vectors().size} rows", vectors().size >= 15)
    }

    @Test
    fun `signing reproduces every official signature`() {
        val signing = vectors().filter { it.secretKey.isNotEmpty() }
        assertTrue("no signing vectors found", signing.isNotEmpty())
        for (v in signing) {
            val sig = Schnorr.sign(
                message = Hex.decode(v.message),
                secretKey = Hex.decode(v.secretKey),
                auxRand = Hex.decode(v.auxRand),
            )
            assertEquals("vector ${v.index}", v.signature.lowercase(), Hex.encode(sig))
        }
    }

    @Test
    fun `public keys derive to the official values`() {
        val signing = vectors().filter { it.secretKey.isNotEmpty() }
        for (v in signing) {
            assertEquals(
                "vector ${v.index}",
                v.publicKey.lowercase(),
                Hex.encode(Schnorr.publicKey(Hex.decode(v.secretKey))),
            )
        }
    }

    @Test
    fun `verification matches the official verdict including the failure cases`() {
        val all = vectors()
        // The failure half of the file is the part that catches an over-permissive
        // verifier, so assert it is actually present.
        assertTrue("no negative vectors found", all.any { !it.shouldVerify })
        for (v in all) {
            val ok = Schnorr.verify(
                message = Hex.decode(v.message),
                publicKey = Hex.decode(v.publicKey),
                signature = Hex.decode(v.signature),
            )
            assertEquals("vector ${v.index} (${v.comment})", v.shouldVerify, ok)
        }
    }

    @Test
    fun `a fresh signature verifies and a tampered one does not`() {
        val sk = Hex.decode("b7e151628aed2a6abf7158809cf4f3c762e7160f38b4da56a784d9045190cfef")
        val pk = Schnorr.publicKey(sk)
        val msg = Schnorr.taggedHash("test", "hallo".toByteArray())
        val sig = Schnorr.sign(msg, sk)
        assertTrue(Schnorr.verify(msg, pk, sig))

        val tampered = sig.copyOf()
        tampered[63] = (tampered[63].toInt() xor 0x01).toByte()
        assertFalse(Schnorr.verify(msg, pk, tampered))
    }

    @Test
    fun `two signatures of the same message differ because aux randomness is fresh`() {
        val sk = Hex.decode("b7e151628aed2a6abf7158809cf4f3c762e7160f38b4da56a784d9045190cfef")
        val msg = ByteArray(32) { 7 }
        assertFalse(Hex.encode(Schnorr.sign(msg, sk)) == Hex.encode(Schnorr.sign(msg, sk)))
    }

    @Test
    fun `hex round-trips including leading zero bytes`() {
        val bytes = byteArrayOf(0, 0, 15, -1, 16)
        assertEquals("00000fff10", Hex.encode(bytes))
        assertTrue(Hex.decode("00000fff10").contentEquals(bytes))
    }
}
