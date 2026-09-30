package io.github.jatinmangla.jarvis.update

import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.pm.PackageInstaller
import android.content.pm.PackageManager
import android.content.pm.Signature
import android.os.Build
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import io.github.jatinmangla.jarvis.BuildConfig
import io.github.jatinmangla.jarvis.JarvisApp
import io.github.jatinmangla.jarvis.R
import io.github.jatinmangla.jarvis.net.Api
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.Request
import java.io.File
import java.io.IOException
import java.security.MessageDigest

/**
 * Self-update from the public releases repo (no Play Store, no GitHub login):
 * 1. read latest.json; newer versionCode -> download the APK into the app's private cache;
 * 2. check its size and SHA-256 against latest.json, and that it is signed by the SAME
 *    certificate as this installed app (Android enforces that too; checking first gives a
 *    clear message and never hands a foreign APK to the installer);
 * 3. install with PackageInstaller. On Android 12+ an app may update ITSELF without a tap
 *    (UPDATE_PACKAGES_WITHOUT_USER_ACTION) once "install unknown apps" is allowed for it.
 */
class Updater(private val context: Context) {
    class UpdateError(message: String) : Exception(message)

    private val dir = File(context.cacheDir, "updates")
    val apkFile = File(dir, "jarvis-update.apk")

    /** Only real (release) builds update themselves; debug builds are a different package. */
    val enabled: Boolean get() = !BuildConfig.DEBUG

    /** The newer release, or null when this app is up to date. Throws IOException offline. */
    suspend fun check(): UpdateInfo? = withContext(Dispatchers.IO) {
        val req = Request.Builder().url(UpdateInfo.LATEST_URL).header("Cache-Control", "no-cache").build()
        Api.client.newCall(req).execute().use { r ->
            if (!r.isSuccessful) throw IOException("HTTP ${r.code}")
            val info = UpdateInfo.parse(r.body?.string().orEmpty()) ?: throw IOException("bad latest.json")
            info.takeIf { it.versionCode > BuildConfig.VERSION_CODE }
        }
    }

    /** Download and verify; returns the verified APK file. */
    suspend fun download(info: UpdateInfo): File = withContext(Dispatchers.IO) {
        dir.mkdirs()
        val part = File(dir, "download.part")
        val digest = MessageDigest.getInstance("SHA-256")
        Api.client.newCall(Request.Builder().url(info.apk).build()).execute().use { r ->
            if (!r.isSuccessful) throw IOException("HTTP ${r.code}")
            val body = r.body ?: throw IOException("empty")
            var total = 0L
            part.outputStream().use { out ->
                body.byteStream().use { input ->
                    val buf = ByteArray(64 * 1024)
                    while (true) {
                        val n = input.read(buf)
                        if (n < 0) break
                        total += n
                        if (total > info.size) throw UpdateError("The download is larger than announced.")
                        digest.update(buf, 0, n)
                        out.write(buf, 0, n)
                    }
                }
            }
            if (total != info.size) throw UpdateError("The download is incomplete.")
        }
        if (hex(digest.digest()) != info.sha256) {
            part.delete()
            throw UpdateError("The download's checksum is wrong; not installing it.")
        }
        checkSigner(part)
        apkFile.delete()
        if (!part.renameTo(apkFile)) throw IOException("rename failed")
        apkFile
    }

    private fun checkSigner(apk: File) {
        val pm = context.packageManager
        val archive = pm.getPackageArchiveInfo(apk.path, PackageManager.GET_SIGNING_CERTIFICATES)
            ?: throw UpdateError("The download is not a valid app.")
        if (archive.packageName != context.packageName) throw UpdateError("The download is a different app.")
        val installed = pm.getPackageInfo(context.packageName, PackageManager.GET_SIGNING_CERTIFICATES)
        val theirs = certs(archive.signingInfo?.apkContentsSigners)
        val ours = certs(installed.signingInfo?.apkContentsSigners)
        // Some Android versions don't read signatures of an uninstalled file; then Android's
        // own check at install time (same signer required) is what protects the update.
        if (theirs.isNotEmpty() && theirs != ours) throw UpdateError("The download is signed by someone else; not installing it.")
    }

    private fun certs(signers: Array<Signature>?): Set<String> =
        signers.orEmpty().map { hex(MessageDigest.getInstance("SHA-256").digest(it.toByteArray())) }.toSet()

    private fun hex(bytes: ByteArray) = bytes.joinToString("") { "%02x".format(it) }

    /** May this app install updates (Settings -> Install unknown apps -> Jarvis)? */
    fun allowed(): Boolean = context.packageManager.canRequestPackageInstalls()

    /** Hand the verified APK to Android's installer. The result arrives at InstallReceiver. */
    fun install(apk: File = apkFile) {
        val installer = context.packageManager.packageInstaller
        val params = PackageInstaller.SessionParams(PackageInstaller.SessionParams.MODE_FULL_INSTALL).apply {
            setAppPackageName(context.packageName)
            setSize(apk.length())
            if (Build.VERSION.SDK_INT >= 31) setRequireUserAction(PackageInstaller.SessionParams.USER_ACTION_NOT_REQUIRED)
        }
        val id = installer.createSession(params)
        installer.openSession(id).use { session ->
            session.openWrite("jarvis.apk", 0, apk.length()).use { out ->
                apk.inputStream().use { it.copyTo(out) }
                session.fsync(out)
            }
            val intent = Intent(context, InstallReceiver::class.java)
            val flags = PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_MUTABLE // the system adds the result
            session.commit(PendingIntent.getBroadcast(context, id, intent, flags).intentSender)
        }
    }
}

/** Installer results. If Android wants a tap (first update, or before Android 12), open its
 *  screen when the app is in front, else post a notification that opens it. */
class InstallReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        when (intent.getIntExtra(PackageInstaller.EXTRA_STATUS, PackageInstaller.STATUS_FAILURE)) {
            PackageInstaller.STATUS_PENDING_USER_ACTION -> {
                @Suppress("DEPRECATION")
                val confirm = (if (Build.VERSION.SDK_INT >= 33) intent.getParcelableExtra(Intent.EXTRA_INTENT, Intent::class.java)
                else intent.getParcelableExtra(Intent.EXTRA_INTENT)) ?: return
                confirm.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
                if (JarvisApp.inForeground) {
                    context.startActivity(confirm)
                } else {
                    notifyTap(context, confirm)
                }
            }
            PackageInstaller.STATUS_SUCCESS -> Unit // the app restarts as the new version
            else -> JarvisApp.updateFailed.value = true
        }
    }

    private fun notifyTap(context: Context, confirm: Intent) {
        val nm = context.getSystemService(NotificationManager::class.java)
        nm.createNotificationChannel(NotificationChannel("updates", "App updates", NotificationManager.IMPORTANCE_DEFAULT))
        val tap = PendingIntent.getActivity(context, 1, confirm, PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
        val n = NotificationCompat.Builder(context, "updates")
            .setSmallIcon(R.drawable.ic_notification)
            .setContentTitle("Jarvis update ready")
            .setContentText("Tap to install the new version.")
            .setContentIntent(tap)
            .setAutoCancel(true)
            .build()
        try {
            NotificationManagerCompat.from(context).notify(7001, n)
        } catch (e: SecurityException) {
            // notifications not allowed: the banner in the app offers the update again
        }
    }
}
