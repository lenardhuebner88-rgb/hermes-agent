package net.hermes.deck.net

import android.util.Base64
import java.io.IOException
import java.security.MessageDigest
import net.hermes.deck.model.Attachment
import net.hermes.deck.nostr.Hex
import net.hermes.deck.nostr.RelayProtocol
import okhttp3.MediaType.Companion.toMediaTypeOrNull
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONObject

/**
 * Uploads attachments to Buzz's Blossom endpoint.
 *
 * The relay authorises uploads with a signed `kind:24242` event carried in
 * `Authorization: Nostr <base64>`, and cross-checks the body's digest against
 * both the event's `x` tag and the `X-SHA-256` header. All three must agree, so
 * the hash is computed once up front and reused.
 */
class MediaUploader(
    private val baseUrl: String,
    private val secretKey: ByteArray,
    private val http: OkHttpClient = OkHttpClient(),
    private val clock: () -> Long = { System.currentTimeMillis() / 1000 },
) {

    class UploadFailed(message: String, val httpCode: Int? = null) : IOException(message)

    /**
     * Server-side limits, so the app can refuse early with a useful message
     * instead of pushing 60 MB over a phone connection to be rejected.
     */
    companion object {
        const val MAX_IMAGE_BYTES = 50L * 1024 * 1024
        const val MAX_FILE_BYTES = 100L * 1024 * 1024

        /** The relay only stores these image types; everything else is a generic file. */
        val ALLOWED_IMAGE_TYPES = setOf("image/jpeg", "image/png", "image/gif", "image/webp")

        /**
         * Types the relay refuses outright — worth catching before the upload so
         * the user sees why rather than a bare 400.
         */
        val BLOCKED_TYPES = setOf(
            "text/html", "application/xhtml+xml", "image/svg+xml",
            "text/javascript", "application/javascript",
            "application/vnd.android.package-archive",
        )

        fun sha256(bytes: ByteArray): String =
            Hex.encode(MessageDigest.getInstance("SHA-256").digest(bytes))
    }

    /** Rejects what the relay would reject, with a reason in German. Null = fine. */
    fun rejectionReason(bytes: ByteArray, mime: String): String? {
        if (bytes.isEmpty()) return "Die Datei ist leer."
        if (mime in BLOCKED_TYPES) return "Dateien vom Typ $mime nimmt Buzz nicht an."
        val isImage = mime.startsWith("image/")
        if (isImage && mime !in ALLOWED_IMAGE_TYPES) {
            return "Buzz speichert nur JPEG, PNG, GIF und WebP — $mime gehört nicht dazu."
        }
        val limit = if (isImage) MAX_IMAGE_BYTES else MAX_FILE_BYTES
        if (bytes.size > limit) {
            return "Zu groß: ${bytes.size / 1024 / 1024} MB, erlaubt sind ${limit / 1024 / 1024} MB."
        }
        return null
    }

    suspend fun upload(bytes: ByteArray, mime: String, fileName: String): Attachment {
        rejectionReason(bytes, mime)?.let { throw UploadFailed(it) }

        val digest = sha256(bytes)
        val host = baseUrl.removePrefix("https://").removePrefix("http://").trimEnd('/')
        val auth = RelayProtocol.blossomUploadAuth(
            secretKey = secretKey,
            sha256Hex = digest,
            // Short-lived: the header is a bearer token for this one blob.
            expiresAt = clock() + 300,
            serverHost = host,
            now = clock(),
        )
        val authHeader = "Nostr " + Base64.encodeToString(
            auth.toJson().toString().toByteArray(Charsets.UTF_8),
            Base64.NO_WRAP,
        )

        val request = Request.Builder()
            .url("${baseUrl.trimEnd('/')}/upload")
            .put(bytes.toRequestBody(mime.toMediaTypeOrNull()))
            .header("Authorization", authHeader)
            .header("X-SHA-256", digest)
            .header("Content-Type", mime)
            .build()

        http.newCall(request).execute().use { response ->
            val body = response.body?.string().orEmpty()
            if (!response.isSuccessful) {
                throw UploadFailed(uploadErrorMessage(response.code, body), response.code)
            }
            val descriptor = runCatching { JSONObject(body) }.getOrNull()
                ?: throw UploadFailed("Unerwartete Antwort beim Upload")
            return Attachment(
                url = descriptor.optString("url"),
                mime = descriptor.optString("type", mime),
                sha256 = descriptor.optString("sha256", digest),
                size = descriptor.optLong("size", bytes.size.toLong()),
                name = descriptor.optString("filename", fileName),
            )
        }
    }

    private fun uploadErrorMessage(code: Int, body: String): String = when (code) {
        401, 403 ->
            // The membership gate is the usual cause here, not a bad signature:
            // BUZZ_REQUIRE_RELAY_MEMBERSHIP is on for this community.
            "Upload abgelehnt — ist dieser Schlüssel Mitglied der Community? (HTTP $code)"
        413 -> "Die Datei ist dem Relay zu groß (HTTP 413)"
        429 -> "Zu viele Uploads kurz hintereinander — kurz warten (HTTP 429)"
        else -> "Upload fehlgeschlagen (HTTP $code) ${body.take(200)}".trim()
    }
}
