package io.github.jatinmangla.jarvis.screen

import android.annotation.SuppressLint
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.graphics.Bitmap
import android.graphics.PixelFormat
import android.graphics.drawable.GradientDrawable
import android.hardware.display.DisplayManager
import android.hardware.display.VirtualDisplay
import android.media.Image
import android.media.ImageReader
import android.media.projection.MediaProjection
import android.media.projection.MediaProjectionManager
import android.os.Build
import android.os.Handler
import android.os.HandlerThread
import android.os.IBinder
import android.os.Looper
import android.provider.Settings
import android.view.Gravity
import android.view.MotionEvent
import android.view.View
import android.view.WindowManager
import android.widget.FrameLayout
import android.widget.ImageView
import androidx.core.app.NotificationCompat
import io.github.jatinmangla.jarvis.JarvisApp
import io.github.jatinmangla.jarvis.MainActivity
import io.github.jatinmangla.jarvis.R
import kotlinx.coroutines.MainScope
import kotlinx.coroutines.cancel
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.launch
import java.io.ByteArrayOutputStream
import kotlin.math.abs
import kotlin.math.roundToInt

/** Shared state between the screen service and the voice loop. */
object ScreenShare {
    val active = MutableStateFlow(false)

    @Volatile
    private var snapshot: Pair<ByteArray, Long>? = null

    const val MAX_AGE_MS = 90_000L // like the server's SCREEN_MAX_AGE_SEC

    internal fun put(jpeg: ByteArray) {
        snapshot = jpeg to System.currentTimeMillis()
    }

    /** The snapshot for this question, if fresh; it is used once. */
    fun take(): ByteArray? {
        val s = snapshot
        snapshot = null
        return s?.takeIf { System.currentTimeMillis() - it.second < MAX_AGE_MS }?.first
    }

    fun overlayAllowed(context: Context) = Settings.canDrawOverlays(context)

    fun start(context: Context, resultCode: Int, data: Intent) {
        val intent = Intent(context, ScreenShareService::class.java)
            .setAction(ScreenShareService.ACTION_START)
            .putExtra(ScreenShareService.EXTRA_CODE, resultCode)
            .putExtra(ScreenShareService.EXTRA_DATA, data)
        context.startForegroundService(intent)
    }

    fun stop(context: Context) {
        context.startService(Intent(context, ScreenShareService::class.java).setAction(ScreenShareService.ACTION_STOP))
    }
}

/**
 * Screen sharing on the phone: Jarvis can look at the app you are in.
 *
 * How it works: while sharing, a small Jarvis bubble floats over other apps. Tap it in any
 * app: the bubble hides, ONE snapshot of the screen is taken, and Jarvis opens and listens;
 * your question and that snapshot go to Jarvis together (like the laptop page's screen
 * sharing). Frames are only looked at when you tap the bubble; nothing is recorded, saved or
 * sent otherwise. Android shows its own "sharing your screen" sign the whole time, and the
 * notification's Stop (or Android's own stop button) ends it.
 */
class ScreenShareService : Service() {
    companion object {
        const val ACTION_START = "start"
        const val ACTION_STOP = "stop"
        const val EXTRA_CODE = "code"
        const val EXTRA_DATA = "data"
        const val EXTRA_LISTEN = "io.github.jatinmangla.jarvis.LISTEN"
        private const val CHANNEL = "screen"
        private const val NOTIFICATION_ID = 7101
        private const val MAX_WIDTH = 900 // readable phone text, ~150-300 KB JPEG
    }

    private var projection: MediaProjection? = null
    private var display: VirtualDisplay? = null
    private var reader: ImageReader? = null
    private var thread: HandlerThread? = null
    private var handler: Handler? = null
    private var bubble: View? = null
    private val main = Handler(Looper.getMainLooper())
    private val scope = MainScope()
    private val lock = Any()
    private var latest: Image? = null // the newest frame, kept only until the next one
    private var width = 0
    private var height = 0

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        when (intent?.action) {
            ACTION_START -> start(intent)
            else -> stopSelf()
        }
        return START_NOT_STICKY
    }

    private fun start(intent: Intent) {
        val code = intent.getIntExtra(EXTRA_CODE, 0)
        @Suppress("DEPRECATION")
        val data: Intent? = if (Build.VERSION.SDK_INT >= 33) intent.getParcelableExtra(EXTRA_DATA, Intent::class.java)
        else intent.getParcelableExtra(EXTRA_DATA)
        // Android 14+: the foreground service must be running BEFORE getMediaProjection().
        startForeground(NOTIFICATION_ID, notification(), ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PROJECTION)
        if (data == null || projection != null) return
        val mpm = getSystemService(MediaProjectionManager::class.java)
        val p = try {
            mpm.getMediaProjection(code, data)
        } catch (e: Exception) {
            null
        } ?: run { stopSelf(); return }
        projection = p

        val t = HandlerThread("jarvis-screen").also { it.start() }
        thread = t
        handler = Handler(t.looper)
        p.registerCallback(object : MediaProjection.Callback() {
            override fun onStop() {
                main.post { stopSelf() } // Android's own "stop sharing"
            }
        }, handler)

        val wm = getSystemService(WindowManager::class.java)
        val (screenW, screenH) = if (Build.VERSION.SDK_INT >= 30) {
            wm.maximumWindowMetrics.bounds.let { it.width() to it.height() }
        } else {
            @Suppress("DEPRECATION")
            android.util.DisplayMetrics().also { wm.defaultDisplay.getRealMetrics(it) }.let { it.widthPixels to it.heightPixels }
        }
        val scale = minOf(1f, MAX_WIDTH.toFloat() / screenW)
        width = (screenW * scale).roundToInt()
        height = (screenH * scale).roundToInt()
        val dpi = (resources.displayMetrics.densityDpi * scale).roundToInt()
        val r = ImageReader.newInstance(width, height, PixelFormat.RGBA_8888, 3)
        r.setOnImageAvailableListener({ source ->
            val image = try {
                source.acquireLatestImage()
            } catch (e: IllegalStateException) {
                null
            } ?: return@setOnImageAvailableListener
            synchronized(lock) {
                latest?.close()
                latest = image
            }
        }, handler)
        reader = r
        display = p.createVirtualDisplay(
            "jarvis-screen", width, height, dpi, DisplayManager.VIRTUAL_DISPLAY_FLAG_AUTO_MIRROR,
            r.surface, null, handler,
        )
        ScreenShare.active.value = true
        showBubble()
        // The bubble is for OTHER apps: hide it while Jarvis itself is open.
        scope.launch { JarvisApp.foreground.collect { inJarvis -> bubble?.visibility = if (inJarvis) View.GONE else View.VISIBLE } }
    }

    /** Bubble tapped: hide it, wait for a frame without it, keep that one frame, open Jarvis. */
    private fun capture() {
        val b = bubble ?: return
        b.visibility = View.INVISIBLE
        handler?.postDelayed({
            val jpeg = synchronized(lock) { latest?.let { toJpeg(it) } }
            if (jpeg != null) ScreenShare.put(jpeg)
            main.post {
                val open = Intent(this, MainActivity::class.java)
                    .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_SINGLE_TOP)
                    .putExtra(EXTRA_LISTEN, true)
                startActivity(open)
            }
        }, 350)
    }

    private fun toJpeg(image: Image): ByteArray? = try {
        val plane = image.planes[0]
        val buffer = plane.buffer.also { it.rewind() }
        val rowPixels = plane.rowStride / plane.pixelStride
        val full = Bitmap.createBitmap(rowPixels, image.height, Bitmap.Config.ARGB_8888)
        full.copyPixelsFromBuffer(buffer)
        val cropped = if (rowPixels == image.width) full else Bitmap.createBitmap(full, 0, 0, image.width, image.height)
        val out = ByteArrayOutputStream()
        cropped.compress(Bitmap.CompressFormat.JPEG, 75, out)
        if (cropped !== full) cropped.recycle()
        full.recycle()
        out.toByteArray()
    } catch (e: Exception) {
        null
    }

    @SuppressLint("ClickableViewAccessibility")
    private fun showBubble() {
        if (!Settings.canDrawOverlays(this)) return
        val wm = getSystemService(WindowManager::class.java)
        val size = (56 * resources.displayMetrics.density).roundToInt()
        val icon = ImageView(this).apply {
            setImageResource(R.drawable.ic_notification)
            setColorFilter(0xFFFFFFFF.toInt())
            val pad = size / 4
            setPadding(pad, pad, pad, pad)
            background = GradientDrawable().apply { shape = GradientDrawable.OVAL; setColor(0xFF2563EB.toInt()) }
            contentDescription = "Ask Jarvis about this screen"
            elevation = 8f
        }
        val view = FrameLayout(this).apply { addView(icon, FrameLayout.LayoutParams(size, size)) }
        val params = WindowManager.LayoutParams(
            WindowManager.LayoutParams.WRAP_CONTENT, WindowManager.LayoutParams.WRAP_CONTENT,
            WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY,
            WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE or WindowManager.LayoutParams.FLAG_LAYOUT_NO_LIMITS,
            PixelFormat.TRANSLUCENT,
        ).apply {
            gravity = Gravity.TOP or Gravity.START
            x = resources.displayMetrics.widthPixels - size - 16
            y = resources.displayMetrics.heightPixels / 3
        }
        var downX = 0f
        var downY = 0f
        var startX = 0
        var startY = 0
        view.setOnTouchListener { v, e ->
            when (e.action) {
                MotionEvent.ACTION_DOWN -> {
                    downX = e.rawX; downY = e.rawY; startX = params.x; startY = params.y; true
                }
                MotionEvent.ACTION_MOVE -> {
                    params.x = startX + (e.rawX - downX).roundToInt()
                    params.y = startY + (e.rawY - downY).roundToInt()
                    wm.updateViewLayout(v, params); true
                }
                MotionEvent.ACTION_UP -> {
                    if (abs(e.rawX - downX) < 20 && abs(e.rawY - downY) < 20) capture()
                    true
                }
                else -> false
            }
        }
        wm.addView(view, params)
        bubble = view
    }

    private fun notification(): Notification {
        val nm = getSystemService(NotificationManager::class.java)
        nm.createNotificationChannel(NotificationChannel(CHANNEL, "Screen sharing", NotificationManager.IMPORTANCE_LOW))
        val stop = PendingIntent.getService(
            this, 1, Intent(this, ScreenShareService::class.java).setAction(ACTION_STOP),
            PendingIntent.FLAG_IMMUTABLE,
        )
        val open = PendingIntent.getActivity(this, 2, Intent(this, MainActivity::class.java), PendingIntent.FLAG_IMMUTABLE)
        return NotificationCompat.Builder(this, CHANNEL)
            .setSmallIcon(R.drawable.ic_notification)
            .setContentTitle("Jarvis screen sharing is on")
            .setContentText("Tap the Jarvis bubble in any app to ask about that screen.")
            .setContentIntent(open)
            .addAction(0, "Stop sharing", stop)
            .setOngoing(true)
            .build()
    }

    override fun onDestroy() {
        scope.cancel()
        ScreenShare.active.value = false
        ScreenShare.take() // drop any unused snapshot
        bubble?.let { runCatching { getSystemService(WindowManager::class.java).removeView(it) } }
        bubble = null
        display?.release()
        synchronized(lock) {
            latest?.close()
            latest = null
        }
        reader?.close()
        projection?.stop()
        projection = null
        thread?.quitSafely()
        super.onDestroy()
    }
}
