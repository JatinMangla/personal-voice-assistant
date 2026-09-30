package io.github.jatinmangla.jarvis

import android.app.Application
import android.os.Build
import androidx.lifecycle.DefaultLifecycleObserver
import androidx.lifecycle.LifecycleOwner
import androidx.lifecycle.ProcessLifecycleOwner
import io.github.jatinmangla.jarvis.reminders.ReminderAlarms
import io.github.jatinmangla.jarvis.update.Updater
import kotlinx.coroutines.flow.MutableStateFlow

class JarvisApp : Application() {
    companion object {
        @Volatile
        var inForeground = false

        /** Set by the screen while listening/thinking/speaking: never update mid-answer. */
        @Volatile
        var busy = false

        /** A verified update waits in the cache. */
        @Volatile
        var updateReady = false

        val updateFailed = MutableStateFlow(false)
    }

    override fun onCreate() {
        super.onCreate()
        ReminderAlarms.createChannel(this)
        ProcessLifecycleOwner.get().lifecycle.addObserver(object : DefaultLifecycleObserver {
            override fun onStart(owner: LifecycleOwner) {
                inForeground = true
            }

            override fun onStop(owner: LifecycleOwner) {
                inForeground = false
                // Leaving the app is the quiet moment to update: on Android 12+ it installs
                // without a tap, and nobody sees the app restart.
                val updater = Updater(this@JarvisApp)
                if (updateReady && !busy && updater.enabled && updater.allowed() && Build.VERSION.SDK_INT >= 31 &&
                    updater.apkFile.exists()
                ) {
                    updateReady = false
                    runCatching { updater.install() }
                }
            }
        })
    }
}
