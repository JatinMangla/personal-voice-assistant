package io.github.jatinmangla.jarvis

import io.github.jatinmangla.jarvis.audio.SilenceDetector
import io.github.jatinmangla.jarvis.net.Ndjson
import io.github.jatinmangla.jarvis.net.Pairing
import io.github.jatinmangla.jarvis.net.Status
import io.github.jatinmangla.jarvis.net.TurnEvent
import io.github.jatinmangla.jarvis.reminders.PhoneReminder
import io.github.jatinmangla.jarvis.update.UpdateInfo
import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.time.LocalDateTime
import java.util.Base64

class PairingTest {
    private val token = "A".repeat(43)

    private fun qr(url: String, t: String = token, v: Int = 1) =
        JSONObject().put("jarvis", v).put("url", url).put("token", t).toString()

    @Test
    fun readsTheLaptopPagesCode() {
        val p = Pairing.fromQr(qr("https://volley-gnarly-constable.ngrok-free.dev/"))!!
        assertEquals("https://volley-gnarly-constable.ngrok-free.dev", p.url)
        assertEquals(token, p.token)
        assertEquals("volley-gnarly-constable.ngrok-free.dev", p.host)
        assertEquals(p, Pairing.fromJson(p.toJson()))
    }

    @Test
    fun refusesAnythingElse() {
        assertNull(Pairing.fromQr("hello"))
        assertNull(Pairing.fromQr(null))
        assertNull(Pairing.fromQr(qr("http://example.com"))) // no plain http
        assertNull(Pairing.fromQr(qr("https://user:pw@example.com")))
        assertNull(Pairing.fromQr(qr("https://example.com/path")))
        assertNull(Pairing.fromQr(qr("https://example.com?x=1")))
        assertNull(Pairing.fromQr(qr("https://example.com", t = "short")))
        assertNull(Pairing.fromQr(qr("https://example.com", t = "A".repeat(42) + "!")))
        assertNull(Pairing.fromQr(qr("https://example.com", v = 2)))
    }
}

class NdjsonTest {
    @Test
    fun parsesEveryEventType() {
        val t = Ndjson.parse("""{"type":"transcript","text":"hello","lang":"hi"}""") as TurnEvent.Transcript
        assertEquals("hello", t.text)
        assertEquals("hi", t.lang)

        val mp3 = byteArrayOf(0x49, 0x44, 0x33, 1, 2)
        val b64 = Base64.getEncoder().encodeToString(mp3)
        val s = Ndjson.parse("""{"type":"sentence","text":"Hi.","audio":"$b64"}""") as TurnEvent.Sentence
        assertArrayEquals(mp3, s.audio)
        assertNull((Ndjson.parse("""{"type":"sentence","text":"x","audio":null}""") as TurnEvent.Sentence).audio)

        val d = Ndjson.parse("""{"type":"done","heard":true,"turn_id":42,"reminders":"abc"}""") as TurnEvent.Done
        assertTrue(d.heard)
        assertEquals(42L, d.turnId)
        assertEquals("abc", d.remindersFingerprint)
        val quiet = Ndjson.parse("""{"type":"done","heard":false}""") as TurnEvent.Done
        assertFalse(quiet.heard)
        assertNull(quiet.turnId)
        assertNull(Ndjson.parse("""{"type":"done","heard":true,"turn_id":null}""").let { (it as TurnEvent.Done).turnId })

        assertNull(Ndjson.parse("not json"))
        assertNull(Ndjson.parse("""{"type":"unknown"}"""))
    }

    @Test
    fun linkCardsKeepOnlyWebLinksAndValidVideoIds() {
        val links = JSONArray()
            .put(JSONObject().put("url", "https://youtube.com/watch?v=dQw4w9WgXcQ").put("title", "Song")
                .put("kind", "video").put("video_id", "dQw4w9WgXcQ").put("image", "https://i.ytimg.com/x.jpg")
                .put("play", true))
            .put(JSONObject().put("url", "javascript:alert(1)").put("title", "bad"))
            .put(JSONObject().put("url", "https://shop.in/p").put("title", "Kettle").put("kind", "product")
                .put("price", "₹999").put("image", "http://insecure/img.png").put("video_id", "../../etc"))
        val cards = (Ndjson.parse(JSONObject().put("type", "links").put("links", links).toString()) as TurnEvent.Links).links
        assertEquals(2, cards.size)
        assertTrue(cards[0].isVideo)
        assertTrue(cards[0].play)
        assertFalse(cards[1].play)
        assertEquals("₹999", cards[1].price)
        assertNull(cards[1].image) // only https images
        assertNull(cards[1].videoId)
    }
}

class StatusTest {
    @Test
    fun parsesTheServersStatus() {
        val o = JSONObject("""{"state":"ready","name":"Jarvis","api":1,"last_saved":"10:05","save_ok":true,
            "poll_sec":60,"meters":[{"name":"day","used":3000,"limit":15420,"percent":19,"resets":"x"},
            {"name":"data","used":250000000,"limit":1000000000,"percent":25}],"warnings":[],
            "notify_pct":90,"stop_pct":99,"start_url":"https://github.com/x","reminders":"f00"}""")
        val s = Status.parse(o)
        assertEquals(1, s.api)
        assertEquals("50 / 257 min", s.meters[0].amount)
        assertEquals("250 / 1000 MB", s.meters[1].amount)
        assertEquals("f00", s.remindersFingerprint)
        assertEquals("https://github.com/x", s.startUrl)
        assertNull(Status.parse(JSONObject("""{"start_url":"javascript:x","last_saved":null}""")).startUrl)
    }
}

class SilenceDetectorTest {
    private fun run(d: SilenceDetector, level: Double, ms: Int): Boolean {
        repeat(ms / 50) { if (d.feed(level, 50)) return true }
        return false
    }

    @Test
    fun stopsAfterAPauseFollowingSpeech() {
        val d = SilenceDetector(quiet = false)
        assertFalse(run(d, 0.005, 300)) // background noise
        assertFalse(run(d, 0.3, 1000))  // speech
        assertTrue(d.heard)
        assertFalse(run(d, 0.005, 1000)) // a short pause is fine
        assertTrue(run(d, 0.005, 400))   // > 1.2 s: stop
    }

    @Test
    fun givesUpWhenNothingIsSaid() {
        val d = SilenceDetector(quiet = false)
        assertFalse(run(d, 0.005, 7900))
        assertTrue(run(d, 0.005, 200))
        assertFalse(d.heard)
    }

    @Test
    fun whisperModeHearsSofterSpeechAndWaitsLonger() {
        val loud = SilenceDetector(quiet = false)
        val soft = SilenceDetector(quiet = true)
        for (d in listOf(loud, soft)) {
            run(d, 0.004, 300)
            run(d, 0.02, 600)
        }
        assertFalse(loud.heard)
        assertTrue(soft.heard)
        assertFalse(run(soft, 0.004, 1500))
        assertTrue(run(soft, 0.004, 200))
    }
}

class ReminderTest {
    private val now = LocalDateTime.of(2026, 10, 1, 10, 0)

    @Test
    fun nextFire() {
        val later = LocalDateTime.of(2026, 10, 1, 18, 0)
        assertEquals(later, PhoneReminder.nextFire(later, null, now))
        assertNull(PhoneReminder.nextFire(LocalDateTime.of(2026, 9, 30, 18, 0), null, now))
        assertEquals(LocalDateTime.of(2026, 10, 1, 18, 0),
            PhoneReminder.nextFire(LocalDateTime.of(2020, 1, 1, 18, 0), "daily", now))
        assertEquals(LocalDateTime.of(2026, 10, 2, 8, 0),
            PhoneReminder.nextFire(LocalDateTime.of(2026, 9, 20, 8, 0), "daily", now))
        assertEquals(LocalDateTime.of(2026, 10, 5, 9, 0), // Mondays
            PhoneReminder.nextFire(LocalDateTime.of(2026, 9, 7, 9, 0), "weekly", now))
        assertEquals(LocalDateTime.of(2026, 10, 31, 9, 0),
            PhoneReminder.nextFire(LocalDateTime.of(2026, 1, 31, 9, 0), "monthly", now))
        assertEquals(LocalDateTime.of(2027, 2, 28, 9, 0),
            PhoneReminder.nextFire(LocalDateTime.of(2024, 2, 29, 9, 0), "yearly", now))
        assertNull(PhoneReminder.nextFire(LocalDateTime.of(2026, 9, 1, 9, 0), "hourly", now))
    }

    @Test
    fun jsonRoundTripAndBadRowsSkipped() {
        val arr = JSONArray("""[{"id":1,"text":"call mom","due":"2026-10-01T18:00","repeat":null},
            {"id":2,"text":"water plants","due":"2026-09-20T08:00","repeat":"daily"},
            {"id":3,"text":"","due":"2026-10-01T18:00"},{"id":4,"text":"x","due":"soon"},
            {"id":5,"text":"y","due":"2026-10-01T18:00","repeat":"hourly"}]""")
        val list = PhoneReminder.listFromJson(arr)
        assertEquals(listOf(1L, 2L, 5L), list.map { it.id })
        assertNull(list[2].repeat)
        assertEquals(list, PhoneReminder.listFromJson(JSONArray(PhoneReminder.listToJson(list))))
    }
}

class UpdateInfoTest {
    private val sha = "a".repeat(64)
    private fun json(apk: String = UpdateInfo.REPO_RAW + "apk/jarvis-0.2.0.apk", size: Long = 1_500_000) =
        JSONObject().put("versionCode", 7).put("versionName", "0.2.0").put("apk", apk).put("sha256", sha)
            .put("size", size).put("signerSha256", "b".repeat(64)).toString()

    @Test
    fun acceptsTheReleaseRepoOnly() {
        val info = UpdateInfo.parse(json())
        assertNotNull(info)
        assertEquals(7, info!!.versionCode)
        assertNull(UpdateInfo.parse(json(apk = "https://evil.example/jarvis.apk")))
        assertNull(UpdateInfo.parse(json(apk = UpdateInfo.REPO_RAW + "apk/../x.apk")))
        assertNull(UpdateInfo.parse(json(size = 0)))
        assertNull(UpdateInfo.parse("{}"))
        assertNull(UpdateInfo.parse("<html>"))
    }
}
