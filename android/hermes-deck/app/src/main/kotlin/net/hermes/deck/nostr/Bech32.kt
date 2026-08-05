package net.hermes.deck.nostr

/**
 * Bech32 (BIP-173) as used by NIP-19 for `nsec1…` / `npub1…`.
 *
 * Buzz hands out keys in this form, so the app has to read it. Only the plain
 * (non-TLV) entity forms are supported — that is all a key import needs.
 */
object Bech32 {

    private const val CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
    private val GENERATOR = intArrayOf(0x3b6a57b2, 0x26508e6d, 0x1ea119fa, 0x3d4233dd, 0x2a1462b3)

    data class Decoded(val hrp: String, val data: ByteArray) {
        override fun equals(other: Any?): Boolean =
            other is Decoded && hrp == other.hrp && data.contentEquals(other.data)

        override fun hashCode(): Int = 31 * hrp.hashCode() + data.contentHashCode()
    }

    class InvalidBech32(message: String) : IllegalArgumentException(message)

    /** Decodes a bech32 string into its prefix and the 8-bit payload. */
    fun decode(input: String): Decoded {
        val trimmed = input.trim()
        if (trimmed.length < 8) throw InvalidBech32("too short")
        // Mixed case is invalid per BIP-173 — it is not merely normalised away.
        val hasLower = trimmed.any { it.isLowerCase() }
        val hasUpper = trimmed.any { it.isUpperCase() }
        if (hasLower && hasUpper) throw InvalidBech32("mixed case")
        val s = trimmed.lowercase()

        val sep = s.lastIndexOf('1')
        if (sep < 1 || sep + 7 > s.length) throw InvalidBech32("misplaced separator")
        val hrp = s.substring(0, sep)
        val dataPart = s.substring(sep + 1)

        val values = IntArray(dataPart.length)
        for (i in dataPart.indices) {
            val v = CHARSET.indexOf(dataPart[i])
            if (v < 0) throw InvalidBech32("invalid character '${dataPart[i]}'")
            values[i] = v
        }
        if (polymod(hrpExpand(hrp) + values.toList()) != 1) throw InvalidBech32("checksum mismatch")

        val payload = values.copyOfRange(0, values.size - 6)
        return Decoded(hrp, convertBits(payload, 5, 8, false))
    }

    fun encode(hrp: String, data: ByteArray): String {
        val values = convertBits(data.map { it.toInt() and 0xFF }.toIntArray(), 8, 5, true)
            .map { it.toInt() and 0xFF }
        val checksum = createChecksum(hrp, values)
        val sb = StringBuilder(hrp).append('1')
        (values + checksum).forEach { sb.append(CHARSET[it]) }
        return sb.toString()
    }

    /** `nsec1…` → 32 secret key bytes. Rejects any other entity type. */
    fun decodeNsec(nsec: String): ByteArray {
        val decoded = decode(nsec)
        if (decoded.hrp != "nsec") throw InvalidBech32("expected an nsec key, got '${decoded.hrp}'")
        if (decoded.data.size != 32) throw InvalidBech32("nsec payload must be 32 bytes, got ${decoded.data.size}")
        return decoded.data
    }

    fun encodeNpub(pubkeyHex: String): String = encode("npub", Hex.decode(pubkeyHex))

    fun decodeNpub(npub: String): String {
        val decoded = decode(npub)
        if (decoded.hrp != "npub") throw InvalidBech32("expected an npub key, got '${decoded.hrp}'")
        if (decoded.data.size != 32) throw InvalidBech32("npub payload must be 32 bytes")
        return Hex.encode(decoded.data)
    }

    private fun polymod(values: List<Int>): Int {
        var chk = 1
        for (v in values) {
            val top = chk ushr 25
            chk = ((chk and 0x1ffffff) shl 5) xor v
            for (i in 0..4) {
                if (((top ushr i) and 1) != 0) chk = chk xor GENERATOR[i]
            }
        }
        return chk
    }

    private fun hrpExpand(hrp: String): List<Int> =
        hrp.map { it.code ushr 5 } + listOf(0) + hrp.map { it.code and 31 }

    private fun createChecksum(hrp: String, values: List<Int>): List<Int> {
        val enc = hrpExpand(hrp) + values + listOf(0, 0, 0, 0, 0, 0)
        val mod = polymod(enc) xor 1
        return (0..5).map { (mod ushr (5 * (5 - it))) and 31 }
    }

    private fun convertBits(data: IntArray, from: Int, to: Int, pad: Boolean): ByteArray {
        var acc = 0
        var bits = 0
        val out = ArrayList<Byte>()
        val maxv = (1 shl to) - 1
        for (value in data) {
            if (value < 0 || (value ushr from) != 0) throw InvalidBech32("value out of range")
            acc = (acc shl from) or value
            bits += from
            while (bits >= to) {
                bits -= to
                out.add(((acc ushr bits) and maxv).toByte())
            }
        }
        if (pad) {
            if (bits > 0) out.add(((acc shl (to - bits)) and maxv).toByte())
        } else if (bits >= from || ((acc shl (to - bits)) and maxv) != 0) {
            throw InvalidBech32("invalid padding")
        }
        return out.toByteArray()
    }
}
