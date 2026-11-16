package com.aram2k.gusan.ui

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
import androidx.compose.foundation.layout.navigationBarsPadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.statusBarsPadding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.selection.selectable
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material.icons.automirrored.filled.KeyboardArrowRight
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.RadioButton
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import com.aram2k.gusan.BuildConfig
import com.aram2k.gusan.core.Backend
import com.aram2k.gusan.core.SpeechLanguages
import com.aram2k.gusan.graph
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

@Composable
fun SettingsScreen(onBack: () -> Unit, onSignIn: () -> Unit) {
    val context = LocalContext.current
    val graph = context.graph
    val settings = graph.settings
    val account by graph.account.state.collectAsStateWithLifecycle()
    val readiness = rememberReadiness()
    val scope = rememberCoroutineScope()
    var language by remember { mutableStateOf(settings.language) }
    var haptics by remember { mutableStateOf(settings.haptics) }
    var keySounds by remember { mutableStateOf(settings.keySounds) }
    var sounds by remember { mutableStateOf(settings.sounds) }
    var autoCapitalize by remember { mutableStateOf(settings.autoCapitalize) }
    var doubleSpace by remember { mutableStateOf(settings.doubleSpacePeriod) }
    var saveHistory by remember { mutableStateOf(settings.saveHistory) }
    var pickingLanguage by remember { mutableStateOf(false) }
    var signingOut by remember { mutableStateOf(false) }
    var clearing by remember { mutableStateOf(false) }

    Column(Modifier.fillMaxSize().statusBarsPadding().verticalScroll(rememberScrollState()).navigationBarsPadding().padding(bottom = 32.dp)) {
        Row(Modifier.fillMaxWidth().padding(4.dp), verticalAlignment = Alignment.CenterVertically) {
            IconButton(onClick = onBack) { Icon(Icons.AutoMirrored.Filled.ArrowBack, contentDescription = "Back") }
            Text("Settings", style = MaterialTheme.typography.titleLarge)
        }
        Column(Modifier.padding(horizontal = 20.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
            Panel {
                if (account.signedIn) {
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        Box(Modifier.size(44.dp).clip(CircleShape).background(MaterialTheme.colorScheme.primary), contentAlignment = Alignment.Center) {
                            Text(account.email.take(1).uppercase().ifEmpty { "G" }, style = MaterialTheme.typography.titleLarge, color = MaterialTheme.colorScheme.onPrimary)
                        }
                        Spacer(Modifier.width(12.dp))
                        Column(Modifier.weight(1f)) {
                            Text(account.email.ifEmpty { "Signed in" }, style = MaterialTheme.typography.titleMedium)
                            Text(if (account.isPro) "Gusan Pro" else "Free plan", style = MaterialTheme.typography.bodyMedium, color = MaterialTheme.colorScheme.onSurfaceVariant)
                        }
                        PlanBadge(account.isPro)
                    }
                    Row(Modifier.clickable { openPricing(context) }.padding(vertical = 8.dp)) {
                        Text(if (account.isPro) "Manage your plan" else "Get Gusan Pro: voice and meeting notes", color = MaterialTheme.colorScheme.primary, style = MaterialTheme.typography.titleSmall)
                    }
                } else {
                    Text("You're not signed in", style = MaterialTheme.typography.titleMedium)
                    BigButton("Sign in", onClick = onSignIn)
                }
            }

            Group("Voice")
            Panel(padding = PaddingValues(horizontal = 16.dp, vertical = 4.dp)) {
                LinkRow("Language you speak", SpeechLanguages.all.first { it.first == language }.second) { pickingLanguage = true }
                HorizontalDivider(color = MaterialTheme.colorScheme.outlineVariant)
                ToggleRow("Sound when Gusan starts listening", sounds) { sounds = it; settings.sounds = it }
            }

            Group("Keyboard")
            Panel(padding = PaddingValues(horizontal = 16.dp, vertical = 4.dp)) {
                LinkRow("Gusan keyboard", if (readiness.keyboardReady) "In use" else if (readiness.keyboardOn) "On, not in use" else "Off") {
                    if (readiness.keyboardOn) showKeyboardPicker(context) else openKeyboardSettings(context)
                }
                HorizontalDivider(color = MaterialTheme.colorScheme.outlineVariant)
                ToggleRow("Vibrate on key press", haptics) { haptics = it; settings.haptics = it }
                ToggleRow("Sound on key press", keySounds) { keySounds = it; settings.keySounds = it }
                ToggleRow("Capitalize sentences", autoCapitalize) { autoCapitalize = it; settings.autoCapitalize = it }
                ToggleRow("Double space types a period", doubleSpace) { doubleSpace = it; settings.doubleSpacePeriod = it }
            }

            Group("Privacy")
            Panel(padding = PaddingValues(horizontal = 16.dp, vertical = 4.dp)) {
                ToggleRow("Keep dictations on this phone", saveHistory) { saveHistory = it; settings.saveHistory = it }
                HorizontalDivider(color = MaterialTheme.colorScheme.outlineVariant)
                LinkRow("Clear dictations", "") { clearing = true }
            }
            Text(
                "Gusan never sends what you type. Your voice goes to Gusan Cloud only after you tap the mic or stop a recording. Recordings and notes are kept on this phone and aren't backed up.",
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
                modifier = Modifier.padding(horizontal = 4.dp),
            )

            Group("About")
            Panel(padding = PaddingValues(horizontal = 16.dp, vertical = 4.dp)) {
                LinkRow("Gusan on your computer", "gusan.aibuben.xyz") { openUrl(context, Backend.WEBSITE) }
                HorizontalDivider(color = MaterialTheme.colorScheme.outlineVariant)
                LinkRow("Version", BuildConfig.VERSION_NAME, chevron = false) {}
            }
            if (account.signedIn) {
                TextButton(onClick = { signingOut = true }, modifier = Modifier.fillMaxWidth().padding(top = 8.dp)) {
                    Text("Sign out", color = MaterialTheme.colorScheme.error, style = MaterialTheme.typography.titleMedium)
                }
            }
        }
    }

    if (pickingLanguage) {
        AlertDialog(
            onDismissRequest = { pickingLanguage = false },
            title = { Text("Language you speak") },
            text = {
                Column(Modifier.verticalScroll(rememberScrollState())) {
                    SpeechLanguages.all.forEach { (code, name) ->
                        Row(
                            Modifier.fillMaxWidth().selectable(selected = code == language, role = Role.RadioButton) {
                                language = code; settings.language = code; pickingLanguage = false
                            }.padding(vertical = 6.dp),
                            verticalAlignment = Alignment.CenterVertically,
                        ) {
                            RadioButton(selected = code == language, onClick = null)
                            Text(name, Modifier.padding(start = 12.dp), style = MaterialTheme.typography.bodyLarge)
                        }
                    }
                }
            },
            confirmButton = { TextButton(onClick = { pickingLanguage = false }) { Text("Close") } },
        )
    }
    if (signingOut) {
        AlertDialog(
            onDismissRequest = { signingOut = false },
            title = { Text("Sign out?") },
            text = { Text("Typing keeps working. Voice and meeting notes need you signed in. Your meetings stay on this phone.") },
            confirmButton = { TextButton(onClick = { signingOut = false; graph.account.signOut() }) { Text("Sign out", color = MaterialTheme.colorScheme.error) } },
            dismissButton = { TextButton(onClick = { signingOut = false }) { Text("Cancel") } },
        )
    }
    if (clearing) {
        AlertDialog(
            onDismissRequest = { clearing = false },
            title = { Text("Clear all dictations?") },
            text = { Text("Everything Gusan wrote from the keyboard is removed from this phone.") },
            confirmButton = {
                TextButton(onClick = { clearing = false; scope.launch { withContext(Dispatchers.IO) { graph.history.clear() } } }) {
                    Text("Clear", color = MaterialTheme.colorScheme.error)
                }
            },
            dismissButton = { TextButton(onClick = { clearing = false }) { Text("Cancel") } },
        )
    }
}

@Composable
private fun Group(title: String) {
    Text(title, style = MaterialTheme.typography.labelLarge, color = MaterialTheme.colorScheme.onSurfaceVariant, modifier = Modifier.padding(start = 4.dp, top = 16.dp))
}

@Composable
private fun LinkRow(title: String, value: String, chevron: Boolean = true, onClick: () -> Unit) {
    Row(Modifier.fillMaxWidth().clickable(onClick = onClick).padding(vertical = 14.dp), verticalAlignment = Alignment.CenterVertically) {
        Text(title, Modifier.weight(1f), style = MaterialTheme.typography.bodyLarge)
        if (value.isNotEmpty()) Text(value, style = MaterialTheme.typography.bodyMedium, color = MaterialTheme.colorScheme.onSurfaceVariant)
        if (chevron) Icon(Icons.AutoMirrored.Filled.KeyboardArrowRight, contentDescription = null, tint = MaterialTheme.colorScheme.onSurfaceVariant)
    }
}

@Composable
private fun ToggleRow(title: String, checked: Boolean, onChange: (Boolean) -> Unit) {
    Row(Modifier.fillMaxWidth().clickable(role = Role.Switch) { onChange(!checked) }.padding(vertical = 8.dp), verticalAlignment = Alignment.CenterVertically) {
        Text(title, Modifier.weight(1f).padding(end = 12.dp), style = MaterialTheme.typography.bodyLarge)
        Switch(checked = checked, onCheckedChange = null)
    }
}
