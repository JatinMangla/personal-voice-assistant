package io.github.jatinmangla.jarvis.reminders

import android.Manifest
import android.app.AlarmManager
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import androidx.core.content.ContextCompat
import io.github.jatinmangla.jarvis.MainActivity
import io.github.jatinmangla.jarvis.R
import io.github.jatinmangla.jarvis.SecureStore
import org.json.JSONArray
import java.time.LocalDateTime

/**
 * Local reminder notifications: the phone rings at the due time by itself, even while
 * Jarvis (on Kaggle) is off. The list comes from Jarvis (/api/app/reminders) whenever the
 * app is open while Jarvis runs, and is kept encrypted on the phone (SecureStore). Alarms
 * carry only the reminder's id; the text is read from the encrypted list when it rings.
 */
object ReminderAlarms {
    const val CHANNEL = "reminders"
    private const val MAX_ALARMS = 60
    private const val EXTRA_ID = "reminder_id"

    fun createChannel(context: Context) {
        val channel = NotificationChannel(CHANNEL, "Reminders", NotificationManager.IMPORTANCE_HIGH).apply {
            description = "Your Jarvis reminders, at the due time"
            lockscreenVisibility = android.app.Notification.VISIBILITY_PRIVATE
        }
        context.getSystemService(NotificationManager::class.java).createNotificationChannel(channel)
    }

    /** Replace the stored list and all alarms with [list]. */
    fun sync(context: Context, list: List<PhoneReminder>, fingerprint: String?) {
        val store = SecureStore(context)
        store.remindersJson = PhoneReminder.listToJson(list)
        store.remindersFingerprint = fingerprint
        reschedule(context)
    }

    fun stored(context: Context): List<PhoneReminder> =
        PhoneReminder.listFromJson(SecureStore(context).remindersJson?.let { runCatching { JSONArray(it) }.getOrNull() })

    /** Cancel every alarm and set them again from the stored list (also after a reboot,
     *  an app update, or a clock/time-zone change). */
    fun reschedule(context: Context) {
        val store = SecureStore(context)
        val alarms = context.getSystemService(AlarmManager::class.java)
        store.scheduledAlarmIds.forEach { alarms.cancel(pending(context, it.toLong())) }
        val now = LocalDateTime.now(PhoneReminder.ZONE)
        val upcoming = stored(context)
            .mapNotNull { r -> PhoneReminder.nextFire(r.due, r.repeat, now)?.let { r to it } }
            .sortedBy { it.second }
            .take(MAX_ALARMS)
        for ((reminder, at) in upcoming) {
            val millis = at.atZone(PhoneReminder.ZONE).toInstant().toEpochMilli()
            val intent = pending(context, reminder.id)
            val exact = Build.VERSION.SDK_INT < 31 || alarms.canScheduleExactAlarms()
            if (exact) {
                alarms.setExactAndAllowWhileIdle(AlarmManager.RTC_WAKEUP, millis, intent)
            } else {
                alarms.setAndAllowWhileIdle(AlarmManager.RTC_WAKEUP, millis, intent)
            }
        }
        store.scheduledAlarmIds = upcoming.map { it.first.id.toString() }.toSet()
    }

    private fun pending(context: Context, id: Long): PendingIntent {
        val intent = Intent(context, ReminderReceiver::class.java).putExtra(EXTRA_ID, id)
        return PendingIntent.getBroadcast(
            context, id.toInt(), intent, PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        )
    }

    fun notify(context: Context, id: Long) {
        val reminder = stored(context).firstOrNull { it.id == id } ?: return
        if (Build.VERSION.SDK_INT >= 33 &&
            ContextCompat.checkSelfPermission(context, Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED
        ) return
        val open = PendingIntent.getActivity(
            context, 0, Intent(context, MainActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK),
            PendingIntent.FLAG_IMMUTABLE,
        )
        // On the lock screen only "You have a reminder" shows; the text after unlocking.
        val public = NotificationCompat.Builder(context, CHANNEL)
            .setSmallIcon(R.drawable.ic_notification)
            .setContentTitle("Jarvis")
            .setContentText("You have a reminder")
            .build()
        val notification = NotificationCompat.Builder(context, CHANNEL)
            .setSmallIcon(R.drawable.ic_notification)
            .setContentTitle("Reminder")
            .setContentText(reminder.text)
            .setStyle(NotificationCompat.BigTextStyle().bigText(reminder.text))
            .setCategory(NotificationCompat.CATEGORY_REMINDER)
            .setPriority(NotificationCompat.PRIORITY_HIGH)
            .setVisibility(NotificationCompat.VISIBILITY_PRIVATE)
            .setPublicVersion(public)
            .setContentIntent(open)
            .setAutoCancel(true)
            .build()
        NotificationManagerCompat.from(context).notify(("r" + id).hashCode(), notification)
    }

    fun idFrom(intent: Intent): Long = intent.getLongExtra(EXTRA_ID, -1)
}

class ReminderReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        val id = ReminderAlarms.idFrom(intent)
        if (id >= 0) ReminderAlarms.notify(context, id)
        ReminderAlarms.reschedule(context) // the next repeat, and the next alarms in line
    }
}

/** After a reboot, an app update or a clock change, alarms must be set again. */
class RescheduleReceiver : BroadcastReceiver() {
    private val actions = setOf(
        Intent.ACTION_BOOT_COMPLETED, Intent.ACTION_MY_PACKAGE_REPLACED, Intent.ACTION_TIME_CHANGED,
        Intent.ACTION_TIMEZONE_CHANGED, AlarmManager.ACTION_SCHEDULE_EXACT_ALARM_PERMISSION_STATE_CHANGED,
    )

    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action in actions) ReminderAlarms.reschedule(context)
    }
}
