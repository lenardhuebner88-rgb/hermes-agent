package net.hermes.deck.ui

import android.content.ActivityNotFoundException
import android.content.Context
import android.content.Intent
import android.net.Uri

/**
 * Hands a `buzz://` link to whatever app answers for it.
 *
 * Returns the reason it failed, or null on success — the caller decides how to
 * say so, and the screen must say *something*: a tap that does nothing looks
 * exactly like a broken button.
 *
 * **No `resolveActivity` pre-check on purpose.** Since Android 11 — this app
 * targets 37 — package visibility filters `resolveActivity`, so it returns null
 * for an app that is installed and perfectly able to handle the link, unless the
 * intent is also declared in `<queries>`. A pre-check would therefore report
 * "Buzz is not installed" on a phone where Buzz is running. `startActivity` is
 * exempt from the filtering, so the honest way round is to fire and catch.
 *
 * Success here means only that some app accepted the link. Whether Buzz then
 * shows the right message is beyond what this side can know, and nothing in
 * this app claims otherwise.
 */
fun openInBuzz(context: Context, link: String?): String? {
    if (link.isNullOrEmpty()) {
        return "Diese Nachricht lässt sich nicht adressieren."
    }
    val intent = Intent(Intent.ACTION_VIEW, Uri.parse(link)).apply {
        // The sheet is not an activity of its own, so the target needs its own
        // task rather than being stacked onto the deck's.
        addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
    }
    return try {
        context.startActivity(intent)
        null
    } catch (_: ActivityNotFoundException) {
        "Keine App für buzz://-Links gefunden. Ist Buzz installiert?"
    }
}
