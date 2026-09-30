package io.github.jatinmangla.jarvis.reminders

import org.json.JSONArray
import org.json.JSONObject
import java.time.LocalDateTime
import java.time.ZoneId
import java.time.format.DateTimeParseException

/** A reminder the phone rings for. [due] is local time in [zone] (Jarvis uses India time). */
data class PhoneReminder(val id: Long, val text: String, val due: LocalDateTime, val repeat: String?) {
    companion object {
        val ZONE: ZoneId = ZoneId.of("Asia/Kolkata")
        private val REPEATS = setOf("daily", "weekly", "monthly", "yearly")

        /** The server's /api/app/reminders "reminders" array; bad rows are skipped. */
        fun listFromJson(array: JSONArray?): List<PhoneReminder> {
            if (array == null) return emptyList()
            return (0 until array.length()).mapNotNull { i -> fromJson(array.optJSONObject(i)) }
        }

        private fun fromJson(o: JSONObject?): PhoneReminder? {
            o ?: return null
            val due = try {
                LocalDateTime.parse(o.optString("due").take(16))
            } catch (e: DateTimeParseException) {
                return null
            }
            val text = o.optString("text").trim().take(200)
            if (text.isEmpty() || !o.has("id")) return null
            val repeat = if (o.isNull("repeat")) null else o.optString("repeat").takeIf { it in REPEATS }
            return PhoneReminder(o.optLong("id"), text, due, repeat)
        }

        fun listToJson(list: List<PhoneReminder>): String = JSONArray().apply {
            list.forEach {
                put(JSONObject().put("id", it.id).put("text", it.text).put("due", it.due.toString())
                    .put("repeat", it.repeat ?: JSONObject.NULL))
            }
        }.toString()

        /** When to ring next, after [now]: the due time itself if still ahead, else the next
         *  repeat (same rules as reminders.next_due on the server), else null. */
        fun nextFire(due: LocalDateTime, repeat: String?, now: LocalDateTime): LocalDateTime? {
            if (due.isAfter(now)) return due
            var next = due
            // Jump close to now first so a years-old daily reminder doesn't loop for long.
            when (repeat) {
                "daily" -> next = next.plusDays(maxOf(0, java.time.Duration.between(next, now).toDays()))
                "weekly" -> next = next.plusWeeks(maxOf(0, java.time.Duration.between(next, now).toDays() / 7))
                "monthly", "yearly" -> {}
                else -> return null
            }
            var n = 0L
            val first = next
            while (!next.isAfter(now)) {
                n++
                next = when (repeat) {
                    "daily" -> first.plusDays(n)
                    "weekly" -> first.plusWeeks(n)
                    "monthly" -> first.plusMonths(n) // plusMonths keeps day-of-month, clamped
                    else -> first.plusYears(n)
                }
            }
            return next
        }
    }
}
