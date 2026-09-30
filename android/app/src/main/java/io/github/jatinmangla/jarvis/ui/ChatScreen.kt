package io.github.jatinmangla.jarvis.ui

import androidx.compose.animation.core.animateFloatAsState
import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.BoxWithConstraints
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.aspectRatio
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.safeDrawingPadding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.layout.widthIn
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.DropdownMenu
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.material3.FilterChip
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Icon
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import coil.compose.AsyncImage
import io.github.jatinmangla.jarvis.BuildConfig
import io.github.jatinmangla.jarvis.ChatItem
import io.github.jatinmangla.jarvis.Link
import io.github.jatinmangla.jarvis.Phase
import io.github.jatinmangla.jarvis.R
import io.github.jatinmangla.jarvis.UiState
import io.github.jatinmangla.jarvis.net.LinkCard
import io.github.jatinmangla.jarvis.net.Status

/** What the chat screen can ask the activity to do. */
interface ChatActions {
    fun mic()
    fun whisper()
    fun clear()
    fun rate(item: ChatItem.Rating, value: Int)
    fun openUrl(url: String)
    fun openVideo(videoId: String)
    fun checkUpdates()
    fun installUpdate()
    fun unpair()
}

@Composable
fun ChatScreen(state: UiState, actions: ChatActions) {
    Column(Modifier.safeDrawingPadding().padding(horizontal = 14.dp, vertical = 10.dp)) {
        Header(state, actions)
        state.update?.let { UpdateBanner(it.info.versionName, it.ready, it.error, actions) }
        state.status?.let { Limits(it) }
        HorizontalDivider(Modifier.padding(top = 8.dp), color = MaterialTheme.colorScheme.outline)
        ChatLog(state, actions, Modifier.weight(1f))
        HorizontalDivider(color = MaterialTheme.colorScheme.outline)
        Controls(state, actions)
    }
}

@Composable
private fun Header(state: UiState, actions: ChatActions) {
    var menu by remember { mutableStateOf(false) }
    var confirmUnpair by remember { mutableStateOf(false) }
    Row(verticalAlignment = Alignment.CenterVertically) {
        Row(Modifier.weight(1f), verticalAlignment = Alignment.Bottom) {
            Text(state.status?.name ?: "Jarvis", fontSize = 20.sp, fontWeight = FontWeight.Bold)
            Spacer(Modifier.width(8.dp))
            val s = state.status
            val saved = when {
                s == null -> ""
                !s.saveOk -> "saved ${s.lastSaved ?: "-"} (last save failed)"
                s.lastSaved != null -> "saved ${s.lastSaved}"
                else -> "not saved yet"
            }
            Text(
                saved, fontSize = 12.sp, maxLines = 1, overflow = TextOverflow.Ellipsis,
                color = if (s?.saveOk == false) MaterialTheme.colorScheme.error else MaterialTheme.colorScheme.onSurfaceVariant,
            )
        }
        TextButton(onClick = actions::clear) { Text("Clear") }
        Box {
            TextButton(onClick = { menu = true }) { Text("Menu") }
            DropdownMenu(expanded = menu, onDismissRequest = { menu = false }) {
                DropdownMenuItem(text = { Text("Check for updates") }, onClick = { menu = false; actions.checkUpdates() })
                DropdownMenuItem(text = { Text("Unpair this phone") }, onClick = { menu = false; confirmUnpair = true })
                DropdownMenuItem(
                    text = { Text("Jarvis app ${BuildConfig.VERSION_NAME} · ${state.pairing?.host ?: ""}", fontSize = 12.sp) },
                    onClick = { menu = false }, enabled = false,
                )
            }
        }
    }
    if (confirmUnpair) {
        AlertDialog(
            onDismissRequest = { confirmUnpair = false },
            title = { Text("Unpair this phone?") },
            text = { Text("The key is deleted from this phone. Also tap Revoke for it on the laptop page (Phone).") },
            confirmButton = { TextButton(onClick = { confirmUnpair = false; actions.unpair() }) { Text("Unpair") } },
            dismissButton = { TextButton(onClick = { confirmUnpair = false }) { Text("Cancel") } },
        )
    }
}

@Composable
private fun UpdateBanner(version: String, ready: Boolean, error: String?, actions: ChatActions) {
    Surface(
        Modifier.fillMaxWidth().padding(top = 6.dp), shape = RoundedCornerShape(10.dp),
        color = MaterialTheme.colorScheme.primary.copy(alpha = 0.12f),
    ) {
        Row(Modifier.padding(horizontal = 12.dp, vertical = 6.dp), verticalAlignment = Alignment.CenterVertically) {
            Text(
                error ?: if (ready) "Version $version is ready. It installs when you leave the app, or now:"
                else "Downloading version $version…",
                Modifier.weight(1f), fontSize = 13.sp,
            )
            if (ready) TextButton(onClick = actions::installUpdate) { Text("Install") }
        }
    }
}

@Composable
private fun Limits(s: Status) {
    Column(Modifier.padding(top = 8.dp)) {
        s.meters.chunked(2).forEach { row ->
            Row(horizontalArrangement = Arrangement.spacedBy(14.dp)) {
                row.forEach { m ->
                    val warn = m.percent >= s.notifyPct
                    val color = if (warn) MaterialTheme.colorScheme.error else MaterialTheme.colorScheme.onSurfaceVariant
                    Column(Modifier.weight(1f).padding(bottom = 4.dp)) {
                        Text("${m.label}: ${m.amount}", fontSize = 11.sp, color = color, maxLines = 1,
                            overflow = TextOverflow.Ellipsis, fontWeight = if (warn) FontWeight.SemiBold else null)
                        LinearProgressIndicator(
                            progress = { (m.percent / 100f).coerceIn(0f, 1f) },
                            modifier = Modifier.fillMaxWidth().height(4.dp).padding(top = 3.dp),
                            color = if (warn) MaterialTheme.colorScheme.error else MaterialTheme.colorScheme.primary,
                            trackColor = MaterialTheme.colorScheme.outline,
                            drawStopIndicator = {},
                        )
                    }
                }
                if (row.size == 1) Spacer(Modifier.weight(1f))
            }
        }
        if (s.warnings.isNotEmpty()) {
            Text(
                s.warnings.joinToString(" ") + " It stops automatically at ${s.stopPct}% so this stays free.",
                Modifier.fillMaxWidth().padding(top = 6.dp).clip(RoundedCornerShape(10.dp))
                    .background(Listen.copy(alpha = 0.12f)).padding(horizontal = 12.dp, vertical = 8.dp),
                color = MaterialTheme.colorScheme.error, fontSize = 14.sp,
            )
        }
    }
}

@Composable
private fun ChatLog(state: UiState, actions: ChatActions, modifier: Modifier) {
    val list = rememberLazyListState()
    val lastText = (state.chat.lastOrNull() as? ChatItem.Bot)?.text?.length ?: 0
    LaunchedEffect(state.chat.size, lastText) {
        if (state.chat.isNotEmpty()) list.animateScrollToItem(state.chat.size - 1)
    }
    if (state.chat.isEmpty()) {
        Box(modifier.fillMaxWidth(), contentAlignment = Alignment.Center) {
            Text("Tap the mic and speak. It stops listening when you pause.",
                color = MaterialTheme.colorScheme.onSurfaceVariant, textAlign = TextAlign.Center, fontSize = 14.sp)
        }
        return
    }
    LazyColumn(
        modifier.fillMaxWidth(), state = list,
        verticalArrangement = Arrangement.spacedBy(10.dp),
        contentPadding = androidx.compose.foundation.layout.PaddingValues(vertical = 12.dp),
    ) {
        items(state.chat, key = { it.key }) { item ->
            when (item) {
                is ChatItem.You -> Bubble(item.text, mine = true)
                is ChatItem.Bot -> Bubble(item.text, mine = false)
                is ChatItem.Note -> Text(item.text, fontSize = 13.sp, color = MaterialTheme.colorScheme.onSurfaceVariant,
                    modifier = Modifier.fillMaxWidth(0.88f))
                is ChatItem.Links -> LinkCards(item.links, actions)
                is ChatItem.Rating -> RatingRow(item, actions)
            }
        }
    }
}

@Composable
private fun Bubble(text: String, mine: Boolean) {
    BoxWithConstraints(Modifier.fillMaxWidth(), contentAlignment = if (mine) Alignment.CenterEnd else Alignment.CenterStart) {
        val shape = if (mine) RoundedCornerShape(14.dp, 14.dp, 4.dp, 14.dp) else RoundedCornerShape(14.dp, 14.dp, 14.dp, 4.dp)
        Surface(
            shape = shape,
            color = if (mine) MaterialTheme.colorScheme.primary else MaterialTheme.colorScheme.surfaceVariant,
            contentColor = if (mine) Color.White else MaterialTheme.colorScheme.onSurface,
            border = if (mine) null else BorderStroke(1.dp, MaterialTheme.colorScheme.outline),
            modifier = Modifier.widthIn(max = maxWidth * 0.88f),
        ) {
            Text(text, Modifier.padding(horizontal = 14.dp, vertical = 10.dp))
        }
    }
}

@Composable
private fun RatingRow(item: ChatItem.Rating, actions: ChatActions) {
    Row(horizontalArrangement = Arrangement.spacedBy(6.dp)) {
        for ((value, label) in listOf(1 to "Good", -1 to "Bad")) {
            val on = item.value == value
            OutlinedButton(
                onClick = { actions.rate(item, value) },
                border = BorderStroke(1.dp, if (on) MaterialTheme.colorScheme.primary else MaterialTheme.colorScheme.outline),
                contentPadding = androidx.compose.foundation.layout.PaddingValues(horizontal = 10.dp, vertical = 0.dp),
                modifier = Modifier.height(30.dp).semantics { contentDescription = "$label reply" },
            ) {
                Text(label, fontSize = 12.sp,
                    color = if (on) MaterialTheme.colorScheme.onSurface else MaterialTheme.colorScheme.onSurfaceVariant)
            }
        }
    }
}

@Composable
private fun LinkCards(links: List<LinkCard>, actions: ChatActions) {
    var firstVideo = true
    Column(Modifier.widthIn(max = 420.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
        for (l in links) {
            val big = l.isVideo && firstVideo
            if (l.isVideo) firstVideo = false
            val open = { if (l.isVideo) actions.openVideo(l.videoId!!) else actions.openUrl(l.url) }
            Surface(
                shape = RoundedCornerShape(12.dp), color = MaterialTheme.colorScheme.surfaceVariant,
                border = BorderStroke(1.dp, MaterialTheme.colorScheme.outline),
                modifier = Modifier.fillMaxWidth().clickable(onClick = open),
            ) {
                if (big) {
                    Column {
                        l.image?.let { Thumb(it, Modifier.fillMaxWidth().aspectRatio(16f / 9f)) }
                        CardBody(l, Modifier.padding(horizontal = 12.dp, vertical = 8.dp), play = true)
                    }
                } else {
                    Row(Modifier.heightIn(min = 72.dp)) {
                        l.image?.let { Thumb(it, Modifier.width(96.dp).heightIn(min = 72.dp)) }
                        CardBody(l, Modifier.padding(start = if (l.image == null) 12.dp else 10.dp, end = 10.dp, top = 8.dp, bottom = 8.dp), play = false)
                    }
                }
            }
        }
    }
}

@Composable
private fun Thumb(url: String, modifier: Modifier) {
    AsyncImage(model = url, contentDescription = null, contentScale = ContentScale.Crop,
        modifier = modifier.background(MaterialTheme.colorScheme.outline))
}

@Composable
private fun CardBody(l: LinkCard, modifier: Modifier, play: Boolean) {
    Column(modifier, verticalArrangement = Arrangement.spacedBy(2.dp)) {
        val site = listOf(l.site, if (l.kind == "product") "" else l.note).filter { it.isNotBlank() }.joinToString(" · ")
        if (site.isNotEmpty()) Text(site, fontSize = 12.sp, color = MaterialTheme.colorScheme.onSurfaceVariant, maxLines = 1, overflow = TextOverflow.Ellipsis)
        Text(l.title, fontSize = 14.sp, fontWeight = FontWeight.SemiBold, color = MaterialTheme.colorScheme.primary,
            maxLines = 2, overflow = TextOverflow.Ellipsis)
        l.price?.let {
            Text(it, fontSize = 13.sp, fontWeight = FontWeight.Bold, color = Color.White,
                modifier = Modifier.clip(RoundedCornerShape(6.dp)).background(Speak).padding(horizontal = 7.dp, vertical = 1.dp))
        }
        if (l.snippet.isNotBlank()) Text(l.snippet, fontSize = 12.sp, color = MaterialTheme.colorScheme.onSurfaceVariant,
            maxLines = 2, overflow = TextOverflow.Ellipsis)
        if (play) Text("Play on YouTube", fontSize = 14.sp, fontWeight = FontWeight.SemiBold, color = Color.White,
            modifier = Modifier.padding(top = 6.dp).clip(RoundedCornerShape(999.dp)).background(Listen)
                .padding(horizontal = 14.dp, vertical = 6.dp))
    }
}

@Composable
private fun Controls(state: UiState, actions: ChatActions) {
    Column(Modifier.fillMaxWidth().padding(top = 8.dp), horizontalAlignment = Alignment.CenterHorizontally) {
        Text(state.statusText, fontSize = 14.sp, color = MaterialTheme.colorScheme.onSurfaceVariant,
            textAlign = TextAlign.Center, modifier = Modifier.heightIn(min = 20.dp))
        Spacer(Modifier.height(6.dp))
        Row(verticalAlignment = Alignment.CenterVertically) {
            Box(Modifier.weight(1f), contentAlignment = Alignment.CenterEnd) {
                FilterChip(
                    selected = state.whisper, onClick = actions::whisper,
                    label = { Text(if (state.whisper) "Whisper: on" else "Whisper") },
                )
            }
            Spacer(Modifier.width(14.dp))
            MicButton(state, actions::mic)
            Spacer(Modifier.width(14.dp))
            Box(Modifier.weight(1f), contentAlignment = Alignment.CenterStart) {
                val offline = state.link == Link.Offline || state.link == Link.Closed
                val url = state.startUrl
                if (state.link == Link.Unpaired) {
                    OutlinedButton(onClick = actions::unpair) { Text("Pair again") }
                } else if (offline && url != null) {
                    Button(onClick = { actions.openUrl(url) },
                        colors = androidx.compose.material3.ButtonDefaults.buttonColors(containerColor = Speak)) {
                        Text("Start")
                    }
                }
            }
        }
    }
}

@Composable
private fun MicButton(state: UiState, onClick: () -> Unit) {
    val color = when (state.phase) {
        Phase.Listening -> Listen
        Phase.Thinking -> Think
        Phase.Speaking -> Speak
        Phase.Ready -> Accent
    }
    val ring by animateFloatAsState(if (state.phase == Phase.Listening) state.level else 0f, label = "level")
    val enabled = state.link != Link.Unpaired
    Box(Modifier.size(96.dp), contentAlignment = Alignment.Center) {
        Box(Modifier.size(72.dp + (ring * 24).dp).clip(CircleShape).background(Listen.copy(alpha = 0.25f * ring)))
        Box(
            Modifier.size(72.dp).clip(CircleShape).background(if (enabled) color else color.copy(alpha = 0.45f))
                .border(0.dp, Color.Transparent, CircleShape)
                .clickable(enabled = enabled, onClick = onClick)
                .semantics { contentDescription = if (state.phase == Phase.Listening) "Stop listening" else "Tap to talk" },
            contentAlignment = Alignment.Center,
        ) {
            Icon(painterResource(R.drawable.ic_notification), contentDescription = null, tint = Color.White,
                modifier = Modifier.size(32.dp))
        }
    }
}
