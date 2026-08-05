package net.hermes.deck.nostr

import java.math.BigInteger
import java.security.MessageDigest
import java.security.SecureRandom
import org.bouncycastle.crypto.ec.CustomNamedCurves
import org.bouncycastle.math.ec.ECPoint

/**
 * BIP-340 Schnorr signatures over secp256k1.
 *
 * BouncyCastle gives us the curve arithmetic but ships no BIP-340 signer, so the
 * scheme itself lives here. Every step is verified against the official BIP-340
 * test vectors in `SchnorrTest`; do not "simplify" the even-y negations or the
 * tagged hashes — each one silently changes the signature and the relay would
 * reject events with no diagnosable error.
 */
object Schnorr {

    private val curve = CustomNamedCurves.getByName("secp256k1")
    private val n: BigInteger = curve.n
    private val p: BigInteger = curve.curve.field.characteristic
    private val g: ECPoint = curve.g

    /** sha256(sha256(tag) || sha256(tag) || msg) — BIP-340 tagged hash. */
    fun taggedHash(tag: String, vararg parts: ByteArray): ByteArray {
        val digest = MessageDigest.getInstance("SHA-256")
        val tagHash = digest.digest(tag.toByteArray(Charsets.UTF_8))
        digest.reset()
        digest.update(tagHash)
        digest.update(tagHash)
        parts.forEach { digest.update(it) }
        return digest.digest()
    }

    /** The 32-byte x-only public key for a 32-byte secret key. */
    fun publicKey(secretKey: ByteArray): ByteArray {
        val d0 = secretKeyScalar(secretKey)
        return xOnly(g.multiply(d0).normalize())
    }

    /**
     * Signs a message of any length — BIP-340 was widened beyond 32 bytes in
     * 2022 and the official vectors cover sizes 0, 1, 17 and 100. Nostr only
     * ever signs the 32-byte event id, but a signer that silently rejects the
     * other sizes is simply wrong.
     *
     * [auxRand] is the BIP-340 auxiliary randomness; callers outside tests should
     * leave it null so a fresh 32 random bytes are drawn per signature.
     */
    fun sign(message: ByteArray, secretKey: ByteArray, auxRand: ByteArray? = null): ByteArray {
        val d0 = secretKeyScalar(secretKey)
        val pointP = g.multiply(d0).normalize()
        // The secret is negated when P has odd y, so that P is always even-y.
        val d = if (pointP.affineYCoord.toBigInteger().testBit(0)) n.subtract(d0) else d0
        val pubBytes = xOnly(pointP)

        val aux = auxRand ?: ByteArray(32).also { SecureRandom().nextBytes(it) }
        require(aux.size == 32) { "aux_rand must be 32 bytes, got ${aux.size}" }
        val t = xor(toBytes32(d), taggedHash("BIP0340/aux", aux))

        val rand = taggedHash("BIP0340/nonce", t, pubBytes, message)
        val k0 = BigInteger(1, rand).mod(n)
        require(k0.signum() != 0) { "BIP-340: nonce is zero" }

        val pointR = g.multiply(k0).normalize()
        val k = if (pointR.affineYCoord.toBigInteger().testBit(0)) n.subtract(k0) else k0
        val rBytes = xOnly(pointR)

        val e = BigInteger(1, taggedHash("BIP0340/challenge", rBytes, pubBytes, message)).mod(n)
        val s = k.add(e.multiply(d)).mod(n)

        val signature = rBytes + toBytes32(s)
        // Signing a bad signature is worse than failing loudly: it would only
        // surface as a silently dropped event at the relay.
        check(verify(message, pubBytes, signature)) { "BIP-340: produced signature does not verify" }
        return signature
    }

    /** Verifies a 64-byte signature against a 32-byte x-only public key. */
    fun verify(message: ByteArray, publicKey: ByteArray, signature: ByteArray): Boolean {
        if (publicKey.size != 32 || signature.size != 64) return false
        val pointP = liftX(BigInteger(1, publicKey)) ?: return false
        val r = BigInteger(1, signature.copyOfRange(0, 32))
        val s = BigInteger(1, signature.copyOfRange(32, 64))
        if (r >= p || s >= n) return false

        val e = BigInteger(1, taggedHash("BIP0340/challenge", signature.copyOfRange(0, 32), publicKey, message)).mod(n)
        val pointR = g.multiply(s).add(pointP.multiply(n.subtract(e))).normalize()
        if (pointR.isInfinity) return false
        if (pointR.affineYCoord.toBigInteger().testBit(0)) return false
        return pointR.affineXCoord.toBigInteger() == r
    }

    private fun secretKeyScalar(secretKey: ByteArray): BigInteger {
        require(secretKey.size == 32) { "secret key must be 32 bytes, got ${secretKey.size}" }
        val d0 = BigInteger(1, secretKey)
        require(d0.signum() != 0 && d0 < n) { "secret key out of range" }
        return d0
    }

    /** The even-y curve point with the given x, or null if x is not on the curve. */
    private fun liftX(x: BigInteger): ECPoint? {
        if (x >= p) return null
        val ySq = x.modPow(BigInteger.valueOf(3), p).add(BigInteger.valueOf(7)).mod(p)
        val y = ySq.modPow(p.add(BigInteger.ONE).shiftRight(2), p)
        // BigInteger.TWO is Java 9+ and not reliably present on older Android
        // core libraries, so build the exponent explicitly.
        if (y.modPow(BigInteger.valueOf(2), p) != ySq) return null
        val even = if (y.testBit(0)) p.subtract(y) else y
        return curve.curve.createPoint(x, even).normalize()
    }

    private fun xOnly(point: ECPoint): ByteArray = toBytes32(point.affineXCoord.toBigInteger())

    private fun toBytes32(value: BigInteger): ByteArray {
        val raw = value.toByteArray()
        val out = ByteArray(32)
        // BigInteger.toByteArray() is signed and variable-length: it may carry a
        // leading zero byte or be shorter than 32. Right-align either way.
        if (raw.size > 32) {
            System.arraycopy(raw, raw.size - 32, out, 0, 32)
        } else {
            System.arraycopy(raw, 0, out, 32 - raw.size, raw.size)
        }
        return out
    }

    private fun xor(a: ByteArray, b: ByteArray): ByteArray =
        ByteArray(a.size) { (a[it].toInt() xor b[it].toInt()).toByte() }
}

/** Lowercase hex helpers; the whole Nostr wire format is hex strings. */
object Hex {
    fun encode(bytes: ByteArray): String {
        val out = StringBuilder(bytes.size * 2)
        for (b in bytes) {
            val v = b.toInt() and 0xFF
            out.append("0123456789abcdef"[v ushr 4])
            out.append("0123456789abcdef"[v and 0x0F])
        }
        return out.toString()
    }

    fun decode(hex: String): ByteArray {
        require(hex.length % 2 == 0) { "hex string must have even length, got ${hex.length}" }
        return ByteArray(hex.length / 2) {
            ((digit(hex[it * 2]) shl 4) or digit(hex[it * 2 + 1])).toByte()
        }
    }

    private fun digit(c: Char): Int {
        val v = Character.digit(c, 16)
        require(v >= 0) { "not a hex digit: $c" }
        return v
    }
}
