package io.github.jatinmangla.jarvis.audio

/**
 * Decides when to stop listening, like the page: learn the background noise for 300 ms,
 * count speech as a bit above it, stop after a 1.2 s pause (Whisper mode: a lower bar and
 * 1.6 s), after 8 s with nothing heard, or after 30 s in any case.
 *
 * Levels are MediaRecorder.getMaxAmplitude() / 32767 (a peak, polled every ~50 ms), so the
 * minimum bars are higher than the page's RMS values.
 */
class SilenceDetector(private val quiet: Boolean) {
    var heard = false
        private set
    private var noise = 0.01
    private var speechMs = 0L
    private var silenceMs = 0L
    private var elapsedMs = 0L

    /** Feed one level reading taken [dtMs] after the previous one; true = stop now. */
    fun feed(level: Double, dtMs: Long): Boolean {
        elapsedMs += dtMs
        if (elapsedMs < 300) {
            noise = maxOf(noise, level)
            return false
        }
        val loud = if (quiet) level > maxOf(0.012, noise * 1.8) else level > maxOf(0.025, noise * 2.2)
        if (loud) {
            speechMs += dtMs
            silenceMs = 0
        } else {
            silenceMs += dtMs
        }
        if (speechMs > 200) heard = true
        val pauseMs = if (quiet) 1600 else 1200
        return (heard && silenceMs > pauseMs) || (!heard && elapsedMs > 8000) || elapsedMs > 30000
    }
}
