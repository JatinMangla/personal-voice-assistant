package io.github.jatinmangla.jarvis

import io.github.jatinmangla.jarvis.net.Api
import io.github.jatinmangla.jarvis.net.ApiException
import io.github.jatinmangla.jarvis.net.Pairing
import io.github.jatinmangla.jarvis.net.TurnEvent
import io.github.jatinmangla.jarvis.reminders.PhoneReminder
import kotlinx.coroutines.flow.toList
import kotlinx.coroutines.runBlocking
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Assume.assumeTrue
import org.junit.Test

/**
 * The app's real HTTP client against the real server code (src/app.py) with fake speech
 * parts. Runs only when JARVIS_IT_URL and JARVIS_IT_TOKEN are set (a local test server,
 * plain http is fine here: the network security config only applies on the phone).
 */
class ServerContractTest {
    private val url: String? = System.getenv("JARVIS_IT_URL")
    private val token: String? = System.getenv("JARVIS_IT_TOKEN")

    private fun api(t: String = token!!) = Api(Pairing(url!!, t))

    @Test
    fun fullSession() = runBlocking {
        assumeTrue(url != null && token != null)
        assertEquals(Api.APP_API, api().hello().getInt("api"))

        val status = api().status()
        assertEquals(Api.APP_API, status.api)
        assertEquals(4, status.meters.size)
        assertTrue(status.startUrl!!.startsWith("https://"))

        val ogg = "OggS".toByteArray() + ByteArray(4000)
        val events = api().turn(ogg, "audio/ogg", quiet = true).toList()
        assertEquals("hello jarvis quiet", (events[0] as TurnEvent.Transcript).text)
        val sentences = events.filterIsInstance<TurnEvent.Sentence>()
        assertEquals(listOf("Hello there.", "Here you go."), sentences.map { it.text })
        assertTrue(String(sentences[0].audio!!).startsWith("ID3"))
        assertEquals("dQw4w9WgXcQ", events.filterIsInstance<TurnEvent.Links>().single().links[0].videoId)
        val done = events.last() as TurnEvent.Done
        assertTrue(done.heard)
        assertEquals(status.remindersFingerprint, done.remindersFingerprint)

        api().post("/api/rate", JSONObject().put("turn_id", done.turnId).put("value", 1))

        val body = api().get("/api/app/reminders")
        val reminders = PhoneReminder.listFromJson(body.getJSONArray("reminders"))
        assertEquals("call mom", reminders.single().text)
        assertEquals(status.remindersFingerprint, body.getString("fingerprint"))

        // Page-only endpoints refuse the app's token.
        val refused = runCatching { api().post("/api/app/pair") }.exceptionOrNull() as ApiException
        assertEquals(403, refused.code)
        val wrong = runCatching { api("B".repeat(43)).status() }.exceptionOrNull() as ApiException
        assertEquals(ApiException.Kind.Unpaired, wrong.kind)

        // After pairing the app swaps the QR's token; the old one stops working.
        val fresh = api().post("/api/app/rotate").getString("token")
        val old = runCatching { api().status() }.exceptionOrNull() as ApiException
        assertEquals(ApiException.Kind.Unpaired, old.kind)
        assertEquals(Api.APP_API, api(fresh).status().api)
    }
}
