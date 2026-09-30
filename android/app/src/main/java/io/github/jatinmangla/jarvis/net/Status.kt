package io.github.jatinmangla.jarvis.net

import org.json.JSONObject
import java.util.Locale

data class Meter(val name: String, val used: Double, val limit: Double, val percent: Int, val resets: String?) {
    val label: String
        get() = when (name) {
            "day" -> "GPU today"
            "week" -> "GPU last 7 days"
            "requests" -> "Link requests (month)"
            "data" -> "Link data (month)"
            else -> name
        }

    val amount: String
        get() = when (name) {
            "day" -> "${Math.round(used / 60)} / ${Math.round(limit / 60)} min"
            "week" -> String.format(Locale.US, "%.1f / %d h", used / 3600, Math.round(limit / 3600))
            "data" -> "${Math.round(used / 1e6)} / ${Math.round(limit / 1e6)} MB"
            else -> String.format(Locale.US, "%,d / %,d", used.toLong(), limit.toLong())
        }
}

/** GET /api/status. */
data class Status(
    val state: String,
    val name: String,
    val api: Int,
    val lastSaved: String?,
    val saveOk: Boolean,
    val pollSec: Int,
    val meters: List<Meter>,
    val warnings: List<String>,
    val notifyPct: Int,
    val stopPct: Int,
    val startUrl: String?,
    val remindersFingerprint: String?,
) {
    companion object {
        fun parse(o: JSONObject): Status {
            val meters = o.optJSONArray("meters")
            val warnings = o.optJSONArray("warnings")
            return Status(
                state = o.optString("state", "ready"),
                name = o.optString("name", "Jarvis"),
                api = o.optInt("api", 0),
                lastSaved = o.optNullableString("last_saved"),
                saveOk = o.optBoolean("save_ok", true),
                pollSec = o.optInt("poll_sec", 60).coerceIn(15, 600),
                meters = (0 until (meters?.length() ?: 0)).mapNotNull { i ->
                    meters!!.optJSONObject(i)?.let {
                        Meter(it.optString("name"), it.optDouble("used", 0.0), it.optDouble("limit", 1.0),
                            it.optInt("percent", 0), it.optNullableString("resets"))
                    }
                },
                warnings = (0 until (warnings?.length() ?: 0)).map { warnings!!.optString(it) },
                notifyPct = o.optInt("notify_pct", 90),
                stopPct = o.optInt("stop_pct", 99),
                startUrl = o.optNullableString("start_url")?.takeIf { it.startsWith("https://") },
                remindersFingerprint = o.optNullableString("reminders"),
            )
        }
    }
}
