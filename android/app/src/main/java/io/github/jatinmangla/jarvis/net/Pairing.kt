package io.github.jatinmangla.jarvis.net

import org.json.JSONException
import org.json.JSONObject
import java.net.URI
import java.net.URISyntaxException

/** Where Jarvis is and this phone's device token (from the pairing QR code on the page). */
data class Pairing(val url: String, val token: String) {
    val host: String get() = URI(url).host ?: url

    fun toJson(): String = JSONObject().put("url", url).put("token", token).toString()

    companion object {
        private val TOKEN = Regex("[A-Za-z0-9_-]{32,128}")

        /** The QR code: {"jarvis": 1, "url": "https://...", "token": "..."}. Anything else,
         *  plain-http addresses or addresses with paths/credentials are refused. */
        fun fromQr(text: String?): Pairing? {
            val o = try {
                JSONObject(text ?: return null)
            } catch (e: JSONException) {
                return null
            }
            if (o.optInt("jarvis") != 1) return null
            return of(o.optString("url"), o.optString("token"))
        }

        fun fromJson(text: String?): Pairing? = try {
            val o = JSONObject(text ?: return null)
            of(o.optString("url"), o.optString("token"))
        } catch (e: JSONException) {
            null
        }

        private fun of(rawUrl: String, token: String): Pairing? {
            val url = rawUrl.trim().trimEnd('/')
            val uri = try {
                URI(url)
            } catch (e: URISyntaxException) {
                return null
            }
            if (uri.scheme != "https" || uri.host.isNullOrEmpty() || uri.rawUserInfo != null ||
                !uri.rawPath.isNullOrEmpty() || uri.rawQuery != null || uri.rawFragment != null
            ) return null
            if (!TOKEN.matches(token)) return null
            return Pairing(url, token)
        }
    }
}
