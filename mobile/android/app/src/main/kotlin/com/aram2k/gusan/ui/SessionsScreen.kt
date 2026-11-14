package com.aram2k.gusan.ui

import android.text.format.DateUtils
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Delete
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.LifecycleResumeEffect
import com.aram2k.gusan.core.SmartEntry
import com.aram2k.gusan.graph
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

/** What Gusan wrote from the keyboard, newest first. Tap one to copy it. */
@Composable
fun SessionsScreen() {
    val context = LocalContext.current
    val history = context.graph.history
    val scope = rememberCoroutineScope()
    var entries by remember { mutableStateOf<List<SmartEntry>>(emptyList()) }
    var loaded by remember { mutableStateOf(false) }
    var clearing by remember { mutableStateOf(false) }

    suspend fun load() {
        entries = withContext(Dispatchers.IO) { history.list() }
        loaded = true
    }
    LaunchedEffect(Unit) { load() }
    // The keyboard may have added something while another app was in front.
    LifecycleResumeEffect(Unit) {
        scope.launch { load() }
        onPauseOrDispose { }
    }

    LazyColumn(
        Modifier.fillMaxSize(),
        contentPadding = PaddingValues(horizontal = 20.dp, vertical = 16.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp),
    ) {
        item {
            Row(verticalAlignment = Alignment.CenterVertically) {
                ScreenTitle("Sessions", Modifier.weight(1f))
                if (entries.isNotEmpty()) TextButton(onClick = { clearing = true }) { Text("Clear all") }
            }
        }
        if (loaded && entries.isEmpty()) item {
            Text(
                "What Gusan writes for you from the keyboard shows up here, so nothing is lost. It stays on this phone.",
                style = MaterialTheme.typography.bodyLarge,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
                modifier = Modifier.padding(top = 24.dp),
            )
        }
        items(entries, key = { it.id }) { entry ->
            AppCard(Modifier.clip(RoundedCornerShape(16.dp)).clickable { copyText(context, entry.result) }) {
                Text(entry.result, style = MaterialTheme.typography.bodyLarge, maxLines = 6, overflow = TextOverflow.Ellipsis)
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Text(
                        "You said: " + entry.heard,
                        Modifier.weight(1f),
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                        maxLines = 2,
                        overflow = TextOverflow.Ellipsis,
                    )
                    IconButton(onClick = {
                        scope.launch {
                            withContext(Dispatchers.IO) { history.remove(entry.id) }
                            load()
                        }
                    }) { Icon(Icons.Filled.Delete, contentDescription = "Delete", tint = MaterialTheme.colorScheme.onSurfaceVariant) }
                }
                Text(
                    DateUtils.getRelativeTimeSpanString(entry.atMs, System.currentTimeMillis(), DateUtils.MINUTE_IN_MILLIS).toString(),
                    style = MaterialTheme.typography.labelSmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
        }
    }

    if (clearing) {
        AlertDialog(
            onDismissRequest = { clearing = false },
            title = { Text("Clear all sessions?") },
            text = { Text("Everything in this list is removed from this phone. This can't be undone.") },
            confirmButton = {
                TextButton(onClick = {
                    clearing = false
                    scope.launch {
                        withContext(Dispatchers.IO) { history.clear() }
                        load()
                    }
                }) { Text("Clear", color = MaterialTheme.colorScheme.error) }
            },
            dismissButton = { TextButton(onClick = { clearing = false }) { Text("Cancel") } },
        )
    }
}
