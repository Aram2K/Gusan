package com.aram2k.gusan.ui

import androidx.compose.foundation.Canvas
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.navigationBarsPadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.statusBarsPadding
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Check
import androidx.compose.material.icons.filled.Delete
import androidx.compose.material.icons.filled.KeyboardArrowDown
import androidx.compose.material.icons.filled.Mic
import androidx.compose.material.icons.filled.Pause
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.FilledIconButton
import androidx.compose.material3.FilledTonalIconButton
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.IconButtonDefaults
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableLongStateOf
import androidx.compose.runtime.mutableStateListOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.geometry.CornerRadius
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import com.aram2k.gusan.graph
import com.aram2k.gusan.meetings.MeetingService
import kotlinx.coroutines.delay

private const val BARS = 56

/** The meeting being recorded, full screen: the time, the sound, pause and stop. */
@Composable
fun RecordingScreen(onMinimize: () -> Unit, onStopped: (meetingId: String?) -> Unit) {
    val context = LocalContext.current
    val live by context.graph.meetings.recording.collectAsStateWithLifecycle()
    var now by remember { mutableLongStateOf(System.currentTimeMillis()) }
    val levels = remember { mutableStateListOf<Float>().apply { repeat(BARS) { add(0f) } } }
    var discarding by remember { mutableStateOf(false) }
    var everStarted by remember { mutableStateOf(false) }
    var lastId by remember { mutableStateOf<String?>(null) }
    var discarded by remember { mutableStateOf(false) }
    LaunchedEffect(live?.id) { live?.id?.let { lastId = it } }

    LaunchedEffect(Unit) { while (true) { now = System.currentTimeMillis(); delay(250) } }
    LaunchedEffect(live?.level, live?.paused) {
        val l = live ?: return@LaunchedEffect
        levels.removeAt(0)
        levels.add(if (l.paused) 0f else l.level)
    }
    // Recording ended (stopped here, from the notification, or it never started): leave.
    LaunchedEffect(live == null) {
        if (live != null) everStarted = true
        else {
            delay(if (everStarted) 0 else 2500)
            if (context.graph.meetings.recording.value == null) onStopped(if (discarded) null else lastId)
        }
    }

    val rec = recordColor()
    val paused = live?.paused == true
    Column(
        Modifier.fillMaxSize().background(MaterialTheme.colorScheme.background).statusBarsPadding().navigationBarsPadding().padding(24.dp),
        horizontalAlignment = Alignment.CenterHorizontally,
    ) {
        Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
            IconButton(onClick = onMinimize) { Icon(Icons.Filled.KeyboardArrowDown, contentDescription = "Back to Home, keep recording") }
            Spacer(Modifier.weight(1f))
        }
        Spacer(Modifier.weight(1f))
        Text(
            when { live == null -> "Starting…"; paused -> "Paused"; else -> "Recording" },
            style = MaterialTheme.typography.titleMedium,
            color = if (paused) MaterialTheme.colorScheme.onSurfaceVariant else rec,
        )
        Text(
            clock((live?.elapsedMs(now) ?: 0) / 1000),
            fontSize = 64.sp,
            fontWeight = FontWeight.Light,
            fontFamily = FontFamily.Monospace,
            modifier = Modifier.padding(vertical = 12.dp),
        )
        Canvas(Modifier.fillMaxWidth().height(120.dp).padding(vertical = 8.dp)) {
            val step = size.width / BARS
            val w = step * 0.55f
            levels.forEachIndexed { i, level ->
                val h = maxOf(6.dp.toPx(), level * size.height)
                drawRoundRect(
                    color = if (paused) Color.Gray.copy(alpha = 0.4f) else rec.copy(alpha = 0.35f + 0.65f * (i + 1) / BARS),
                    topLeft = Offset(i * step + (step - w) / 2, (size.height - h) / 2),
                    size = Size(w, h),
                    cornerRadius = CornerRadius(w / 2),
                )
            }
        }
        Text(
            if (paused) "The microphone is off." else "Keep the phone near the people talking. You can lock the screen.",
            style = MaterialTheme.typography.bodyMedium,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
            textAlign = TextAlign.Center,
            modifier = Modifier.padding(top = 8.dp),
        )
        Spacer(Modifier.weight(1f))
        Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceEvenly, verticalAlignment = Alignment.CenterVertically) {
            Labeled("Discard") {
                FilledTonalIconButton(onClick = { discarding = true }, enabled = live != null, modifier = Modifier.size(60.dp)) {
                    Icon(Icons.Filled.Delete, contentDescription = "Discard")
                }
            }
            Labeled(if (paused) "Resume" else "Pause") {
                FilledIconButton(
                    onClick = { if (paused) MeetingService.resume(context) else MeetingService.pause(context) },
                    enabled = live != null,
                    modifier = Modifier.size(84.dp),
                    shape = CircleShape,
                    colors = IconButtonDefaults.filledIconButtonColors(containerColor = rec, contentColor = Color.White),
                ) { Icon(if (paused) Icons.Filled.Mic else Icons.Filled.Pause, contentDescription = null, modifier = Modifier.size(36.dp)) }
            }
            Labeled("Done") {
                FilledTonalIconButton(onClick = { MeetingService.stopRecording(context) }, enabled = live != null, modifier = Modifier.size(60.dp)) {
                    Icon(Icons.Filled.Check, contentDescription = "Done, write the notes")
                }
            }
        }
        Spacer(Modifier.height(16.dp))
    }

    if (discarding) {
        AlertDialog(
            onDismissRequest = { discarding = false },
            title = { Text("Discard this recording?") },
            text = { Text("It's deleted and no notes are written.") },
            confirmButton = {
                TextButton(onClick = { discarding = false; discarded = true; MeetingService.discard(context) }) { Text("Discard", color = MaterialTheme.colorScheme.error) }
            },
            dismissButton = { TextButton(onClick = { discarding = false }) { Text("Keep recording") } },
        )
    }
}

@Composable
private fun Labeled(label: String, content: @Composable () -> Unit) {
    Column(horizontalAlignment = Alignment.CenterHorizontally) {
        Box(contentAlignment = Alignment.Center) { content() }
        Text(label, style = MaterialTheme.typography.labelLarge, color = MaterialTheme.colorScheme.onSurfaceVariant, modifier = Modifier.padding(top = 8.dp))
    }
}
