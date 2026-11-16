package com.aram2k.gusan.ui

import android.text.format.DateUtils
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.animation.core.RepeatMode
import androidx.compose.animation.core.animateFloat
import androidx.compose.animation.core.infiniteRepeatable
import androidx.compose.animation.core.rememberInfiniteTransition
import androidx.compose.animation.core.tween
import androidx.compose.foundation.ExperimentalFoundationApi
import androidx.compose.foundation.Image
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.combinedClickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.navigationBarsPadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.statusBarsPadding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowForward
import androidx.compose.material.icons.filled.AccountCircle
import androidx.compose.material.icons.filled.ContentCopy
import androidx.compose.material.icons.filled.GraphicEq
import androidx.compose.material.icons.filled.Mic
import androidx.compose.material.icons.outlined.Keyboard
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.SegmentedButton
import androidx.compose.material3.SegmentedButtonDefaults
import androidx.compose.material3.SingleChoiceSegmentedButtonRow
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableLongStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.alpha
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.LifecycleResumeEffect
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import com.aram2k.gusan.R
import com.aram2k.gusan.audio.MicRecorder
import com.aram2k.gusan.core.Meeting
import com.aram2k.gusan.core.SmartEntry
import com.aram2k.gusan.core.SmartStats
import com.aram2k.gusan.graph
import com.aram2k.gusan.meetings.LiveRecording
import com.aram2k.gusan.meetings.MeetingService
import com.aram2k.gusan.meetings.Work
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

@Composable
fun HomeScreen(onSettings: () -> Unit, onSetup: () -> Unit, onSignIn: () -> Unit, onMeeting: (String) -> Unit, onRecording: () -> Unit) {
    val context = LocalContext.current
    val graph = context.graph
    val account by graph.account.state.collectAsStateWithLifecycle()
    val meetings by graph.meetings.meetings.collectAsStateWithLifecycle()
    val recording by graph.meetings.recording.collectAsStateWithLifecycle()
    val work by graph.meetings.work.collectAsStateWithLifecycle()
    val readiness = rememberReadiness()
    val scope = rememberCoroutineScope()
    var tab by rememberSaveable { mutableIntStateOf(0) }
    var dictations by remember { mutableStateOf<List<SmartEntry>>(emptyList()) }
    var micRefused by remember { mutableStateOf(false) }

    suspend fun loadDictations() { dictations = withContext(Dispatchers.IO) { graph.history.list() } }
    LaunchedEffect(Unit) { graph.meetings.reload() }
    // The keyboard writes while another app is in front: read again whenever Gusan comes back.
    LifecycleResumeEffect(Unit) {
        scope.launch { loadDictations() }
        onPauseOrDispose { }
    }

    val askMic = rememberLauncherForActivityResult(ActivityResultContracts.RequestMultiplePermissions()) {
        if (MicRecorder.hasPermission(context)) { MeetingService.startRecording(context); onRecording() } else micRefused = true
    }
    fun record() {
        when {
            !account.signedIn -> onSignIn()
            MicRecorder.hasPermission(context) -> { MeetingService.startRecording(context); onRecording() }
            else -> askMic.launch(
                if (android.os.Build.VERSION.SDK_INT >= 33) arrayOf(android.Manifest.permission.RECORD_AUDIO, android.Manifest.permission.POST_NOTIFICATIONS)
                else arrayOf(android.Manifest.permission.RECORD_AUDIO),
            )
        }
    }

    val missing = listOf(!account.signedIn, !readiness.microphone, !readiness.keyboardReady).count { it }

    Box(Modifier.fillMaxSize()) {
        LazyColumn(
            Modifier.fillMaxSize(),
            contentPadding = PaddingValues(start = 20.dp, end = 20.dp, bottom = 120.dp),
            verticalArrangement = Arrangement.spacedBy(10.dp),
        ) {
            item {
                Row(Modifier.fillMaxWidth().statusBarsPadding().padding(top = 8.dp, bottom = 8.dp), verticalAlignment = Alignment.CenterVertically) {
                    Image(painterResource(R.drawable.gusan_logo), contentDescription = null, modifier = Modifier.size(32.dp))
                    Spacer(Modifier.width(10.dp))
                    Text("Gusan", style = MaterialTheme.typography.headlineSmall, modifier = Modifier.weight(1f))
                    if (account.signedIn) PlanBadge(account.isPro)
                    IconButton(onClick = onSettings) {
                        Icon(Icons.Filled.AccountCircle, contentDescription = "Settings and account", modifier = Modifier.size(30.dp))
                    }
                }
            }
            if (missing > 0) item {
                Surface(
                    onClick = onSetup,
                    shape = RoundedCornerShape(20.dp),
                    color = MaterialTheme.colorScheme.primaryContainer,
                    modifier = Modifier.fillMaxWidth(),
                ) {
                    Row(Modifier.padding(16.dp), verticalAlignment = Alignment.CenterVertically) {
                        Column(Modifier.weight(1f)) {
                            Text("Finish setting up", style = MaterialTheme.typography.titleMedium, color = MaterialTheme.colorScheme.onPrimaryContainer)
                            Text(
                                if (missing == 1) "One step left" else "$missing steps left",
                                style = MaterialTheme.typography.bodyMedium,
                                color = MaterialTheme.colorScheme.onPrimaryContainer.copy(alpha = 0.8f),
                            )
                        }
                        Icon(Icons.AutoMirrored.Filled.ArrowForward, contentDescription = null, tint = MaterialTheme.colorScheme.onPrimaryContainer)
                    }
                }
            }
            if (micRefused) item {
                Panel {
                    Text("Gusan needs the microphone to record.", style = MaterialTheme.typography.titleMedium)
                    TextButton(onClick = { openAppSettings(context) }) { Text("Open settings") }
                }
            }
            item {
                SingleChoiceSegmentedButtonRow(Modifier.fillMaxWidth().padding(top = 6.dp, bottom = 4.dp)) {
                    listOf("Meetings", "Dictations").forEachIndexed { i, label ->
                        SegmentedButton(
                            selected = tab == i,
                            onClick = { tab = i },
                            shape = SegmentedButtonDefaults.itemShape(i, 2),
                            icon = {},
                        ) { Text(label) }
                    }
                }
            }
            if (tab == 0) {
                val shown = meetings.filter { it.id != recording?.id }
                if (shown.isEmpty()) item {
                    EmptyState(
                        Icons.Filled.GraphicEq,
                        "Record your first meeting",
                        "Put your phone on the table and tap Record. Gusan writes the transcript and notes with action items.",
                    )
                }
                items(shown, key = { it.id }) { MeetingRow(it, work[it.id]) { onMeeting(it.id) } }
            } else {
                if (dictations.isNotEmpty()) item { StatsCard(SmartStats.of(dictations, System.currentTimeMillis())) }
                if (dictations.isEmpty()) item {
                    EmptyState(
                        Icons.Outlined.Keyboard,
                        "Talk in any app",
                        "Open the Gusan keyboard anywhere, tap the mic and speak. What Gusan writes is kept here, so nothing gets lost.",
                    )
                }
                items(dictations, key = { it.id }) { entry ->
                    DictationRow(entry, onCopy = { copyText(context, entry.result) }, onDelete = {
                        scope.launch {
                            withContext(Dispatchers.IO) { graph.history.remove(entry.id) }
                            loadDictations()
                        }
                    })
                }
            }
        }
        Box(Modifier.align(Alignment.BottomCenter).navigationBarsPadding().padding(20.dp)) {
            val live = recording
            if (live != null) RecordingPill(live, onRecording) else RecordButton(::record)
        }
    }
}

@Composable
private fun EmptyState(icon: ImageVector, title: String, body: String) {
    Column(Modifier.fillMaxWidth().padding(top = 48.dp, start = 16.dp, end = 16.dp), horizontalAlignment = Alignment.CenterHorizontally) {
        Box(Modifier.size(64.dp).clip(CircleShape).background(MaterialTheme.colorScheme.surfaceContainerHighest), contentAlignment = Alignment.Center) {
            Icon(icon, contentDescription = null, tint = MaterialTheme.colorScheme.onSurfaceVariant, modifier = Modifier.size(30.dp))
        }
        Text(title, style = MaterialTheme.typography.titleLarge, modifier = Modifier.padding(top = 16.dp, bottom = 6.dp))
        Text(body, style = MaterialTheme.typography.bodyMedium, color = MaterialTheme.colorScheme.onSurfaceVariant, textAlign = TextAlign.Center)
    }
}

@Composable
private fun RecordButton(onClick: () -> Unit) {
    Surface(onClick = onClick, shape = RoundedCornerShape(32.dp), color = recordColor(), shadowElevation = 6.dp) {
        Row(Modifier.heightIn(min = 60.dp).padding(horizontal = 28.dp), verticalAlignment = Alignment.CenterVertically) {
            Icon(Icons.Filled.Mic, contentDescription = null, tint = Color.White)
            Spacer(Modifier.width(10.dp))
            Text("Record", style = MaterialTheme.typography.titleMedium, color = Color.White)
        }
    }
}

@Composable
private fun RecordingPill(live: LiveRecording, onClick: () -> Unit) {
    var now by remember { mutableLongStateOf(System.currentTimeMillis()) }
    LaunchedEffect(live.id) { while (true) { now = System.currentTimeMillis(); delay(500) } }
    val pulse by rememberInfiniteTransition(label = "pulse").animateFloat(
        initialValue = 1f, targetValue = 0.3f, animationSpec = infiniteRepeatable(tween(700), RepeatMode.Reverse), label = "dot",
    )
    Surface(onClick = onClick, shape = RoundedCornerShape(32.dp), color = MaterialTheme.colorScheme.inverseSurface, shadowElevation = 6.dp) {
        Row(Modifier.heightIn(min = 60.dp).padding(horizontal = 24.dp), verticalAlignment = Alignment.CenterVertically) {
            Box(Modifier.size(10.dp).alpha(if (live.paused) 1f else pulse).clip(CircleShape).background(recordColor()))
            Spacer(Modifier.width(12.dp))
            Text(
                (if (live.paused) "Paused  " else "Recording  ") + clock(live.elapsedMs(now) / 1000),
                style = MaterialTheme.typography.titleMedium,
                color = MaterialTheme.colorScheme.inverseOnSurface,
            )
        }
    }
}

@Composable
private fun StatsCard(stats: SmartStats) {
    Panel {
        Text("This week", style = MaterialTheme.typography.labelLarge, color = MaterialTheme.colorScheme.onSurfaceVariant)
        Row {
            Stat("%,d".format(stats.wordsThisWeek), "words written", Modifier.weight(1f))
            Stat(if (stats.minutesSavedThisWeek >= 60) "%dh %dm".format(stats.minutesSavedThisWeek / 60, stats.minutesSavedThisWeek % 60) else "${stats.minutesSavedThisWeek} min", "saved vs typing", Modifier.weight(1f))
        }
    }
}

@Composable
private fun Stat(value: String, label: String, modifier: Modifier) {
    Column(modifier) {
        Text(value, style = MaterialTheme.typography.headlineMedium)
        Text(label, style = MaterialTheme.typography.bodyMedium, color = MaterialTheme.colorScheme.onSurfaceVariant)
    }
}

private fun ago(ms: Long): String =
    DateUtils.getRelativeTimeSpanString(ms, System.currentTimeMillis(), DateUtils.MINUTE_IN_MILLIS, DateUtils.FORMAT_ABBREV_RELATIVE).toString()

@Composable
private fun MeetingRow(meeting: Meeting, work: Work?, onClick: () -> Unit) {
    Surface(onClick = onClick, shape = RoundedCornerShape(20.dp), color = MaterialTheme.colorScheme.surfaceContainer, modifier = Modifier.fillMaxWidth()) {
        Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(4.dp)) {
            Text(meeting.title, style = MaterialTheme.typography.titleMedium, maxLines = 2, overflow = TextOverflow.Ellipsis)
            val summary = meeting.notes.lineSequence().map { it.trim() }.firstOrNull { it.isNotEmpty() && !it.startsWith("#") }
            if (summary != null) {
                Text(summary.replace("**", ""), style = MaterialTheme.typography.bodyMedium, color = MaterialTheme.colorScheme.onSurfaceVariant, maxLines = 2, overflow = TextOverflow.Ellipsis)
            }
            Row(verticalAlignment = Alignment.CenterVertically, modifier = Modifier.padding(top = 4.dp)) {
                Text(
                    "${ago(meeting.createdAtMs)} · ${clock(meeting.durationMs / 1000)}",
                    style = MaterialTheme.typography.labelMedium,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                    modifier = Modifier.weight(1f),
                )
                val status = statusText(meeting, work)
                if (status.isNotEmpty()) {
                    val failed = meeting.status == Meeting.Status.FAILED && work == null
                    val tint = if (failed) MaterialTheme.colorScheme.error else MaterialTheme.colorScheme.primary
                    Text(
                        status,
                        style = MaterialTheme.typography.labelMedium,
                        fontWeight = FontWeight.SemiBold,
                        color = tint,
                        modifier = Modifier.clip(RoundedCornerShape(50)).background(tint.copy(alpha = 0.12f)).padding(horizontal = 10.dp, vertical = 4.dp),
                    )
                }
            }
        }
    }
}

@OptIn(ExperimentalFoundationApi::class)
@Composable
private fun DictationRow(entry: SmartEntry, onCopy: () -> Unit, onDelete: () -> Unit) {
    var confirm by remember { mutableStateOf(false) }
    Surface(shape = RoundedCornerShape(20.dp), color = MaterialTheme.colorScheme.surfaceContainer, modifier = Modifier.fillMaxWidth()) {
        Column(Modifier.combinedClickable(onClick = onCopy, onLongClick = { confirm = true }).padding(16.dp)) {
            Text(entry.result, style = MaterialTheme.typography.bodyLarge, maxLines = 5, overflow = TextOverflow.Ellipsis)
            Row(verticalAlignment = Alignment.CenterVertically, modifier = Modifier.padding(top = 8.dp)) {
                Text(
                    ago(entry.atMs) + " · “" + entry.heard + "”",
                    style = MaterialTheme.typography.labelMedium,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                    maxLines = 1,
                    overflow = TextOverflow.Ellipsis,
                    modifier = Modifier.weight(1f),
                )
                Icon(Icons.Filled.ContentCopy, contentDescription = "Copy", tint = MaterialTheme.colorScheme.onSurfaceVariant, modifier = Modifier.size(18.dp))
            }
        }
    }
    if (confirm) {
        AlertDialog(
            onDismissRequest = { confirm = false },
            title = { Text("Delete this?") },
            text = { Text("It's removed from this phone.") },
            confirmButton = { TextButton(onClick = { confirm = false; onDelete() }) { Text("Delete", color = MaterialTheme.colorScheme.error) } },
            dismissButton = { TextButton(onClick = { confirm = false }) { Text("Cancel") } },
        )
    }
}
