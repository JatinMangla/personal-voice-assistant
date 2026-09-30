package io.github.jatinmangla.jarvis

import android.app.Application
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import io.github.jatinmangla.jarvis.audio.Recorder
import io.github.jatinmangla.jarvis.audio.Recording
import io.github.jatinmangla.jarvis.audio.SpeechPlayer
import io.github.jatinmangla.jarvis.net.Api
import io.github.jatinmangla.jarvis.net.ApiException
import io.github.jatinmangla.jarvis.net.LinkCard
import io.github.jatinmangla.jarvis.net.Pairing
import io.github.jatinmangla.jarvis.net.Status
import io.github.jatinmangla.jarvis.net.TurnEvent
import io.github.jatinmangla.jarvis.reminders.PhoneReminder
import io.github.jatinmangla.jarvis.reminders.ReminderAlarms
import io.github.jatinmangla.jarvis.update.UpdateInfo
import io.github.jatinmangla.jarvis.update.Updater
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import org.json.JSONObject
import java.time.LocalDateTime
import java.time.format.DateTimeFormatter
import java.util.Locale
import java.util.concurrent.atomic.AtomicLong

enum class Phase { Ready, Listening, Thinking, Speaking }

/** Online: connected. Offline: Jarvis is off. Closed: it said goodbye. Unpaired: token refused. */
enum class Link { Unknown, Online, Offline, Closed, Unpaired }

sealed interface ChatItem {
    val key: Long

    data class You(override val key: Long, val text: String) : ChatItem
    data class Bot(override val key: Long, val text: String) : ChatItem
    data class Links(override val key: Long, val links: List<LinkCard>) : ChatItem
    data class Note(override val key: Long, val text: String) : ChatItem
    data class Rating(override val key: Long, val turnId: Long, val value: Int = 0, val rated: Boolean = false) : ChatItem
}

data class UpdateUi(val info: UpdateInfo, val ready: Boolean = false, val error: String? = null)

data class UiState(
    val pairing: Pairing?,
    val link: Link = Link.Unknown,
    val phase: Phase = Phase.Ready,
    val statusText: String = "Tap to talk",
    val chat: List<ChatItem> = emptyList(),
    val status: Status? = null,
    val whisper: Boolean = false,
    val startUrl: String? = null,
    val update: UpdateUi? = null,
    val level: Float = 0f,
    val pairMessage: String? = null,
    val pairingBusy: Boolean = false,
)

/**
 * The whole voice loop, like the page (src/static/index.html): tap -> record until a pause ->
 * POST /api/turn -> show and play the streamed sentences -> ready. Plus status polling
 * (only while the app is visible and Jarvis is online: every poll is an ngrok request),
 * the goodbye, reminders sync and self-update.
 */
class JarvisViewModel(app: Application) : AndroidViewModel(app) {
    private val store = SecureStore(app)
    private val recorder = Recorder(app)
    private val player = SpeechPlayer(app)
    private val updater = Updater(app)
    private val keys = AtomicLong()

    private val _state = MutableStateFlow(
        UiState(pairing = store.pairing, whisper = store.whisper, startUrl = store.startUrl)
    )
    val state: StateFlow<UiState> = _state.asStateFlow()

    private var turnJob: Job? = null
    private var listenJob: Job? = null
    private var pollJob: Job? = null
    private var visible = false
    private var discardRecording = false
    private var goodbyePlayed = false
    private var dueNotesShown = false
    private var versionNoteShown = false

    private val api: Api? get() = _state.value.pairing?.let { Api(it) }

    init {
        viewModelScope.launch {
            player.speaking.collect { speaking ->
                val phase = _state.value.phase
                if (speaking && (phase == Phase.Thinking || phase == Phase.Speaking)) {
                    setPhase(Phase.Speaking, "Speaking… tap to interrupt")
                }
            }
        }
        viewModelScope.launch {
            JarvisApp.updateFailed.collect { failed ->
                if (failed) _state.update { s -> s.copy(update = s.update?.copy(error = "The update did not install. Try again later.")) }
            }
        }
    }

    private fun key() = keys.incrementAndGet()

    private fun setPhase(phase: Phase, text: String) {
        JarvisApp.busy = phase != Phase.Ready
        _state.update { it.copy(phase = phase, statusText = text) }
    }

    private fun addItem(item: ChatItem) = _state.update { it.copy(chat = it.chat + item) }

    fun addNote(text: String) = addItem(ChatItem.Note(key(), text))

    // --- lifecycle ------------------------------------------------------------------------
    fun onVisible() {
        visible = true
        if (_state.value.pairing == null) return
        if (_state.value.link == Link.Online) startPolling() else reconnect()
        checkUpdates()
    }

    fun onHidden() {
        visible = false
        pollJob?.cancel()
        if (_state.value.phase == Phase.Listening) { // Android stops the mic in the background anyway
            discardRecording = true
            recorder.stop()
        }
    }

    override fun onCleared() {
        player.release()
        JarvisApp.busy = false
    }

    // --- connection and status ------------------------------------------------------------
    fun reconnect() {
        goodbyePlayed = false
        viewModelScope.launch {
            setPhase(Phase.Thinking, "Connecting…")
            poll()
            if (_state.value.link == Link.Online) {
                setPhase(Phase.Ready, "Tap to talk")
                launch { runCatching { api?.post("/api/ping") } }
                showDueNotes()
                startPolling()
            } else if (_state.value.phase == Phase.Thinking) {
                setPhase(Phase.Ready, _state.value.statusText)
            }
        }
    }

    private fun startPolling() {
        pollJob?.cancel()
        pollJob = viewModelScope.launch {
            while (visible && _state.value.link == Link.Online) {
                delay((_state.value.status?.pollSec ?: 60) * 1000L)
                if (visible) poll()
            }
        }
    }

    private suspend fun poll() {
        val api = api ?: return
        try {
            val s = api.status()
            s.startUrl?.let { store.startUrl = it }
            _state.update { it.copy(status = s, link = Link.Online, startUrl = store.startUrl) }
            if (s.api != Api.APP_API && !versionNoteShown) {
                versionNoteShown = true
                addNote(
                    if (s.api > Api.APP_API) "Jarvis on the server is newer than this app. The app updates itself soon; some things may not work until then."
                    else "Jarvis on the server is older than this app. Push the latest code, then start Jarvis again."
                )
            }
            if (s.remindersFingerprint != null && s.remindersFingerprint != store.remindersFingerprint) syncReminders()
            if (s.state == "closing" && !goodbyePlayed) goodbye()
        } catch (e: ApiException) {
            when (e.kind) {
                ApiException.Kind.Unpaired -> unpaired()
                ApiException.Kind.Locked -> _state.update { it.copy(statusText = "Too many wrong attempts; try again in 15 minutes.") }
                else -> if (_state.value.link != Link.Closed) {
                    _state.update {
                        it.copy(link = Link.Offline, statusText = "Jarvis is off. Start it, wait ~4 minutes, then tap the mic to connect.")
                    }
                }
            }
        } catch (e: CancellationException) {
            throw e
        } catch (e: Exception) {
            _state.update { it.copy(link = Link.Offline, statusText = "Jarvis is off. Start it, wait ~4 minutes, then tap the mic to connect.") }
        }
    }

    private fun unpaired() {
        _state.update {
            it.copy(link = Link.Unpaired, statusText = "This phone is no longer paired. Pair it again from the laptop page.")
        }
        setPhase(Phase.Ready, _state.value.statusText)
    }

    private suspend fun goodbye() {
        goodbyePlayed = true
        listenJob?.cancel()
        discardRecording = true
        recorder.stop()
        turnJob?.cancel()
        player.stop()
        _state.update { it.copy(link = Link.Closed) }
        setPhase(Phase.Ready, "Session closed, memory saved. Start it again, then tap the mic to reconnect.")
        pollJob?.cancel()
        val audio = runCatching { api?.goodbye() }.getOrNull()
        if (audio != null) player.enqueue(audio)
    }

    /** Due reminders as notes, once per connection (the phone's own alarms ring as well). */
    private fun showDueNotes() {
        if (dueNotesShown) return
        viewModelScope.launch {
            try {
                val due = api?.get("/api/reminders")?.optJSONArray("due") ?: return@launch
                dueNotesShown = true
                val format = DateTimeFormatter.ofPattern("EEE h:mm a", Locale.getDefault())
                for (i in 0 until due.length()) {
                    val d = due.optJSONObject(i) ?: continue
                    val whenText = runCatching { LocalDateTime.parse(d.optString("due").take(16)).format(format) }.getOrNull()
                    addNote("Reminder: " + d.optString("text") + (whenText?.let { " ($it)" } ?: ""))
                }
            } catch (e: CancellationException) {
                throw e
            } catch (e: Exception) {
                // shown next time
            }
        }
    }

    private suspend fun syncReminders() {
        try {
            val body = api?.get("/api/app/reminders") ?: return
            val list = PhoneReminder.listFromJson(body.optJSONArray("reminders"))
            withContext(Dispatchers.IO) {
                ReminderAlarms.sync(getApplication(), list, body.optString("fingerprint").ifEmpty { null })
            }
        } catch (e: CancellationException) {
            throw e
        } catch (e: Exception) {
            // next status check tries again
        }
    }

    // --- one spoken turn ------------------------------------------------------------------
    fun onMic() {
        val s = _state.value
        if (s.pairing == null || s.link == Link.Unpaired) return
        when {
            s.link != Link.Online -> reconnect()
            s.phase == Phase.Listening -> recorder.stop()
            else -> startListening()
        }
    }

    private fun startListening() {
        turnJob?.cancel()
        player.stop()
        discardRecording = false
        setPhase(Phase.Listening, "Listening… (tap to stop)")
        listenJob = viewModelScope.launch {
            val recording = recorder.record(_state.value.whisper) { level -> _state.update { it.copy(level = level) } }
            if (discardRecording) {
                setPhase(Phase.Ready, if (_state.value.link == Link.Closed) _state.value.statusText else "Tap to talk")
                return@launch
            }
            when {
                recording == null -> setPhase(Phase.Ready, "The microphone didn't start. Tap to try again.")
                !recording.heard -> setPhase(Phase.Ready, "Didn't hear anything. Tap to try again.")
                recording.audio.size < 1500 -> setPhase(Phase.Ready, "The recording failed. Tap to try again.")
                else -> sendTurn(recording)
            }
        }
    }

    private fun sendTurn(recording: Recording) {
        val api = api ?: return
        turnJob = viewModelScope.launch {
            setPhase(Phase.Thinking, "Thinking…")
            var botKey: Long? = null
            try {
                api.turn(recording.audio, Recording.MIME, _state.value.whisper).collect { event ->
                    when (event) {
                        is TurnEvent.Transcript -> if (event.text.isNotBlank()) addItem(ChatItem.You(key(), event.text))
                        is TurnEvent.Sentence -> {
                            val k = botKey
                            if (k == null) {
                                botKey = key().also { addItem(ChatItem.Bot(it, event.text)) }
                            } else {
                                _state.update { st ->
                                    st.copy(chat = st.chat.map { if (it is ChatItem.Bot && it.key == k) it.copy(text = it.text + " " + event.text) else it })
                                }
                            }
                            event.audio?.let { player.enqueue(it) }
                        }
                        is TurnEvent.Links -> if (event.links.isNotEmpty()) addItem(ChatItem.Links(key(), event.links))
                        is TurnEvent.Done -> {
                            if (!event.heard) {
                                setPhase(Phase.Ready, "Didn't catch that. Tap to try again.")
                                return@collect
                            }
                            event.turnId?.let { addRating(it) }
                            val fp = event.remindersFingerprint
                            if (fp != null && fp != store.remindersFingerprint) launch { syncReminders() }
                        }
                    }
                }
                player.awaitDone()
                if (_state.value.phase != Phase.Ready) setPhase(Phase.Ready, "Tap to talk")
            } catch (e: CancellationException) {
                throw e
            } catch (e: ApiException) {
                when (e.kind) {
                    ApiException.Kind.Busy -> setPhase(Phase.Ready, "Busy or closing. Try again in a moment.")
                    ApiException.Kind.Unpaired -> unpaired()
                    else -> setPhase(Phase.Ready, "Connection problem. Tap to try again.")
                }
            } catch (e: Exception) {
                setPhase(Phase.Ready, "Connection problem. Tap to try again.")
            }
        }
    }

    /** Good/Bad only under the newest reply; rated rows stay so the choice can be seen/undone. */
    private fun addRating(turnId: Long) = _state.update { s ->
        s.copy(chat = s.chat.filterNot { it is ChatItem.Rating && !it.rated } + ChatItem.Rating(key(), turnId))
    }

    fun rate(item: ChatItem.Rating, value: Int) {
        val newValue = if (item.value == value) 0 else value // tap again to undo
        viewModelScope.launch {
            try {
                api?.post("/api/rate", JSONObject().put("turn_id", item.turnId).put("value", newValue))
                _state.update { s ->
                    s.copy(chat = s.chat.map { if (it.key == item.key) item.copy(value = newValue, rated = true) else it })
                }
            } catch (e: CancellationException) {
                throw e
            } catch (e: Exception) {
                // not saved; the button stays as it was
            }
        }
    }

    fun clearChat() = _state.update {
        it.copy(chat = listOf(ChatItem.Note(key(), "Chat cleared. Jarvis still remembers everything. Tap the mic to talk.")))
    }

    fun toggleWhisper() {
        val on = !_state.value.whisper
        store.whisper = on
        _state.update { it.copy(whisper = on) }
    }

    // --- pairing --------------------------------------------------------------------------
    fun pair(qrText: String?) {
        val pairing = Pairing.fromQr(qrText)
        if (pairing == null) {
            _state.update { it.copy(pairMessage = "That isn't a Jarvis pairing code. On the laptop page, tap Phone, then Show pairing code.") }
            return
        }
        viewModelScope.launch {
            _state.update { it.copy(pairingBusy = true, pairMessage = "Pairing…") }
            try {
                Api(pairing).hello()
                store.pairing = pairing
                _state.update { it.copy(pairing = pairing, pairMessage = null, link = Link.Unknown) }
                if (visible) reconnect()
            } catch (e: ApiException) {
                val text = when (e.kind) {
                    ApiException.Kind.Unpaired -> "This code expired or was already used. Make a new one on the laptop page."
                    ApiException.Kind.Locked -> "Too many wrong attempts. Wait 15 minutes, then try again."
                    ApiException.Kind.Offline -> "Couldn't reach Jarvis. Is it running?"
                    else -> "Pairing didn't work (error ${e.code}). Try a new code."
                }
                _state.update { it.copy(pairMessage = text) }
            } finally {
                _state.update { it.copy(pairingBusy = false) }
            }
        }
    }

    /** Forget the token on this phone (also revoke it on the laptop page). */
    fun unpair() {
        pollJob?.cancel()
        turnJob?.cancel()
        player.stop()
        store.forget()
        viewModelScope.launch(Dispatchers.IO) { ReminderAlarms.sync(getApplication(), emptyList(), null) }
        _state.value = UiState(pairing = null, whisper = store.whisper, startUrl = store.startUrl)
        JarvisApp.busy = false
    }

    // --- self-update ----------------------------------------------------------------------
    fun checkUpdates(force: Boolean = false) {
        if (!updater.enabled) {
            if (force) addNote("This is a test build; it does not update itself.")
            return
        }
        val now = System.currentTimeMillis()
        if (!force && now - store.lastUpdateCheck < 6 * 3600_000L) return
        if (_state.value.update?.ready == true) return
        viewModelScope.launch {
            try {
                val info = updater.check()
                store.lastUpdateCheck = now
                if (info == null) {
                    if (force) addNote("The app is up to date (${BuildConfig.VERSION_NAME}).")
                    return@launch
                }
                _state.update { it.copy(update = UpdateUi(info)) }
                updater.download(info)
                JarvisApp.updateReady = true
                _state.update { it.copy(update = UpdateUi(info, ready = true)) }
            } catch (e: CancellationException) {
                throw e
            } catch (e: Updater.UpdateError) {
                _state.update { s -> s.copy(update = s.update?.copy(error = e.message)) }
            } catch (e: Exception) {
                if (force) addNote("Couldn't check for updates (no internet?).")
            }
        }
    }

    fun updateAllowed(): Boolean = updater.allowed()

    fun installUpdate() {
        JarvisApp.updateFailed.value = false
        _state.update { s -> s.copy(update = s.update?.copy(error = null)) }
        runCatching { updater.install() }.onFailure {
            _state.update { s -> s.copy(update = s.update?.copy(error = "The update could not start. Try again.")) }
        }
    }
}
