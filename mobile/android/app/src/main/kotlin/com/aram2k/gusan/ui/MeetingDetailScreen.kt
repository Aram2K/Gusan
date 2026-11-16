package com.aram2k.gusan.ui

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.navigationBarsPadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.statusBarsPadding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.selection.SelectionContainer
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material.icons.filled.CheckBox
import androidx.compose.material.icons.filled.CheckBoxOutlineBlank
import androidx.compose.material.icons.filled.ContentCopy
import androidx.compose.material.icons.filled.Delete
import androidx.compose.material.icons.filled.Edit
import androidx.compose.material.icons.filled.MoreVert
import androidx.compose.material.icons.filled.Share
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.DropdownMenu
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.SegmentedButton
import androidx.compose.material3.SegmentedButtonDefaults
import androidx.compose.material3.SingleChoiceSegmentedButtonRow
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
import androidx.compose.ui.draw.clip
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
import com.aram2k.gusan.meetings.Work

@Composable
fun MeetingDetailScreen(id: String, onBack: () -> Unit) {
    val context = LocalContext.current
    val graph = context.graph
    val meetings by graph.meetings.meetings.collectAsStateWithLifecycle()
    val workMap by graph.meetings.work.collectAsStateWithLifecycle()
    val meeting = meetings.firstOrNull { it.id == id }
    var tab by rememberSaveable { mutableIntStateOf(0) }
    var menu by remember { mutableStateOf(false) }
    var renaming by remember { mutableStateOf(false) }
    var deleting by remember { mutableStateOf(false) }

    Column(Modifier.fillMaxSize().statusBarsPadding()) {
        Row(Modifier.fillMaxWidth().padding(horizontal = 4.dp, vertical = 4.dp), verticalAlignment = Alignment.CenterVertically) {
            IconButton(onClick = onBack) { Icon(Icons.AutoMirrored.Filled.ArrowBack, contentDescription = "Back") }
            Spacer(Modifier.weight(1f))
            if (meeting != null) {
                val text = if (tab == 0) NotesMarkdown.plain(meeting.notes) else meeting.transcript
                IconButton(onClick = { shareText(context, meeting.title, meeting.title + "\n\n" + text) }, enabled = text.isNotBlank()) {
                    Icon(Icons.Filled.Share, contentDescription = "Share")
                }
                Box {
                    IconButton(onClick = { menu = true }) { Icon(Icons.Filled.MoreVert, contentDescription = "More") }
                    DropdownMenu(expanded = menu, onDismissRequest = { menu = false }) {
                        DropdownMenuItem(
                            text = { Text(if (tab == 0) "Copy notes" else "Copy transcript") },
                            leadingIcon = { Icon(Icons.Filled.ContentCopy, null) },
                            enabled = text.isNotBlank(),
                            onClick = { menu = false; copyText(context, text) },
                        )
                        DropdownMenuItem(text = { Text("Rename") }, leadingIcon = { Icon(Icons.Filled.Edit, null) }, onClick = { menu = false; renaming = true })
                        DropdownMenuItem(
                            text = { Text("Delete", color = MaterialTheme.colorScheme.error) },
                            leadingIcon = { Icon(Icons.Filled.Delete, null, tint = MaterialTheme.colorScheme.error) },
                            onClick = { menu = false; deleting = true },
                        )
                    }
                }
            }
        }
        if (meeting == null) {
            Text("This meeting is no longer on this phone.", style = MaterialTheme.typography.bodyLarge, modifier = Modifier.padding(24.dp))
            return@Column
        }
        Column(Modifier.padding(horizontal = 20.dp), verticalArrangement = Arrangement.spacedBy(4.dp)) {
            Text(meeting.title, style = MaterialTheme.typography.headlineMedium)
            Text(
                android.text.format.DateFormat.getMediumDateFormat(context).format(meeting.createdAtMs) + ", " +
                    android.text.format.DateFormat.getTimeFormat(context).format(meeting.createdAtMs) + " · " + clock(meeting.durationMs / 1000),
                style = MaterialTheme.typography.bodyMedium,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
        }
        Progress(meeting, workMap[id])
        if (meeting.notes.isNotBlank() || meeting.transcript.isNotBlank()) {
            SingleChoiceSegmentedButtonRow(Modifier.fillMaxWidth().padding(horizontal = 20.dp, vertical = 12.dp)) {
                listOf("Notes", "Transcript").forEachIndexed { i, label ->
                    SegmentedButton(selected = tab == i, onClick = { tab = i }, shape = SegmentedButtonDefaults.itemShape(i, 2), icon = {}) { Text(label) }
                }
            }
        }
        SelectionContainer(Modifier.weight(1f)) {
            Column(
                Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(horizontal = 20.dp).navigationBarsPadding().padding(bottom = 24.dp),
                verticalArrangement = Arrangement.spacedBy(8.dp),
            ) {
                when {
                    tab == 0 && meeting.notes.isNotBlank() -> Notes(meeting.notes)
                    tab == 1 && meeting.transcript.isNotBlank() -> meeting.transcript.split("\n\n").forEach {
                        Text(it, style = MaterialTheme.typography.bodyLarge, modifier = Modifier.padding(bottom = 8.dp))
                    }
                    tab == 0 && meeting.transcript.isNotBlank() -> Text(
                        "The notes appear here once they're written.",
                        style = MaterialTheme.typography.bodyLarge,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                }
            }
        }
    }

    if (renaming && meeting != null) {
        var name by remember { mutableStateOf(meeting.title) }
        AlertDialog(
            onDismissRequest = { renaming = false },
            title = { Text("Rename") },
            text = { OutlinedTextField(value = name, onValueChange = { name = it }, singleLine = true, shape = RoundedCornerShape(12.dp)) },
            confirmButton = { TextButton(onClick = { graph.meetings.rename(id, name); renaming = false }, enabled = name.isNotBlank()) { Text("Save") } },
            dismissButton = { TextButton(onClick = { renaming = false }) { Text("Cancel") } },
        )
    }
    if (deleting) {
        AlertDialog(
            onDismissRequest = { deleting = false },
            title = { Text("Delete this meeting?") },
            text = { Text("The recording, transcript and notes are removed from this phone. This can't be undone.") },
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

/** Where the transcript and notes are, and the next step when they aren't there. */
@Composable
private fun Progress(meeting: Meeting, work: Work?) {
    val context = LocalContext.current
    val busy = work != null || meeting.busy
    if (meeting.status == Meeting.Status.DONE && !busy) return
    Panel(Modifier.padding(start = 20.dp, end = 20.dp, top = 16.dp)) {
        when {
            busy -> {
                Text(statusText(meeting, work) + "…", style = MaterialTheme.typography.titleMedium)
                if (work != null && work.total > 1) {
                    LinearProgressIndicator(progress = { work.done / work.total.toFloat() }, modifier = Modifier.fillMaxWidth().clip(RoundedCornerShape(4.dp)))
                } else {
                    LinearProgressIndicator(Modifier.fillMaxWidth().clip(RoundedCornerShape(4.dp)))
                }
                Text(
                    "You can leave the app. We'll let you know when the notes are ready.",
                    style = MaterialTheme.typography.bodyMedium,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
                TextButton(onClick = { MeetingService.cancel(context, meeting.id) }) { Text("Stop for now") }
            }
            meeting.status == Meeting.Status.RECORDING -> Text("Recording…", style = MaterialTheme.typography.titleMedium)
            else -> {
                if (meeting.error.isNotEmpty()) Text(meeting.error, style = MaterialTheme.typography.bodyLarge)
                else Text("This recording has no notes yet.", style = MaterialTheme.typography.bodyLarge)
                Row(verticalAlignment = Alignment.CenterVertically) {
                    BigButton(
                        if (meeting.status == Meeting.Status.FAILED) "Try again" else "Write the notes",
                        modifier = Modifier.weight(1f),
                        onClick = { MeetingService.process(context, meeting.id) },
                    )
                    if (meeting.error.contains("Pro")) {
                        Spacer(Modifier.width(8.dp))
                        TextButton(onClick = { openPricing(context) }) { Text("Get Pro") }
                    }
                }
            }
        }
    }
}

private fun styled(spans: List<Span>): AnnotatedString = buildAnnotatedString {
    spans.forEach { span -> if (span.bold) withStyle(SpanStyle(fontWeight = FontWeight.SemiBold)) { append(span.text) } else append(span.text) }
}

@Composable
private fun Notes(markdown: String) {
    NotesMarkdown.parse(markdown).forEach { block ->
        when (block) {
            is NoteBlock.Heading -> Text(
                styled(block.text),
                style = if (block.level <= 2) MaterialTheme.typography.titleLarge else MaterialTheme.typography.titleMedium,
                color = if (block.level <= 2) MaterialTheme.colorScheme.onSurface else MaterialTheme.colorScheme.primary,
                modifier = Modifier.padding(top = if (block.level <= 2) 16.dp else 8.dp),
            )
            is NoteBlock.Paragraph -> Text(styled(block.text), style = MaterialTheme.typography.bodyLarge)
            is NoteBlock.Bullet -> Row(Modifier.padding(start = if (block.indent > 0) 24.dp else 0.dp)) {
                when (block.checked) {
                    null -> Box(Modifier.padding(top = 10.dp, end = 12.dp).size(6.dp).clip(RoundedCornerShape(3.dp)).background(MaterialTheme.colorScheme.onSurfaceVariant))
                    else -> Icon(
                        if (block.checked == true) Icons.Filled.CheckBox else Icons.Filled.CheckBoxOutlineBlank,
                        contentDescription = if (block.checked == true) "Done" else "To do",
                        modifier = Modifier.padding(end = 10.dp, top = 2.dp).size(20.dp),
                        tint = MaterialTheme.colorScheme.primary,
                    )
                }
                Text(styled(block.text), Modifier.weight(1f), style = MaterialTheme.typography.bodyLarge)
            }
        }
    }
}
