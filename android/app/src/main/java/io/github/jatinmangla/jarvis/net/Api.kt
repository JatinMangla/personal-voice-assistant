package io.github.jatinmangla.jarvis.net

import io.github.jatinmangla.jarvis.BuildConfig
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.currentCoroutineContext
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.flow
import kotlinx.coroutines.flow.flowOn
import kotlinx.coroutines.withContext
import okhttp3.Call
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import okhttp3.Response
import org.json.JSONException
import org.json.JSONObject
import java.io.IOException
import java.util.concurrent.TimeUnit

/** What went wrong with a request, in the words the screen needs. */
class ApiException(val kind: Kind, val code: Int = 0) : IOException(kind.name) {
    enum class Kind {
        Offline,   // no network, or ngrok says the tunnel is down (Jarvis is off)
        Unpaired,  // 401: token unknown/revoked
        Locked,    // 429: too many wrong tokens
        Busy,      // 409: a reply is in progress, or Jarvis is closing
        Failed,    // anything else
    }
}

/**
 * Talks to Jarvis through ngrok. Every request carries:
 * - X-Jarvis-App: lets it past ngrok's Google sign-in (browser only) ...
 * - Authorization: ... because the server then requires this phone's device token;
 * - ngrok-skip-browser-warning: skips ngrok's free-plan warning page.
 * Nothing here is ever logged.
 */
class Api(private val pairing: Pairing) {
    companion object {
        const val APP_API = 1 // the server contract this app understands (status "api")

        val client: OkHttpClient = OkHttpClient.Builder()
            .connectTimeout(15, TimeUnit.SECONDS)
            .readTimeout(120, TimeUnit.SECONDS) // a turn's stream can pause while the LLM thinks
            .writeTimeout(60, TimeUnit.SECONDS)
            .retryOnConnectionFailure(true)
            .build()

        private val JSON = "application/json".toMediaType()
    }

    private fun request(path: String): Request.Builder = Request.Builder()
        .url(pairing.url + path)
        .header("X-Jarvis-App", "1")
        .header("Authorization", "Bearer ${pairing.token}")
        .header("ngrok-skip-browser-warning", "1")
        .header("User-Agent", "JarvisApp/${BuildConfig.VERSION_NAME}")

    private fun check(response: Response) {
        if (response.isSuccessful) return
        // ngrok answers for a stopped tunnel itself (ERR_NGROK_3200 etc.).
        if (response.header("ngrok-error-code") != null) throw ApiException(ApiException.Kind.Offline, response.code)
        throw ApiException(
            when (response.code) {
                401 -> ApiException.Kind.Unpaired
                429 -> ApiException.Kind.Locked
                409 -> ApiException.Kind.Busy
                502, 503, 504 -> ApiException.Kind.Offline
                else -> ApiException.Kind.Failed
            }, response.code
        )
    }

    private fun execute(call: Call): Response = try {
        call.execute()
    } catch (e: IOException) {
        throw ApiException(ApiException.Kind.Offline)
    }

    private suspend fun json(req: Request): JSONObject = withContext(Dispatchers.IO) {
        execute(Api.client.newCall(req)).use { response ->
            check(response)
            try {
                JSONObject(response.body?.string().orEmpty())
            } catch (e: JSONException) {
                throw ApiException(ApiException.Kind.Failed, response.code)
            }
        }
    }

    suspend fun get(path: String): JSONObject = json(request(path).get().build())

    suspend fun post(path: String, body: JSONObject = JSONObject()): JSONObject =
        json(request(path).post(body.toString().toRequestBody(JSON)).build())

    suspend fun postBytes(path: String, bytes: ByteArray, mime: String): JSONObject =
        json(request(path).post(bytes.toRequestBody(mime.toMediaType())).build())

    suspend fun status(): Status = Status.parse(get("/api/status"))

    suspend fun hello(): JSONObject = get("/api/app/hello")

    /** Goodbye audio (said when Jarvis closes), or null. */
    suspend fun goodbye(): ByteArray? = withContext(Dispatchers.IO) {
        execute(Api.client.newCall(request("/api/goodbye").get().build())).use { r ->
            if (r.isSuccessful) r.body?.bytes() else null
        }
    }

    /** One spoken turn: the recording goes up, events stream back line by line. Cancelling
     *  the collector cancels the HTTP call (the server still finishes and saves the turn). */
    fun turn(audio: ByteArray, mime: String, quiet: Boolean): Flow<TurnEvent> = flow {
        val req = request("/api/turn")
            .header("X-Quiet", if (quiet) "1" else "0")
            .post(audio.toRequestBody(mime.toMediaType()))
            .build()
        val call = Api.client.newCall(req)
        currentCoroutineContext()[Job]?.invokeOnCompletion { call.cancel() }
        execute(call).use { response ->
            check(response)
            val source = response.body?.source() ?: throw ApiException(ApiException.Kind.Failed)
            while (true) {
                val line = try {
                    source.readUtf8Line()
                } catch (e: IOException) {
                    throw ApiException(ApiException.Kind.Offline)
                } ?: break
                if (line.isBlank()) continue
                Ndjson.parse(line)?.let { emit(it) }
            }
        }
    }.flowOn(Dispatchers.IO)
}
