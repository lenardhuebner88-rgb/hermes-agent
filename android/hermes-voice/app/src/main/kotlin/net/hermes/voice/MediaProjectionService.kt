package net.hermes.voice

import android.app.Activity
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Intent
import android.content.pm.ServiceInfo
import android.graphics.Bitmap
import android.graphics.PixelFormat
import android.hardware.display.DisplayManager
import android.hardware.display.VirtualDisplay
import android.media.ImageReader
import android.media.projection.MediaProjection
import android.media.projection.MediaProjectionManager
import android.os.Handler
import android.os.HandlerThread
import android.os.IBinder
import android.util.Base64
import android.util.Log
import java.io.ByteArrayOutputStream
import java.nio.ByteBuffer
import java.util.concurrent.atomic.AtomicBoolean
import kotlin.math.roundToInt

/**
 * Foreground service owning the MediaProjection/VirtualDisplay/ImageReader lifecycle. Polls at
 * most 1 frame/second, scales + JPEG-encodes it under a byte budget, and forwards it to the web
 * page through [HermesBridge]. Never logs frame bytes.
 */
class MediaProjectionService : Service() {

    companion object {
        const val ACTION_START = "net.hermes.voice.action.START_CAPTURE"
        const val ACTION_STOP = "net.hermes.voice.action.STOP_CAPTURE"
        const val EXTRA_RESULT_CODE = "net.hermes.voice.extra.RESULT_CODE"
        const val EXTRA_RESULT_DATA = "net.hermes.voice.extra.RESULT_DATA"

        private const val TAG = "MediaProjectionService"
        private const val NOTIFICATION_ID = 1001
        private const val CHANNEL_ID = "screen_sharing"
        private const val MAX_EDGE_PX = 1024
        private const val MAX_FRAME_BYTES = 512 * 1024
        private const val MAX_LADDER_STEPS = 6
        private const val POLL_INTERVAL_MS = 1000L
        private const val INITIAL_QUALITY_PERCENT = 70
    }

    private var mediaProjection: MediaProjection? = null
    private var virtualDisplay: VirtualDisplay? = null
    private var imageReader: ImageReader? = null
    private var handlerThread: HandlerThread? = null
    private var handler: Handler? = null

    private var currentWidth = 0
    private var currentHeight = 0
    private var currentDpi = 0

    @Volatile
    private var framesPaused = false

    /** Guards the one idempotent stop path against re-entry from any of its five triggers. */
    private val stopGate = AtomicBoolean(false)

    private var mediaProjectionCallback: MediaProjection.Callback? = null

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        ensureNotificationChannel()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        when (intent?.action) {
            ACTION_START -> handleStart(intent)
            ACTION_STOP -> stopCapture(reason = "user")
            else -> stopCapture(reason = "error")
        }
        return START_NOT_STICKY
    }

    override fun onDestroy() {
        stopCapture(reason = "lifecycle")
        super.onDestroy()
    }

    private fun handleStart(intent: Intent) {
        val resultCode = intent.getIntExtra(EXTRA_RESULT_CODE, Activity.RESULT_CANCELED)
        @Suppress("DEPRECATION")
        val resultData = intent.getParcelableExtra<Intent>(EXTRA_RESULT_DATA)
        if (resultCode != Activity.RESULT_OK || resultData == null) {
            stopCapture(reason = "error")
            return
        }
        if (HermesBridge.captureState.state != CaptureState.STARTING) {
            // A stop raced ahead of this start (e.g. the state machine already moved back to
            // IDLE while the consent dialog was open, or a competing stop arrived between the
            // Activity's advanceToStarting() and this service actually running). Do not create
            // a projection for a session the state machine no longer recognizes.
            stopCapture(reason = "error")
            return
        }

        startForeground(
            NOTIFICATION_ID,
            buildNotification(),
            ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PROJECTION or
                ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE,
        )

        val projectionManager = getSystemService(MediaProjectionManager::class.java)
        val projection = try {
            projectionManager.getMediaProjection(resultCode, resultData)
        } catch (e: SecurityException) {
            Log.w(TAG, "getMediaProjection rejected", e)
            stopCapture(reason = "error")
            return
        }
        if (projection == null) {
            stopCapture(reason = "error")
            return
        }
        mediaProjection = projection

        val thread = HandlerThread("hermes-voice-capture").also { it.start() }
        handlerThread = thread
        val workHandler = Handler(thread.looper)
        handler = workHandler

        val callback = object : MediaProjection.Callback() {
            override fun onStop() {
                stopCapture(reason = "system")
            }

            override fun onCapturedContentResize(width: Int, height: Int) {
                workHandler.post { handleContentResize(width, height) }
            }

            override fun onCapturedContentVisibilityChanged(isVisible: Boolean) {
                framesPaused = !isVisible
            }
        }
        mediaProjectionCallback = callback
        projection.registerCallback(callback, workHandler)

        val started = try {
            setUpCaptureSurfaces(projection, workHandler)
            true
        } catch (e: Exception) {
            Log.w(TAG, "failed to start capture surfaces", e)
            false
        }

        if (!started) {
            stopCapture(reason = "error")
            return
        }

        if (!HermesBridge.captureState.advanceToCapturing()) {
            // Another stop raced in between surface setup and here (e.g. a competing stop
            // intent processed just before this point). The surfaces we just created must not
            // be left running silently — tear them down through the one idempotent stop path
            // instead of proceeding as if capture had started.
            stopCapture(reason = "error")
            return
        }
        HermesBridge.send(NativeToWebMessage.ScreenCaptureStarted)
        schedulePoll(workHandler)
    }

    private fun setUpCaptureSurfaces(projection: MediaProjection, workHandler: Handler) {
        val metrics = resources.displayMetrics
        val dpi = metrics.densityDpi
        // Capture DOWNSCALED at the source: the VirtualDisplay/ImageReader pipeline scales for
        // free, so this avoids ever allocating full-resolution buffers (full-res ImageReader +
        // raw copy + tight copy + ARGB bitmap can exceed ~85MiB on a 1440x3120 phone before any
        // downscaling even happens).
        val (scaledWidth, scaledHeight) = FrameScaler.computeScaledDimensions(
            metrics.widthPixels,
            metrics.heightPixels,
            MAX_EDGE_PX,
        )

        val reader = ImageReader.newInstance(scaledWidth, scaledHeight, PixelFormat.RGBA_8888, 2)
        imageReader = reader

        // createVirtualDisplay() must be called exactly once per MediaProjection.
        virtualDisplay = projection.createVirtualDisplay(
            "hermes-voice-capture",
            scaledWidth,
            scaledHeight,
            dpi,
            DisplayManager.VIRTUAL_DISPLAY_FLAG_AUTO_MIRROR,
            reader.surface,
            null,
            workHandler,
        )
        currentWidth = scaledWidth
        currentHeight = scaledHeight
        currentDpi = dpi
    }

    private fun handleContentResize(width: Int, height: Int) {
        val display = virtualDisplay ?: return
        val (scaledWidth, scaledHeight) = FrameScaler.computeScaledDimensions(
            width,
            height,
            MAX_EDGE_PX,
        )
        val oldReader = imageReader
        val newReader = ImageReader.newInstance(scaledWidth, scaledHeight, PixelFormat.RGBA_8888, 2)
        imageReader = newReader
        display.resize(scaledWidth, scaledHeight, currentDpi)
        display.surface = newReader.surface
        currentWidth = scaledWidth
        currentHeight = scaledHeight
        oldReader?.close()
    }

    private fun schedulePoll(workHandler: Handler) {
        val runnable = object : Runnable {
            override fun run() {
                if (stopGate.get()) return
                pollFrame()
                workHandler.postDelayed(this, POLL_INTERVAL_MS)
            }
        }
        workHandler.postDelayed(runnable, POLL_INTERVAL_MS)
    }

    private fun pollFrame() {
        if (framesPaused) return
        val reader = imageReader ?: return
        val image = try {
            reader.acquireLatestImage()
        } catch (e: Exception) {
            null
        } ?: return

        try {
            val plane = image.planes[0]
            val rowStride = plane.rowStride
            val pixelStride = plane.pixelStride
            val width = image.width
            val height = image.height

            val buffer: ByteBuffer = plane.buffer
            val raw = ByteArray(buffer.remaining())
            buffer.get(raw)
            val tight = RowPadding.stripRowPadding(raw, width, height, rowStride, pixelStride)

            val bitmap = Bitmap.createBitmap(width, height, Bitmap.Config.ARGB_8888)
            bitmap.copyPixelsFromBuffer(ByteBuffer.wrap(tight))

            val jpeg = encodeWithinBudget(bitmap, width, height)
            bitmap.recycle()

            if (jpeg != null) {
                val base64 = Base64.encodeToString(jpeg, Base64.NO_WRAP)
                HermesBridge.send(NativeToWebMessage.ScreenFrame(base64))
            }
        } finally {
            image.close()
        }
    }

    /**
     * Scales to <= [MAX_EDGE_PX] longest edge, then encodes at [INITIAL_QUALITY_PERCENT]. If the
     * result exceeds [MAX_FRAME_BYTES], walks [FrameScaler.stepDownLadder] (lower quality, then
     * shrinking dimensions) up to [MAX_LADDER_STEPS] attempts. Returns null (drop the frame) if
     * still over budget.
     */
    private fun encodeWithinBudget(source: Bitmap, srcWidth: Int, srcHeight: Int): ByteArray? {
        val (baseWidth, baseHeight) = FrameScaler.computeScaledDimensions(
            srcWidth,
            srcHeight,
            MAX_EDGE_PX,
        )
        val base = scaledBitmap(source, baseWidth, baseHeight)
        val firstAttempt = compress(base, INITIAL_QUALITY_PERCENT)
        if (firstAttempt.size <= MAX_FRAME_BYTES) {
            if (base !== source) base.recycle()
            return firstAttempt
        }

        var steps = 0
        for (step in FrameScaler.stepDownLadder()) {
            if (steps >= MAX_LADDER_STEPS) break
            steps++

            val stepWidth = (baseWidth * step.scale).roundToInt().coerceAtLeast(1)
            val stepHeight = (baseHeight * step.scale).roundToInt().coerceAtLeast(1)
            val stepBitmap = scaledBitmap(base, stepWidth, stepHeight)
            val qualityPercent = (step.quality * 100).roundToInt()
            val encoded = compress(stepBitmap, qualityPercent)
            if (stepBitmap !== base) stepBitmap.recycle()

            if (encoded.size <= MAX_FRAME_BYTES) {
                if (base !== source) base.recycle()
                return encoded
            }
        }

        if (base !== source) base.recycle()
        return null
    }

    private fun scaledBitmap(source: Bitmap, width: Int, height: Int): Bitmap {
        if (width == source.width && height == source.height) return source
        return Bitmap.createScaledBitmap(source, width, height, true)
    }

    private fun compress(bitmap: Bitmap, qualityPercent: Int): ByteArray {
        val out = ByteArrayOutputStream()
        bitmap.compress(Bitmap.CompressFormat.JPEG, qualityPercent.coerceIn(1, 100), out)
        return out.toByteArray()
    }

    /**
     * The one idempotent stop path. Reached from: the notification's Stop action, the
     * MediaProjection.Callback#onStop (system chip / lockscreen / competing projection), a
     * bridge stop_screen_capture message, WebView/Activity destruction and Service#onDestroy.
     * Safe to call more than once and from any state.
     */
    private fun stopCapture(reason: String) {
        if (!stopGate.compareAndSet(false, true)) return

        handler?.removeCallbacksAndMessages(null)

        imageReader?.close()
        imageReader = null

        virtualDisplay?.release()
        virtualDisplay = null

        mediaProjectionCallback?.let { mediaProjection?.unregisterCallback(it) }
        mediaProjectionCallback = null
        mediaProjection?.stop()
        mediaProjection = null

        handlerThread?.quitSafely()
        handlerThread = null
        handler = null

        stopForeground(STOP_FOREGROUND_REMOVE)

        HermesBridge.captureState.stop()
        HermesBridge.captureState.finishStop()
        if (HermesBridge.isAlive()) {
            HermesBridge.send(NativeToWebMessage.ScreenCaptureStopped(reason))
        }

        stopSelf()
    }

    private fun ensureNotificationChannel() {
        val manager = getSystemService(NotificationManager::class.java)
        val channel = NotificationChannel(
            CHANNEL_ID,
            getString(R.string.notification_channel_screen_sharing),
            NotificationManager.IMPORTANCE_LOW,
        )
        manager.createNotificationChannel(channel)
    }

    private fun buildNotification(): Notification {
        val stopIntent = Intent(this, MediaProjectionService::class.java).setAction(ACTION_STOP)
        val stopPendingIntent = PendingIntent.getService(
            this,
            0,
            stopIntent,
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
        )

        return Notification.Builder(this, CHANNEL_ID)
            .setContentTitle(getString(R.string.notification_title_sharing))
            .setContentText(getString(R.string.notification_text_sharing))
            .setSmallIcon(android.R.drawable.ic_menu_share)
            .setOngoing(true)
            .addAction(
                Notification.Action.Builder(
                    android.R.drawable.ic_menu_close_clear_cancel,
                    getString(R.string.notification_action_stop),
                    stopPendingIntent,
                ).build(),
            )
            .build()
    }
}
