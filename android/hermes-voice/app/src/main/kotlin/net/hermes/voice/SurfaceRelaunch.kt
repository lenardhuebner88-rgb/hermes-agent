package net.hermes.voice

/**
 * The `onNewIntent` routing collaborator for a warm `singleTop` [MainActivity].
 *
 * When the shell is already alive and a launcher intent is re-delivered to
 * `onNewIntent` (tapping the Jarvis alias while the Voice surface is on top, or
 * vice-versa), Android does NOT recreate the activity — so the surface must be
 * re-derived and actually reloaded here, or the shell silently stays on whatever
 * it showed before. That was the original AC1 defect: `onNewIntent` only stashed
 * the dictation draft and never loaded the target URL.
 *
 * This object owns that decision AND drives the side effects it implies, so the
 * behaviour is unit-testable off-device without Robolectric: [MainActivity.onNewIntent]
 * is a thin delegation to [route] (`::stopCaptureIfActive`, `::loadSurface`), and a
 * test injects recording lambdas to prove the correct URL is *loaded* — not merely
 * computed — for a warm alias relaunch, that any live capture is stopped first, and
 * that dictation intents and same-surface re-taps never navigate.
 */
object SurfaceRelaunch {
    /**
     * Route a re-delivered launch intent for a warm activity. If it selects a
     * different in-origin surface than [currentUrl], stop any live capture (so no
     * MediaProjection survives the navigation) and then load the new surface via
     * [loadSurface]. Returns true iff a surface switch was performed.
     *
     * On a real switch [stopCapture] always runs before [loadSurface]; for a
     * non-launcher intent (e.g. ACTION_SEND dictation) or a re-tap of the surface
     * already visible, neither lambda runs and the current page is left untouched.
     */
    fun route(
        isLauncherEntry: Boolean,
        launchClass: String?,
        currentUrl: String?,
        stopCapture: () -> Unit,
        loadSurface: (String) -> Unit,
    ): Boolean {
        val target = VoiceAppConfig.relaunchSurfaceTarget(
            isLauncherEntry = isLauncherEntry,
            launchClass = launchClass,
            currentUrl = currentUrl,
        ) ?: return false
        stopCapture()
        loadSurface(target)
        return true
    }
}
