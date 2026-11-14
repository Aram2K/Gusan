package com.aram2k.gusan.ui

import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.selection.selectable
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.RadioButton
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import com.aram2k.gusan.BuildConfig
import com.aram2k.gusan.core.Backend
import com.aram2k.gusan.core.SpeechLanguages
import com.aram2k.gusan.graph

@Composable
fun SettingsScreen(onSignIn: () -> Unit) {
    val context = LocalContext.current
    val graph = context.graph
    val settings = graph.settings
    val account by graph.account.state.collectAsStateWithLifecycle()
    val readiness = rememberReadiness()
    var language by remember { mutableStateOf(settings.language) }
    var haptics by remember { mutableStateOf(settings.haptics) }
    var keySounds by remember { mutableStateOf(settings.keySounds) }
    var sounds by remember { mutableStateOf(settings.sounds) }
    var autoCapitalize by remember { mutableStateOf(settings.autoCapitalize) }
    var doubleSpace by remember { mutableStateOf(settings.doubleSpacePeriod) }
    var saveHistory by remember { mutableStateOf(settings.saveHistory) }
    var pickingLanguage by remember { mutableStateOf(false) }
    var signingOut by remember { mutableStateOf(false) }

    Column(
        Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(horizontal = 20.dp, vertical = 16.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp),
    ) {
        ScreenTitle("Settings")

        SectionLabel("Account")
        AppCard {
            if (account.signedIn) {
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Text(account.email.ifEmpty { "Signed in" }, Modifier.weight(1f), style = MaterialTheme.typography.bodyLarge)
                    PlanBadge(account.isPro)
                }
                HorizontalDivider()
                LinkRow(if (account.isPro) "Manage plan" else "See Gusan Pro") { openPricing(context) }
                LinkRow("Sign out") { signingOut = true }
            } else {
                Text("You're not signed in.", style = MaterialTheme.typography.bodyLarge)
                LinkRow("Sign in", onSignIn)
            }
        }

        SectionLabel("Voice")
        AppCard {
            LinkRow("Language you speak", SpeechLanguages.all.first { it.first == language }.second) { pickingLanguage = true }
            ToggleRow("Sound when listening starts", sounds) { sounds = it; settings.sounds = it }
        }

        SectionLabel("Keyboard")
        AppCard {
            ToggleRow("Vibrate on key press", haptics) { haptics = it; settings.haptics = it }
            ToggleRow("Sound on key press", keySounds) { keySounds = it; settings.keySounds = it }
            ToggleRow("Capital letter at the start of a sentence", autoCapitalize) { autoCapitalize = it; settings.autoCapitalize = it }
            ToggleRow("Two spaces make a full stop", doubleSpace) { doubleSpace = it; settings.doubleSpacePeriod = it }
            HorizontalDivider()
            LinkRow("Turn the keyboard on or off", if (readiness.keyboardOn) "On" else "Off") { openKeyboardSettings(context) }
            LinkRow("Choose keyboard", if (readiness.keyboardChosen) "Gusan" else "Another keyboard") { showKeyboardPicker(context) }
        }

        SectionLabel("Privacy")
        AppCard {
            ToggleRow("Keep what Gusan writes in Sessions", saveHistory) { saveHistory = it; settings.saveHistory = it }
            Text(
                "Your voice goes to Gusan Cloud only when you tap the mic or stop a meeting recording. What you type is never sent. " +
                    "Recordings, transcripts, notes and Sessions stay on this phone.",
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
        }

        SectionLabel("About")
        AppCard {
            LinkRow("Website", "gusan.aibuben.xyz") { openUrl(context, Backend.WEBSITE) }
            Text("Gusan for Android ${BuildConfig.VERSION_NAME}", style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
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
                                language = code
                                settings.language = code
                                pickingLanguage = false
                            }.padding(vertical = 4.dp),
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
            text = { Text("The keyboard keeps typing, but voice and meeting notes need you signed in. Your meetings stay on this phone.") },
            confirmButton = {
                TextButton(onClick = { signingOut = false; graph.account.signOut() }) { Text("Sign out", color = MaterialTheme.colorScheme.error) }
            },
            dismissButton = { TextButton(onClick = { signingOut = false }) { Text("Cancel") } },
        )
    }
}

@Composable
private fun LinkRow(title: String, onClick: () -> Unit) = LinkRow(title, "", onClick)

@Composable
private fun LinkRow(title: String, value: String, onClick: () -> Unit) {
    Row(Modifier.fillMaxWidth().clickable(onClick = onClick).padding(vertical = 10.dp), verticalAlignment = Alignment.CenterVertically) {
        Text(title, Modifier.weight(1f), style = MaterialTheme.typography.bodyLarge)
        if (value.isNotEmpty()) Text(value, style = MaterialTheme.typography.bodyMedium, color = MaterialTheme.colorScheme.onSurfaceVariant)
        Text("  ›", style = MaterialTheme.typography.titleMedium, color = MaterialTheme.colorScheme.onSurfaceVariant)
    }
}

@Composable
private fun ToggleRow(title: String, checked: Boolean, onChange: (Boolean) -> Unit) {
    Row(
        Modifier.fillMaxWidth().clickable(role = Role.Switch) { onChange(!checked) }.padding(vertical = 2.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Text(title, Modifier.weight(1f).padding(end = 12.dp), style = MaterialTheme.typography.bodyLarge)
        Switch(checked = checked, onCheckedChange = null)
    }
}
