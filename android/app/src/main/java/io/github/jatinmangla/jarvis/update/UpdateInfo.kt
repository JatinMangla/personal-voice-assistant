package io.github.jatinmangla.jarvis.update

import org.json.JSONException
import org.json.JSONObject

/** latest.json in the public releases repo (written by .github/workflows/android-release.yml). */
data class UpdateInfo(
    val versionCode: Int,
    val versionName: String,
    val apk: String,
    val sha256: String,
    val size: Long,
    val signerSha256: String,
) {
    companion object {
        const val REPO_RAW = "https://raw.githubusercontent.com/JatinMangla/jarvis-app/main/"
        const val LATEST_URL = REPO_RAW + "latest.json"
        private val HEX64 = Regex("[0-9a-f]{64}")
        private const val MAX_SIZE = 100L * 1024 * 1024

        /** Refuses anything that doesn't point into the releases repo or looks wrong. */
        fun parse(text: String): UpdateInfo? {
            val o = try {
                JSONObject(text)
            } catch (e: JSONException) {
                return null
            }
            val info = UpdateInfo(
                versionCode = o.optInt("versionCode", -1),
                versionName = o.optString("versionName"),
                apk = o.optString("apk"),
                sha256 = o.optString("sha256").lowercase(),
                size = o.optLong("size", -1),
                signerSha256 = o.optString("signerSha256").lowercase(),
            )
            val ok = info.versionCode > 0 && info.versionName.isNotBlank() &&
                info.apk.startsWith(REPO_RAW + "apk/") && info.apk.endsWith(".apk") && !info.apk.contains("..") &&
                HEX64.matches(info.sha256) && HEX64.matches(info.signerSha256) &&
                info.size in 1..MAX_SIZE
            return if (ok) info else null
        }
    }
}
