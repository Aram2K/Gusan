package com.aram2k.gusan.ui

import android.text.format.DateUtils
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
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
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Mic
import androidx.compose.material.icons.filled.Stop
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.Icon
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableLongStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import com.aram2k.gusan.audio.MicRecorder
import com.aram2k.gusan.core.Meeting
import com.aram2k.gusan.graph
import com.aram2k.gusan.meetings.LiveRecording
import com.aram2k.gusan.meetings.MeetingService
import com.aram2k.gusan.meetings.Work
import kotlinx.coroutines.delay

/** 75 -> "1:15", 3723 -> "1:02:03". */
fun clock(seconds: Long): String {
    val h = seconds / 3600
    val m = seconds % 3600 / 60
    val s = seconds % 60
    return if (h > 0) "%d:%02d:%02d".format(h, m, s) else "%d:%02d".format(m, s)
}

/** What a meeting's row and page say about where it is. */
fun statusText(meeting: Meeting, work: Work?): String = when {
    meeting.status == Meeting.Status.RECORDING -> "Recording…"
    work != null && work.total > 1 -> "Writing the transcript, part ${minOf(work.done + 1, work.total)} of ${work.total}…"
    work != null && work.total == 0 -> "Writing the notes…"
    work != null || meeting.status == Meeting.Status.TRANSCRIBING -> "Writing the transcript…"
    meeting.status == Meeting.Status.WRITING_NOTES -> "Writing the notes…"
    meeting.status == Meeting.Status.DONE -> "Notes ready"
    meeting.status == Meeting.Status.FAILED -> "Notes not written yet"
    else -> "Recorded, no notes yet"
}

@Composable
fun MeetingsScreen(onOpen: (String) -> Unit, onSignIn: () -> Unit) {
    val context = LocalContext.current
    val graph = context.graph
    val meetings by graph.meetings.meetings.collectAsStateWithLifecycle()
    val recording by graph.meetings.recording.collectAsStateWithLifecycle()
    val work by graph.meetings.work.collectAsStateWithLifecycle()
    val account by graph.account.state.collectAsStateWithLifecycle()
    var denied by remember { mutableStateOf(false) }
    val askMic = rememberLauncherForActivityResult(ActivityResultContracts.RequestMultiplePermissions()) {
        if (MicRecorder.hasPermission(context)) {
            denied = false
            MeetingService.startRecording(context)
        } else {
            denied = true
        }
    }

    LaunchedEffect(Unit) { graph.meetings.reload() }

    fun record() {
        when {
            !account.signedIn -> onSignIn()
            MicRecorder.hasPermission(context) -> MeetingService.startRecording(context)
            else -> askMic.launch(voicePermissions())
        }
    }

    LazyColumn(
        Modifier.fillMaxSize(),
        contentPadding = PaddingValues(horizontal = 20.dp, vertical = 16.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp),
    ) {
        item { ScreenTitle("Meetings") }
        item {
            val live = recording
            if (live != null) RecordingCard(live) { MeetingService.stopRecording(context) } else RecordCard(::record)
        }
        if (denied) item {
            AppCard {
                Text("Gusan can't hear without the microphone. Allow it in the phone's settings for Gusan, then record.", style = MaterialTheme.typography.bodyMedium)
            }
        }
        if (account.signedIn && !account.isPro) item {
            AppCard {
                Text("Meeting transcripts need Gusan Pro", style = MaterialTheme.typography.titleMedium, fontWeight = FontWeight.SemiBold)
                Text(
                    "You can record now and keep the recording on this phone. The transcript and notes are written by Gusan Cloud once you're on Pro.",
                    style = MaterialTheme.typography.bodyMedium,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
                OutlinedButton(onClick = { openPricing(context) }) { Text("See plans") }
            }
        }
        val shown = meetings.filter { it.id != recording?.id }
        if (shown.isEmpty() && recording == null) item {
            Text(
                "No meetings yet. Put the phone where it hears everyone and tap Record.",
                style = MaterialTheme.typography.bodyLarge,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
                modifier = Modifier.padding(top = 24.dp),
            )
        }
        items(shown, key = { it.id }) { meeting -> MeetingRow(meeting, work[meeting.id]) { onOpen(meeting.id) } }
    }
}

@Composable
private fun RecordCard(onRecord: () -> Unit) {
    AppCard {
        Button(
            onClick = onRecord,
            modifier = Modifier.fillMaxWidth().heightIn(min = 56.dp),
            shape = RoundedCornerShape(14.dp),
            colors = ButtonDefaults.buttonColors(containerColor = Brand.Red, contentColor = androidx.compose.ui.graphics.Color.White),
        ) {
            Icon(Icons.Filled.Mic, contentDescription = null)
            Spacer(Modifier.width(8.dp))
            Text("Record a meeting", style = MaterialTheme.typography.titleMedium)
        }
        Text(
            "Recording carries on with the screen off. When you stop, Gusan writes the transcript and the notes.",
            style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
    }
}

@Composable
private fun RecordingCard(live: LiveRecording, onStop: () -> Unit) {
    var now by remember { mutableLongStateOf(System.currentTimeMillis()) }
    LaunchedEffect(live.id) {
        while (true) {
            now = System.currentTimeMillis()
            delay(500)
        }
    }
    AppCard {
        Row(verticalAlignment = Alignment.CenterVertically) {
            Box(Modifier.size(12.dp).clip(CircleShape).background(Brand.Red))
            Spacer(Modifier.width(10.dp))
            Text("Recording", Modifier.weight(1f), style = MaterialTheme.typography.titleMedium, fontWeight = FontWeight.SemiBold)
            Text(clock((now - live.startedAtMs).coerceAtLeast(0) / 1000), style = MaterialTheme.typography.titleLarge)
        }
        LinearProgressIndicator(
            progress = { live.level.coerceIn(0f, 1f) },
            modifier = Modifier.fillMaxWidth().height(6.dp).clip(RoundedCornerShape(3.dp)),
            color = Brand.Red,
        )
        Button(
            onClick = onStop,
            modifier = Modifier.fillMaxWidth().heightIn(min = 52.dp),
            shape = RoundedCornerShape(14.dp),
        ) {
            Icon(Icons.Filled.Stop, contentDescription = null)
            Spacer(Modifier.width(8.dp))
            Text("Stop and write notes", style = MaterialTheme.typography.titleMedium)
        }
    }
}

@Composable
private fun MeetingRow(meeting: Meeting, work: Work?, onClick: () -> Unit) {
    AppCard(Modifier.clip(RoundedCornerShape(16.dp)).clickable(onClick = onClick)) {
        Text(meeting.title, style = MaterialTheme.typography.titleMedium, fontWeight = FontWeight.SemiBold, maxLines = 2, overflow = TextOverflow.Ellipsis)
        val whenText = DateUtils.getRelativeTimeSpanString(meeting.createdAtMs, System.currentTimeMillis(), DateUtils.MINUTE_IN_MILLIS).toString()
        Text(
            "$whenText · ${clock(meeting.durationMs / 1000)}",
            style = MaterialTheme.typography.bodyMedium,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
        Text(
            statusText(meeting, work),
            style = MaterialTheme.typography.labelLarge,
            color = when {
                meeting.status == Meeting.Status.DONE -> successColor()
                meeting.status == Meeting.Status.FAILED && work == null -> MaterialTheme.colorScheme.error
                else -> MaterialTheme.colorScheme.primary
            },
        )
    }
}
