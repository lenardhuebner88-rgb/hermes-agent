package net.hermes.dictate

/**
 * The controller resets to [Mode.ON_DEVICE] after every use (PlanSpec: cloud is opt-in PER
 * USE). The overlay bubble has no per-use cloud chip to tap, so when the user has switched on
 * "prefer cloud" in settings, the service re-arms cloud mode right before each [Cmd] from
 * [DictationController.micTapped] using the controller's own [DictationController.cloudToggleTapped]
 * — this never weakens the IME's default behavior, which never calls it.
 */
object OverlayCloudRearm {

    /** True when the service should call `controller.cloudToggleTapped(true)` before the tap. */
    fun shouldRearm(phase: DictationController.Phase, mode: Mode, cloudPreferred: Boolean, cloudEnabled: Boolean, loggedIn: Boolean): Boolean =
        phase == DictationController.Phase.IDLE &&
            mode == Mode.ON_DEVICE &&
            cloudPreferred &&
            cloudEnabled &&
            loggedIn
}
