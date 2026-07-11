package net.hermes.voice

/**
 * Generation-bound holder for lifecycle-owned channels.
 *
 * A value captured before detach/re-attach must never be considered current
 * afterwards, even if queued work only reaches its target thread later.
 */
class BridgeGenerationGate<T : Any> {
    private var generation = 0L
    private var current: T? = null

    @Synchronized fun attach(value: T) {
        if (current === value) return
        generation += 1
        current = value
    }

    @Synchronized fun detach() {
        generation += 1
        current = null
    }

    @Synchronized fun snapshot(): Pair<T, Long>? =
        current?.let { it to generation }

    @Synchronized fun isCurrent(value: T, expectedGeneration: Long): Boolean =
        current === value && generation == expectedGeneration
}
