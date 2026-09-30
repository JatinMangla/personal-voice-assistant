package io.github.jatinmangla.jarvis.audio

import android.content.Context
import android.net.Uri
import androidx.annotation.OptIn
import androidx.media3.common.AudioAttributes
import androidx.media3.common.C
import androidx.media3.common.MediaItem
import androidx.media3.common.Player
import androidx.media3.common.util.UnstableApi
import androidx.media3.datasource.ByteArrayDataSource
import androidx.media3.datasource.DataSource
import androidx.media3.exoplayer.ExoPlayer
import androidx.media3.exoplayer.source.ProgressiveMediaSource
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.first

/**
 * Plays the reply sentence by sentence (MP3 per sentence, or WAV for the goodbye), straight
 * from memory (never written to disk), as MEDIA audio: loudspeaker, or wired/Bluetooth
 * headphones when connected. Takes audio focus, so music pauses while Jarvis speaks.
 * Call everything on the main thread.
 */
@OptIn(UnstableApi::class)
class SpeechPlayer(context: Context) {
    private val player: ExoPlayer = ExoPlayer.Builder(context)
        .setAudioAttributes(
            AudioAttributes.Builder()
                .setUsage(C.USAGE_MEDIA)
                .setContentType(C.AUDIO_CONTENT_TYPE_SPEECH)
                .build(),
            true, // handle audio focus
        )
        .setHandleAudioBecomingNoisy(true) // headphones unplugged: stop, don't blast the speaker
        .build()
    private var counter = 0
    private val _speaking = MutableStateFlow(false)

    /** True while there is something queued or playing. */
    val speaking: StateFlow<Boolean> = _speaking.asStateFlow()

    init {
        player.addListener(object : Player.Listener {
            override fun onPlaybackStateChanged(playbackState: Int) = update()
            override fun onPlayWhenReadyChanged(playWhenReady: Boolean, reason: Int) = update()
        })
    }

    private fun update() {
        val state = player.playbackState
        _speaking.value = player.playWhenReady && (state == Player.STATE_BUFFERING || state == Player.STATE_READY)
    }

    fun enqueue(audio: ByteArray) {
        val source = ProgressiveMediaSource.Factory(DataSource.Factory { ByteArrayDataSource(audio) })
            .createMediaSource(MediaItem.fromUri(Uri.parse("jarvis://sentence/${counter++}")))
        val ended = player.playbackState == Player.STATE_ENDED || player.playbackState == Player.STATE_IDLE
        if (ended) {
            player.clearMediaItems()
            player.addMediaSource(source)
            player.seekTo(0, 0)
            player.prepare()
        } else {
            player.addMediaSource(source)
        }
        player.playWhenReady = true
        update()
    }

    /** Stop at once and drop the rest of the reply (tap to interrupt). */
    fun stop() {
        player.stop()
        player.clearMediaItems()
        update()
    }

    /** Suspends until everything queued has been played (or stopped). */
    suspend fun awaitDone() {
        speaking.first { !it }
    }

    fun release() = player.release()
}
