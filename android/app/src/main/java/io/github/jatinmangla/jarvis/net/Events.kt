package io.github.jatinmangla.jarvis.net

import org.json.JSONArray
import org.json.JSONException
import org.json.JSONObject
import java.util.Base64

/** A link found on the internet, shown as a card (never read aloud). */
data class LinkCard(
    val url: String,
    val title: String,
    val site: String,
    val note: String,
    val snippet: String,
    val image: String?,
    val price: String?,
    val kind: String,
    val videoId: String?,
    /** The user asked to PLAY it: the app opens this video in YouTube after the reply. */
    val play: Boolean = false,
) {
    val isVideo: Boolean get() = kind == "video" && videoId != null
}

/** One line of the /api/turn NDJSON stream. */
sealed interface TurnEvent {
    data class Transcript(val text: String, val lang: String) : TurnEvent
    class Sentence(val text: String, val audio: ByteArray?) : TurnEvent
    data class Links(val links: List<LinkCard>) : TurnEvent
    data class Done(
        val heard: Boolean,
        val turnId: Long?,
        val error: Boolean,
        val remindersFingerprint: String?,
    ) : TurnEvent
}

object Ndjson {
    private val VIDEO_ID = Regex("[\\w-]{11}")

    fun parse(line: String): TurnEvent? {
        val o = try {
            JSONObject(line)
        } catch (e: JSONException) {
            return null
        }
        return when (o.optString("type")) {
            "transcript" -> TurnEvent.Transcript(o.optString("text"), o.optString("lang", "en"))
            "sentence" -> TurnEvent.Sentence(o.optString("text"), decodeAudio(o.optNullableString("audio")))
            "links" -> TurnEvent.Links(links(o.optJSONArray("links")))
            "done" -> TurnEvent.Done(
                heard = o.optBoolean("heard", false),
                turnId = if (o.has("turn_id") && !o.isNull("turn_id")) o.optLong("turn_id") else null,
                error = o.optBoolean("error", false),
                remindersFingerprint = o.optNullableString("reminders"),
            )
            else -> null
        }
    }

    private fun decodeAudio(b64: String?): ByteArray? = try {
        b64?.let { Base64.getDecoder().decode(it) }
    } catch (e: IllegalArgumentException) {
        null
    }

    fun links(array: JSONArray?): List<LinkCard> {
        if (array == null) return emptyList()
        val out = ArrayList<LinkCard>()
        for (i in 0 until array.length()) {
            val l = array.optJSONObject(i) ?: continue
            val url = l.optString("url")
            if (!url.startsWith("https://", ignoreCase = true) && !url.startsWith("http://", ignoreCase = true)) continue
            out += LinkCard(
                url = url,
                title = l.optString("title").ifEmpty { url },
                site = l.optString("site"),
                note = l.optString("note"),
                snippet = l.optString("snippet"),
                image = l.optNullableString("image")?.takeIf { it.startsWith("https://", ignoreCase = true) },
                price = l.optNullableString("price"),
                kind = l.optString("kind", "web"),
                videoId = l.optNullableString("video_id")?.takeIf { VIDEO_ID.matches(it) },
                play = l.optBoolean("play", false),
            )
        }
        return out
    }
}

/** org.json's optString gives "null" for a JSON null; this gives null (also for blank). */
fun JSONObject.optNullableString(name: String): String? =
    if (!has(name) || isNull(name)) null else optString(name).takeIf { it.isNotBlank() }
