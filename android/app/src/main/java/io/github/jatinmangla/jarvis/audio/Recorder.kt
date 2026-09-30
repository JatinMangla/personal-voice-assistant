package io.github.jatinmangla.jarvis.audio

import android.content.Context
import android.media.MediaRecorder
import android.os.Build
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.withContext
import java.io.File
import kotlin.coroutines.coroutineContext

/** A finished recording: OGG/Opus bytes, and whether any speech was heard. */
class Recording(val audio: ByteArray, val heard: Boolean) {
    companion object {
        const val MIME = "audio/ogg"
    }
}

/**
 * Records one question as OGG/Opus, 16 kHz mono, 24 kbps (about 10x smaller than WAV:
 * ngrok's free plan has 1 GB a month; the server decodes Ogg directly).
 *
 * MediaRecorder with the MIC / VOICE_RECOGNITION source leaves the phone in normal media
 * mode (no "phone call" mode), so replies play on the loudspeaker or Bluetooth media, never
 * the earpiece: the page's biggest problem (CLAUDE.md gotchas 13, 21) doesn't exist here.
 * The file lives in the app's private cache only while recording and is deleted at once.
 */
class Recorder(private val context: Context) {
    @Volatile
    private var stopRequested = false

    /** Stop now (the mic button was tapped again). */
    fun stop() {
        stopRequested = true
    }

    /** Records until a pause (see SilenceDetector) or stop(); [onLevel] gets 0..1 levels.
     *  Returns null if the recorder could not start or produced nothing. */
    suspend fun record(quiet: Boolean, onLevel: (Float) -> Unit = {}): Recording? = withContext(Dispatchers.IO) {
        stopRequested = false
        val file = File(context.cacheDir, "question.ogg")
        file.delete()
        val recorder = if (Build.VERSION.SDK_INT >= 31) MediaRecorder(context) else @Suppress("DEPRECATION") MediaRecorder()
        val detector = SilenceDetector(quiet)
        var started = false
        try {
            // Whisper mode: VOICE_RECOGNITION has no noise suppression (it can remove soft
            // speech); normal mode: MIC with the phone's usual processing.
            recorder.setAudioSource(if (quiet) MediaRecorder.AudioSource.VOICE_RECOGNITION else MediaRecorder.AudioSource.MIC)
            recorder.setOutputFormat(MediaRecorder.OutputFormat.OGG)
            recorder.setAudioEncoder(MediaRecorder.AudioEncoder.OPUS)
            recorder.setAudioSamplingRate(16000)
            recorder.setAudioChannels(1)
            recorder.setAudioEncodingBitRate(24000)
            recorder.setOutputFile(file.absolutePath)
            recorder.prepare()
            recorder.start()
            started = true
            recorder.maxAmplitude // the first reading is always 0
            var last = System.nanoTime()
            while (coroutineContext.isActive && !stopRequested) {
                delay(50)
                val now = System.nanoTime()
                val level = recorder.maxAmplitude / 32767.0
                onLevel(level.toFloat().coerceIn(0f, 1f))
                if (detector.feed(level, (now - last) / 1_000_000)) break
                last = now
            }
        } catch (e: Exception) {
            // mic busy (a call), no permission, recorder error: nothing usable
        } finally {
            try {
                if (started) recorder.stop()
            } catch (e: RuntimeException) {
                started = false // stop() right after start(): no valid data
            }
            recorder.release()
            onLevel(0f)
        }
        val bytes = if (started && file.exists()) file.readBytes() else null
        file.delete()
        bytes?.let { Recording(it, detector.heard || stopRequested) }
    }
}
