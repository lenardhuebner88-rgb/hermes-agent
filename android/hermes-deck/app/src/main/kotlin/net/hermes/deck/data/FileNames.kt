package net.hermes.deck.data

import android.content.ContentResolver
import android.net.Uri
import android.provider.OpenableColumns

/**
 * The human name behind a content URI.
 *
 * `uri.lastPathSegment` looks like it does this and does not: a MediaStore
 * document URI ends in its row id, so a picked screenshot arrived in Buzz as
 * `image:36` — a name that is wrong without ever looking broken. Only the
 * provider knows the display name, and it has to be asked.
 */
fun ContentResolver.displayNameOf(uri: Uri, fallback: String = "anhang"): String {
    if (uri.scheme == ContentResolver.SCHEME_FILE) {
        return uri.lastPathSegment?.substringAfterLast('/')?.takeIf { it.isNotBlank() } ?: fallback
    }
    val fromProvider = runCatching {
        query(uri, arrayOf(OpenableColumns.DISPLAY_NAME), null, null, null)?.use { cursor ->
            val column = cursor.getColumnIndex(OpenableColumns.DISPLAY_NAME)
            if (column >= 0 && cursor.moveToFirst()) cursor.getString(column) else null
        }
    }.getOrNull()
    return fromProvider?.takeIf { it.isNotBlank() } ?: fallback
}
