package com.aram2k.gusan.ui

import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import android.content.Intent
import android.widget.Toast
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.text.selection.SelectionContainer
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material.icons.filled.CheckBox
import androidx.compose.material.icons.filled.CheckBoxOutlineBlank
import androidx.compose.material.icons.filled.ContentCopy
import androidx.compose.material.icons.filled.Delete
import androidx.compose.material.icons.filled.Edit
import androidx.compose.material.icons.filled.Share
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Tab
import androidx.compose.material3.TabRow
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.AnnotatedString
import androidx.compose.ui.text.SpanStyle
import androidx.compose.ui.text.buildAnnotatedString
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.withStyle
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import com.aram2k.gusan.core.Meeting
import com.aram2k.gusan.core.NoteBlock
import com.aram2k.gusan.core.NotesMarkdown
import com.aram2k.gusan.core.Span
import com.aram2k.gusan.graph
import com.aram2k.gusan.meetings.MeetingService

fun copyText(context: Context, text: String) {
    (context.getSystemService(Context.CLIPBOARD_SERVICE) as ClipboardManager).setPrimaryClip(ClipData.newPlainText("Gusan", text))
    Toast.makeText(context, "Copied", Toast.LENGTH_SHORT).show()
}

fun shareText(context: Context, subject: String, text: String) {
    val send = Intent(Intent.ACTION_SEND).setType("text/plain").putExtra(Intent.EXTRA_SUBJECT, subject).putExtra(Intent.EXTRA_TEXT, text)
    context.startActivity(Intent.createChooser(send, null))
}

@Composable
fun MeetingDetailScreen(id: String, onBack: () -> Unit) {
    val context = LocalContext.current
    val graph = context.graph
    val meetings by graph.meetings.meetings.collectAsStateWithLifecycle()
    val workMap by graph.meetings.work.collectAsStateWithLifecycle()
    val recording by graph.meetings.recording.collectAsStateWithLifecycle()
    val meeting = meetings.firstOrNull { it.id == id }
    val work = workMap[id]
    var tab by rememberSaveable { mutableIntStateOf(0) }
    var renaming by remember { mutableStateOf(false) }
    var deleting by remember { mutableStateOf(false) }

    if (meeting == null) {
        Column(Modifier.fillMaxSize().padding(20.dp)) {
            IconButton(onClick = onBack) { Icon(Icons.AutoMirrored.Filled.ArrowBack, contentDescription = "Back") }
            Text("This meeting is no longer on this phone.", style = MaterialTheme.typography.bodyLarge)
        }
        return
    }
    val isRecording = recording?.id == id
    val shown = if (tab == 0) meeting.notes else meeting.transcript
    val shareable = if (tab == 0) NotesMarkdown.plain(meeting.notes) else meeting.transcript

    Column(Modifier.fillMaxSize()) {
        Row(Modifier.fillMaxWidth().padding(horizontal = 8.dp, vertical = 4.dp), verticalAlignment = Alignment.CenterVertically) {
            IconButton(onClick = onBack) { Icon(Icons.AutoMirrored.Filled.ArrowBack, contentDescription = "Back") }
            Spacer(Modifier.weight(1f))
            IconButton(onClick = { copyText(context, shareable) }, enabled = shown.isNotBlank()) {
                Icon(Icons.Filled.ContentCopy, contentDescription = "Copy")
            }
            IconButton(onClick = { shareText(context, meeting.title, meeting.title + "\n\n" + shareable) }, enabled = shown.isNotBlank()) {
                Icon(Icons.Filled.Share, contentDescription = "Share")
            }
            IconButton(onClick = { deleting = true }, enabled = !isRecording) { Icon(Icons.Filled.Delete, contentDescription = "Delete") }
        }
        Column(Modifier.padding(horizontal = 20.dp), verticalArrangement = Arrangement.spacedBy(6.dp)) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Text(meeting.title, Modifier.weight(1f), style = MaterialTheme.typography.headlineSmall, fontWeight = FontWeight.Bold)
                IconButton(onClick = { renaming = true }) { Icon(Icons.Filled.Edit, contentDescription = "Rename") }
            }
            Text(
                android.text.format.DateFormat.getMediumDateFormat(context).format(meeting.createdAtMs) + ", " +
                    android.text.format.DateFormat.getTimeFormat(context).format(meeting.createdAtMs) + " · " + clock(meeting.durationMs / 1000),
                style = MaterialTheme.typography.bodyMedium,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
            StatusArea(meeting, work, isRecording, context)
        }
        TabRow(selectedTabIndex = tab, containerColor = Color.Transparent, modifier = Modifier.padding(top = 8.dp)) {
            Tab(selected = tab == 0, onClick = { tab = 0 }, text = { Text("Notes") })
            Tab(selected = tab == 1, onClick = { tab = 1 }, text = { Text("Transcript") })
        }
        SelectionContainer(Modifier.weight(1f)) {
            Column(
                Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(horizontal = 20.dp, vertical = 16.dp),
                verticalArrangement = Arrangement.spacedBy(8.dp),
            ) {
                when {
                    shown.isBlank() -> Text(
                        if (tab == 0) "The notes will show here once they are written." else "The transcript will show here once it is written.",
                        style = MaterialTheme.typography.bodyLarge,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                    tab == 0 -> Notes(meeting.notes)
                    else -> meeting.transcript.split("\n\n").forEach { Text(it, style = MaterialTheme.typography.bodyLarge) }
                }
            }
        }
    }

    if (renaming) {
        var name by remember { mutableStateOf(meeting.title) }
        AlertDialog(
            onDismissRequest = { renaming = false },
            title = { Text("Rename meeting") },
            text = { OutlinedTextField(value = name, onValueChange = { name = it }, singleLine = true) },
            confirmButton = {
                TextButton(onClick = { graph.meetings.rename(id, name); renaming = false }, enabled = name.isNotBlank()) { Text("Save") }
            },
            dismissButton = { TextButton(onClick = { renaming = false }) { Text("Cancel") } },
        )
    }
    if (deleting) {
        AlertDialog(
            onDismissRequest = { deleting = false },
            title = { Text("Delete this meeting?") },
            text = { Text("The recording, the transcript and the notes are removed from this phone. This can't be undone.") },
            confirmButton = {
                TextButton(onClick = {
                    deleting = false
                    MeetingService.cancel(context, id)
                    graph.meetings.delete(id)
                    onBack()
                }) { Text("Delete", color = MaterialTheme.colorScheme.error) }
            },
            dismissButton = { TextButton(onClick = { deleting = false }) { Text("Cancel") } },
        )
    }
}

@Composable
private fun StatusArea(meeting: Meeting, work: com.aram2k.gusan.meetings.Work?, isRecording: Boolean, context: Context) {
    when {
        isRecording -> OutlinedButton(onClick = { MeetingService.stopRecording(context) }) { Text("Stop and write notes") }
        work != null || meeting.busy -> {
            Text(statusText(meeting, work), style = MaterialTheme.typography.bodyMedium, color = MaterialTheme.colorScheme.primary)
            if (work != null && work.total > 1) {
                LinearProgressIndicator(progress = { work.done / work.total.toFloat() }, modifier = Modifier.fillMaxWidth())
            } else {
                LinearProgressIndicator(Modifier.fillMaxWidth())
            }
            TextButton(onClick = { MeetingService.cancel(context, meeting.id) }) { Text("Stop for now") }
        }
        meeting.status != Meeting.Status.DONE -> {
            if (meeting.error.isNotEmpty()) {
                Text(meeting.error, style = MaterialTheme.typography.bodyMedium, color = MaterialTheme.colorScheme.error)
            }
            Row(verticalAlignment = Alignment.CenterVertically) {
                OutlinedButton(onClick = { MeetingService.process(context, meeting.id) }) {
                    Text(if (meeting.status == Meeting.Status.FAILED) "Try again" else "Write the notes")
                }
                if (meeting.error.contains("Pro")) {
                    Spacer(Modifier.width(8.dp))
                    TextButton(onClick = { openPricing(context) }) { Text("See plans") }
                }
            }
        }
    }
}

private fun styled(spans: List<Span>): AnnotatedString = buildAnnotatedString {
    spans.forEach { span ->
        if (span.bold) withStyle(SpanStyle(fontWeight = FontWeight.Bold)) { append(span.text) } else append(span.text)
    }
}

@Composable
private fun Notes(markdown: String) {
    NotesMarkdown.parse(markdown).forEach { block ->
        when (block) {
            is NoteBlock.Heading -> Text(
                styled(block.text),
                style = if (block.level <= 2) MaterialTheme.typography.titleLarge else MaterialTheme.typography.titleMedium,
                fontWeight = FontWeight.Bold,
                modifier = Modifier.padding(top = if (block.level <= 2) 12.dp else 6.dp),
            )
            is NoteBlock.Paragraph -> Text(styled(block.text), style = MaterialTheme.typography.bodyLarge)
            is NoteBlock.Bullet -> Row(Modifier.padding(start = if (block.indent > 0) 24.dp else 0.dp)) {
                when (block.checked) {
                    null -> Text("•", Modifier.width(20.dp), style = MaterialTheme.typography.bodyLarge)
                    else -> Icon(
                        if (block.checked == true) Icons.Filled.CheckBox else Icons.Filled.CheckBoxOutlineBlank,
                        contentDescription = if (block.checked == true) "Done" else "To do",
                        modifier = Modifier.padding(end = 8.dp, top = 2.dp).size(20.dp),
                        tint = MaterialTheme.colorScheme.primary,
                    )
                }
                Text(styled(block.text), Modifier.weight(1f), style = MaterialTheme.typography.bodyLarge)
            }
        }
    }
}
