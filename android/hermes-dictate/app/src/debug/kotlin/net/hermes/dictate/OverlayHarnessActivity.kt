package net.hermes.dictate

import android.app.Activity
import android.os.Bundle
import android.view.Gravity
import android.view.View
import android.widget.FrameLayout
import android.widget.ImageButton
import android.widget.TextView
import androidx.core.content.ContextCompat

/** Debug-only host for deterministic instrumentation of the exact production pill layout. */
class OverlayHarnessActivity : Activity() {
    private lateinit var pill: View

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        pill = layoutInflater.inflate(R.layout.overlay_pill, null)
        val stage = FrameLayout(this).apply {
            setBackgroundColor(ContextCompat.getColor(context, R.color.kb_bg))
            addView(
                pill,
                FrameLayout.LayoutParams(
                    OverlayGeometry.pillWidthPx(resources.displayMetrics.widthPixels, resources.displayMetrics.density),
                    OverlayGeometry.pillHeightPx(resources.displayMetrics.density),
                    Gravity.CENTER,
                ),
            )
        }
        setContentView(stage)
        instance = this
        val requested = intent.getStringExtra(EXTRA_STATUS)?.let(::statusFromWire)
        if (requested != null) {
            drive(
                requested,
                partial = intent.getStringExtra(EXTRA_PARTIAL),
                level = intent.getIntExtra(EXTRA_LEVEL, 0),
                retryAvailable = intent.getBooleanExtra(EXTRA_RETRY, false),
            )
        }
    }

    override fun onDestroy() {
        if (instance === this) instance = null
        super.onDestroy()
    }

    fun drive(status: UiStatus, partial: String? = null, level: Int = 0, retryAvailable: Boolean = false) {
        val presentation = OverlayViewState.from(
            status = status,
            previewText = partial.orEmpty(),
            committedText = partial,
            retryAvailable = retryAvailable,
            errorText = { getString(R.string.err_recognizer_other) },
        )
        val (textId, colorId) = when (presentation.label) {
            OverlayLabel.READY -> R.string.status_idle to R.color.accent
            OverlayLabel.LISTENING -> R.string.status_listening to R.color.listening
            OverlayLabel.PROCESSING -> R.string.status_processing to R.color.processing
            OverlayLabel.DONE -> R.string.status_done to R.color.state_ok
            OverlayLabel.RECORDING -> R.string.status_recording to R.color.cloud
            OverlayLabel.UPLOADING -> R.string.status_uploading to R.color.cloud
            OverlayLabel.ERROR -> R.string.overlay_status_error to R.color.status_error
            OverlayLabel.COPY -> R.string.overlay_status_copy to R.color.state_ok
        }
        pill.findViewById<TextView>(R.id.pill_status).apply {
            text = getString(textId)
            setTextColor(ContextCompat.getColor(context, colorId))
        }
        pill.findViewById<TextView>(R.id.pill_text).apply {
            text = OverlayText.visibleBody(presentation) ?: getString(R.string.overlay_speak_now)
            contentDescription = presentation.body ?: getString(R.string.overlay_speak_now)
        }
        pill.findViewById<OverlayLevelView>(R.id.pill_level).apply {
            this.level = level
            fillColor = ContextCompat.getColor(context, colorId)
        }
        val semantics = OverlayActionSemantics.from(presentation)
        pill.findViewById<ImageButton>(R.id.pill_cancel).apply {
            applyActionState(presentation.cancelEnabled)
            contentDescription = getString(semantics.cancelDescription)
        }
        pill.findViewById<ImageButton>(R.id.pill_hermes).apply {
            applyActionState(presentation.hermesEnabled)
            contentDescription = getString(R.string.send_to_hermes)
        }
        pill.findViewById<ImageButton>(R.id.pill_confirm).apply {
            applyActionState(presentation.confirmAction != OverlayConfirmAction.NONE)
            contentDescription = getString(semantics.confirmDescription)
            setImageResource(semantics.confirmIcon)
        }
    }

    private fun View.applyActionState(enabled: Boolean) {
        isEnabled = enabled
        isClickable = enabled
        alpha = if (enabled) 1f else DISABLED_ACTION_ALPHA
    }

    fun snapshot(): Snapshot = Snapshot(
        status = pill.findViewById<TextView>(R.id.pill_status).text.toString(),
        text = pill.findViewById<TextView>(R.id.pill_text).text.toString(),
        level = pill.findViewById<OverlayLevelView>(R.id.pill_level).level,
        width = pill.width,
        height = pill.height,
        cancelEnabled = pill.findViewById<ImageButton>(R.id.pill_cancel).isEnabled,
        hermesEnabled = pill.findViewById<ImageButton>(R.id.pill_hermes).isEnabled,
        confirmEnabled = pill.findViewById<ImageButton>(R.id.pill_confirm).isEnabled,
        cancelDescription = pill.findViewById<ImageButton>(R.id.pill_cancel).contentDescription.toString(),
        confirmDescription = pill.findViewById<ImageButton>(R.id.pill_confirm).contentDescription.toString(),
    )

    data class Snapshot(
        val status: String,
        val text: String,
        val level: Int,
        val width: Int,
        val height: Int,
        val cancelEnabled: Boolean,
        val hermesEnabled: Boolean,
        val confirmEnabled: Boolean,
        val cancelDescription: String,
        val confirmDescription: String,
    )

    companion object {
        const val EXTRA_STATUS = "status"
        const val EXTRA_PARTIAL = "partial"
        const val EXTRA_LEVEL = "level"
        const val EXTRA_RETRY = "retry"

        private const val DISABLED_ACTION_ALPHA = 0.28f

        @Volatile
        var instance: OverlayHarnessActivity? = null

        private fun statusFromWire(value: String): UiStatus? = when (value.uppercase()) {
            "LISTENING" -> UiStatus.Listening
            "PROCESSING" -> UiStatus.Processing
            "DONE" -> UiStatus.Done
            "RECORDING" -> UiStatus.Recording
            "UPLOADING" -> UiStatus.Uploading
            "ERROR" -> UiStatus.Failed(ErrorKind.RECOGNIZER_OTHER)
            else -> null
        }
    }
}
