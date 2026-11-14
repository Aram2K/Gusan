package com.aram2k.gusan.ui

import android.Manifest
import android.content.Context
import android.content.Intent
import android.os.Build
import android.provider.Settings
import android.view.inputmethod.InputMethodManager
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.Image
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.imePadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.CheckCircle
import androidx.compose.material.icons.filled.RadioButtonUnchecked
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import com.aram2k.gusan.R
import com.aram2k.gusan.audio.MicRecorder
import com.aram2k.gusan.graph
import kotlinx.coroutines.delay

/** What the phone still needs before the keyboard can be used. */
data class Readiness(val keyboardOn: Boolean, val keyboardChosen: Boolean, val microphone: Boolean) {
    companion object {
        fun of(context: Context): Readiness {
            val imm = context.getSystemService(Context.INPUT_METHOD_SERVICE) as InputMethodManager
            val on = imm.enabledInputMethodList.any { it.packageName == context.packageName }
            val current = Settings.Secure.getString(context.contentResolver, Settings.Secure.DEFAULT_INPUT_METHOD).orEmpty()
            return Readiness(on, on && current.startsWith(context.packageName + "/"), MicRecorder.hasPermission(context))
        }
    }
}

/** Keeps an eye on the setup while a screen is showing: the person changes it in the system's own pages. */
@Composable
fun rememberReadiness(): Readiness {
    val context = LocalContext.current
    var readiness by remember { mutableStateOf(Readiness.of(context)) }
    LaunchedEffect(Unit) {
        while (true) {
            readiness = Readiness.of(context)
            delay(1000)
        }
    }
    return readiness
}

fun openKeyboardSettings(context: Context) {
    context.startActivity(Intent(Settings.ACTION_INPUT_METHOD_SETTINGS).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
}

fun showKeyboardPicker(context: Context) {
    (context.getSystemService(Context.INPUT_METHOD_SERVICE) as InputMethodManager).showInputMethodPicker()
}

/** The permissions voice needs: the microphone, and on Android 13 and later the notification that shows a recording. */
fun voicePermissions(): Array<String> =
    if (Build.VERSION.SDK_INT >= 33) arrayOf(Manifest.permission.RECORD_AUDIO, Manifest.permission.POST_NOTIFICATIONS)
    else arrayOf(Manifest.permission.RECORD_AUDIO)

@Composable
fun HomeScreen(onSignIn: () -> Unit, onMeetings: () -> Unit) {
    val context = LocalContext.current
    val account by context.graph.account.state.collectAsStateWithLifecycle()
    val readiness = rememberReadiness()
    val askMic = rememberLauncherForActivityResult(ActivityResultContracts.RequestMultiplePermissions()) {}
    var sample by rememberSaveable { mutableStateOf("") }
    val ready = account.signedIn && readiness.keyboardOn && readiness.keyboardChosen && readiness.microphone

    Column(
        Modifier.fillMaxSize().verticalScroll(rememberScrollState()).imePadding().padding(horizontal = 20.dp, vertical = 16.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp),
    ) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            Image(painterResource(R.drawable.gusan_logo), contentDescription = null, modifier = Modifier.size(44.dp))
            Spacer(Modifier.width(12.dp))
            Column(Modifier.weight(1f)) {
                ScreenTitle("Gusan")
                Text(
                    if (account.signedIn) account.email else "Say it. Gusan writes it.",
                    style = MaterialTheme.typography.bodyMedium,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
            if (account.signedIn) PlanBadge(account.isPro)
        }

        AppCard {
            Text(if (ready) "Gusan is ready" else "Finish setting up", style = MaterialTheme.typography.titleMedium, fontWeight = FontWeight.SemiBold)
            if (ready) {
                Text(
                    "In any app, open the keyboard, tap the mic and say what you want. The finished text lands at the cursor.",
                    style = MaterialTheme.typography.bodyMedium,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            } else {
                SetupStep("Sign in", account.signedIn, "Sign in", onSignIn)
                SetupStep("Turn on the Gusan keyboard", readiness.keyboardOn, "Open settings") { openKeyboardSettings(context) }
                SetupStep("Choose Gusan as your keyboard", readiness.keyboardChosen, "Choose", enabled = readiness.keyboardOn) {
                    showKeyboardPicker(context)
                }
                SetupStep("Allow the microphone", readiness.microphone, "Allow") { askMic.launch(voicePermissions()) }
            }
        }

        if (account.signedIn && !account.isPro) {
            AppCard {
                Text("Voice and notes use Gusan Cloud", style = MaterialTheme.typography.titleMedium, fontWeight = FontWeight.SemiBold)
                Text(
                    "Typing works on every plan. Speaking to the keyboard and meeting notes need Gusan Pro.",
                    style = MaterialTheme.typography.bodyMedium,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
                OutlinedButton(onClick = { openPricing(context) }) { Text("See plans") }
            }
        }

        AppCard {
            Text("Try it", style = MaterialTheme.typography.titleMedium, fontWeight = FontWeight.SemiBold)
            OutlinedTextField(
                value = sample,
                onValueChange = { sample = it },
                modifier = Modifier.fillMaxWidth(),
                minLines = 3,
                placeholder = { Text("Tap here, then tap the mic on the keyboard and say: “Email Anna: the demo moves to Thursday.”") },
            )
            Text(
                "Select some text first and say “make it shorter” or “translate to French” to change it. Undo puts it back.",
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
        }

        AppCard {
            Text("Meetings", style = MaterialTheme.typography.titleMedium, fontWeight = FontWeight.SemiBold)
            Text(
                "Put the phone on the table and record. Gusan writes the transcript and notes with action items.",
                style = MaterialTheme.typography.bodyMedium,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
            OutlinedButton(onClick = onMeetings) { Text("Go to Meetings") }
        }
    }
}

@Composable
private fun SetupStep(title: String, done: Boolean, action: String, onClick: () -> Unit) = SetupStep(title, done, action, true, onClick)

@Composable
private fun SetupStep(title: String, done: Boolean, action: String, enabled: Boolean, onClick: () -> Unit) {
    Row(verticalAlignment = Alignment.CenterVertically, modifier = Modifier.fillMaxWidth()) {
        Icon(
            if (done) Icons.Filled.CheckCircle else Icons.Filled.RadioButtonUnchecked,
            contentDescription = if (done) "Done" else "Not done yet",
            tint = if (done) successColor() else MaterialTheme.colorScheme.outline,
        )
        Spacer(Modifier.width(12.dp))
        Text(title, Modifier.weight(1f), style = MaterialTheme.typography.bodyLarge)
        if (!done) TextButton(onClick = onClick, enabled = enabled) { Text(action) }
    }
}
