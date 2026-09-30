package io.github.jatinmangla.jarvis

import android.Manifest
import android.content.ActivityNotFoundException
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.provider.Settings
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.activity.result.contract.ActivityResultContracts
import androidx.activity.viewModels
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.material3.Surface
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.ui.Modifier
import android.media.projection.MediaProjectionManager
import androidx.core.content.ContextCompat
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.lifecycle.lifecycleScope
import androidx.lifecycle.repeatOnLifecycle
import io.github.jatinmangla.jarvis.screen.ScreenShare
import io.github.jatinmangla.jarvis.screen.ScreenShareService
import kotlinx.coroutines.launch
import com.journeyapps.barcodescanner.ScanContract
import com.journeyapps.barcodescanner.ScanOptions
import io.github.jatinmangla.jarvis.ui.ChatActions
import io.github.jatinmangla.jarvis.ui.ChatScreen
import io.github.jatinmangla.jarvis.ui.JarvisTheme
import io.github.jatinmangla.jarvis.ui.PairScreen

class MainActivity : ComponentActivity(), ChatActions {
    private val vm: JarvisViewModel by viewModels()

    private val micPermission = registerForActivityResult(ActivityResultContracts.RequestPermission()) { granted ->
        if (granted) vm.onMic() else vm.addNote("Jarvis needs the microphone to hear you: allow it in the app's settings.")
    }
    private val notificationPermission = registerForActivityResult(ActivityResultContracts.RequestPermission()) {}
    private val scan = registerForActivityResult(ScanContract()) { result ->
        result.contents?.let { vm.pair(it) }
    }
    private val listenPermission = registerForActivityResult(ActivityResultContracts.RequestPermission()) { granted ->
        if (granted) vm.listenNow()
    }
    private val screenConsent = registerForActivityResult(ActivityResultContracts.StartActivityForResult()) { result ->
        val data = result.data
        if (result.resultCode == RESULT_OK && data != null) {
            ScreenShare.start(this, result.resultCode, data)
            vm.addNote("Screen sharing is on. Open any app and tap the round Jarvis bubble to ask about " +
                "that screen. Jarvis sees one snapshot per question; nothing is recorded or saved.")
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()
        lifecycleScope.launch {
            repeatOnLifecycle(Lifecycle.State.STARTED) { vm.openVideo.collect { openVideo(it) } }
        }
        setContent {
            JarvisTheme {
                Surface(Modifier.fillMaxSize()) {
                    val state by vm.state.collectAsStateWithLifecycle()
                    val sharing by ScreenShare.active.collectAsStateWithLifecycle()
                    val paired = state.pairing != null
                    LaunchedEffect(paired) { if (paired) askForNotifications() }
                    if (state.pairing == null) {
                        PairScreen(state.pairMessage, state.pairingBusy, ::startScan)
                    } else {
                        ChatScreen(state, sharing, this)
                    }
                }
            }
        }
        handleListen(intent)
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        handleListen(intent)
    }

    /** Opened by the screen-sharing bubble: listen at once. */
    private fun handleListen(intent: Intent?) {
        if (intent?.getBooleanExtra(ScreenShareService.EXTRA_LISTEN, false) != true) return
        intent.removeExtra(ScreenShareService.EXTRA_LISTEN)
        if (ContextCompat.checkSelfPermission(this, Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED) {
            vm.listenNow()
        } else {
            listenPermission.launch(Manifest.permission.RECORD_AUDIO)
        }
    }

    override fun screen() {
        if (ScreenShare.active.value) {
            ScreenShare.stop(this)
            vm.addNote("Screen sharing is off.")
            return
        }
        if (!ScreenShare.overlayAllowed(this)) {
            // Once: Settings -> Display over other apps -> Jarvis -> Allow (for the bubble).
            vm.addNote("Allow Jarvis to show its bubble over other apps (once), then tap Screen again.")
            startActivity(Intent(Settings.ACTION_MANAGE_OVERLAY_PERMISSION, Uri.parse("package:$packageName")))
            return
        }
        screenConsent.launch(getSystemService(MediaProjectionManager::class.java).createScreenCaptureIntent())
    }

    override fun onStart() {
        super.onStart()
        vm.onVisible()
    }

    override fun onStop() {
        vm.onHidden()
        super.onStop()
    }

    private fun startScan() {
        scan.launch(
            ScanOptions()
                .setDesiredBarcodeFormats(ScanOptions.QR_CODE)
                .setPrompt("Point at the pairing code on the laptop page")
                .setBeepEnabled(false)
                .setOrientationLocked(false)
        )
    }

    /** Reminders ring as notifications (Android 13+ asks once, after pairing). */
    private fun askForNotifications() {
        if (Build.VERSION.SDK_INT >= 33 && vm.state.value.pairing != null &&
            ContextCompat.checkSelfPermission(this, Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED
        ) notificationPermission.launch(Manifest.permission.POST_NOTIFICATIONS)
    }

    // --- ChatActions ---------------------------------------------------------------------
    override fun mic() {
        if (ContextCompat.checkSelfPermission(this, Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED) {
            vm.onMic()
        } else {
            micPermission.launch(Manifest.permission.RECORD_AUDIO)
        }
    }

    override fun whisper() = vm.toggleWhisper()
    override fun clear() = vm.clearChat()
    override fun rate(item: ChatItem.Rating, value: Int) = vm.rate(item, value)
    override fun checkUpdates() = vm.checkUpdates(force = true)
    override fun unpair() = vm.unpair()

    override fun openUrl(url: String) {
        if (!url.startsWith("https://", true) && !url.startsWith("http://", true)) return
        try {
            startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(url)))
        } catch (e: ActivityNotFoundException) {
            vm.addNote("No app on this phone can open that link.")
        }
    }

    /** The YouTube app if it is installed, otherwise the browser. */
    override fun openVideo(videoId: String) {
        try {
            startActivity(Intent(Intent.ACTION_VIEW, Uri.parse("vnd.youtube:$videoId")))
        } catch (e: ActivityNotFoundException) {
            openUrl("https://www.youtube.com/watch?v=$videoId")
        }
    }

    override fun installUpdate() {
        if (!vm.updateAllowed()) {
            // Once: Settings -> Install unknown apps -> Jarvis -> Allow. Later updates need no tap.
            vm.addNote("Allow Jarvis to install its updates (once), then tap Install again.")
            startActivity(Intent(Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES, Uri.parse("package:$packageName")))
            return
        }
        vm.installUpdate()
    }
}
