package net.hermes.dictate

/** Pure policies for visibility and field identity; no cached-field fallback is allowed. */
object OverlaySafety {
    fun shouldShow(hasEligibleFocus: Boolean, phase: DictationController.Phase): Boolean =
        hasEligibleFocus || phase != DictationController.Phase.IDLE

    fun <T> commitTarget(startedField: T?, liveField: T?): T? =
        liveField?.takeIf { startedField != null && it == startedField }
}
