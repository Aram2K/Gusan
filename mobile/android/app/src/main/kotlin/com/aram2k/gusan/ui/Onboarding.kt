package com.aram2k.gusan.ui

import android.Manifest
import android.os.Build
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.animation.AnimatedContent
import androidx.compose.animation.fadeIn
import androidx.compose.animation.fadeOut
import androidx.compose.animation.slideInHorizontally
import androidx.compose.animation.togetherWith
import androidx.compose.foundation.Image
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.imePadding
import androidx.compose.foundation.layout.navigationBarsPadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.statusBarsPadding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.CheckCircle
import androidx.compose.material.icons.filled.Keyboard
import androidx.compose.material.icons.filled.Mic
import androidx.compose.material.icons.filled.NotificationsActive
import androidx.compose.material.icons.outlined.Lock
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.focus.FocusRequester
import androidx.compose.ui.focus.focusRequester
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.LifecycleResumeEffect
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import com.aram2k.gusan.R
import com.aram2k.gusan.graph
import kotlinx.coroutines.delay

private enum class Step { NOTIFICATIONS, MICROPHONE, ACCOUNT, KEYBOARD, TRY }

/**
 * The first run: one short screen per thing Gusan needs, each saying why before Android asks.
 * Permission dialogs open by themselves; finished steps move on by themselves; every step can be
 * left for later. Opened again from Home ("Finish setup"), it shows only what is still missing.
 */
@Composable
fun Onboarding(firstRun: Boolean, onDone: () -> Unit) {
    val context = LocalContext.current
    val graph = context.graph
    val readiness = rememberReadiness()
    val account by graph.account.state.collectAsStateWithLifecycle()

    fun done(step: Step): Boolean {
        val r = Readiness.of(context)
        return when (step) {
            Step.NOTIFICATIONS -> r.notifications
            Step.MICROPHONE -> r.microphone
            Step.ACCOUNT -> graph.account.state.value.signedIn
            Step.KEYBOARD -> r.keyboardReady
            Step.TRY -> false
        }
    }

    // The steps this run shows, fixed when it opens so the dots don't jump around.
    val steps = remember {
        Step.values().filter { step ->
            when (step) {
                Step.NOTIFICATIONS -> Build.VERSION.SDK_INT >= 33 && !done(step)
                Step.TRY -> firstRun || !Readiness.of(context).keyboardReady
                else -> !done(step)
            }
        }.ifEmpty { listOf(Step.TRY) }
    }
    var index by rememberSaveable { mutableIntStateOf(0) }
    val step = steps[index.coerceIn(0, steps.lastIndex)]

    fun finish() {
        graph.settings.onboardingDone = true
        onDone()
    }

    fun next() {
        var i = index + 1
        while (i < steps.size && done(steps[i])) i++
        if (i >= steps.size) finish() else index = i
    }

    // A step that got done (the dialog answered, the keyboard turned on, signed in) moves on by itself.
    LaunchedEffect(step, readiness, account.signedIn) {
        if (step != Step.TRY && done(step)) {
            delay(450)
            next()
        }
    }

    Column(Modifier.fillMaxSize().statusBarsPadding().navigationBarsPadding().imePadding()) {
        Row(Modifier.fillMaxWidth().padding(horizontal = 24.dp, vertical = 16.dp), verticalAlignment = Alignment.CenterVertically) {
            steps.forEachIndexed { i, _ ->
                Box(
                    Modifier.padding(end = 6.dp).height(4.dp).width(if (i == index) 28.dp else 12.dp).clip(CircleShape)
                        .background(if (i <= index) MaterialTheme.colorScheme.primary else MaterialTheme.colorScheme.outlineVariant),
                )
            }
            Spacer(Modifier.weight(1f))
            if (step != Step.TRY || !firstRun) TextButton(onClick = ::finish) { Text(if (firstRun) "Skip setup" else "Close") }
        }
        AnimatedContent(
            targetState = step,
            transitionSpec = { (slideInHorizontally { it / 3 } + fadeIn()) togetherWith fadeOut() },
            label = "step",
            modifier = Modifier.weight(1f),
        ) { current ->
            when (current) {
                Step.NOTIFICATIONS -> PermissionStep(
                    permission = Manifest.permission.POST_NOTIFICATIONS,
                    welcome = firstRun && index == 0,
                    icon = Icons.Filled.NotificationsActive,
                    title = "Know when your notes are ready",
                    body = "Gusan lets you know the moment a meeting's notes are written, and keeps a small notice up while it records, so you always know when it's listening.",
                    allow = "Allow notifications",
                    granted = readiness.notifications,
                    onSkip = ::next,
                )
                Step.MICROPHONE -> PermissionStep(
                    permission = Manifest.permission.RECORD_AUDIO,
                    welcome = firstRun && index == 0,
                    icon = Icons.Filled.Mic,
                    title = "Let Gusan hear you",
                    body = "Speak instead of typing, and record meetings. Gusan only listens after you tap the mic, and you'll see it's on.",
                    allow = "Allow microphone",
                    granted = readiness.microphone,
                    onSkip = ::next,
                )
                Step.ACCOUNT -> StepPage(
                    icon = null,
                    title = "Sign in",
                    body = "Your Gusan account works on your phone and your computer.",
                ) {
                    SignInContent()
                    TextButton(onClick = ::next, modifier = Modifier.fillMaxWidth()) { Text("Not now") }
                }
                Step.KEYBOARD -> KeyboardStep(readiness, onSkip = ::next)
                Step.TRY -> TryStep(onDone = ::finish)
            }
        }
    }
}

@Composable
private fun StepPage(icon: ImageVector?, title: String, body: String, welcome: Boolean = false, content: @Composable () -> Unit) {
    Column(
        Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(horizontal = 24.dp),
        horizontalAlignment = Alignment.CenterHorizontally,
    ) {
        Spacer(Modifier.height(24.dp))
        if (welcome) {
            Image(painterResource(R.drawable.gusan_logo), contentDescription = null, modifier = Modifier.size(84.dp))
            Spacer(Modifier.height(16.dp))
            Text("Welcome to Gusan", style = MaterialTheme.typography.headlineLarge, textAlign = TextAlign.Center)
            Text(
                "Talk, and it's written. In any app.",
                style = MaterialTheme.typography.titleMedium,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
                modifier = Modifier.padding(top = 6.dp),
            )
            Spacer(Modifier.height(40.dp))
        } else if (icon != null) {
            Spacer(Modifier.height(24.dp))
        }
        if (icon != null) {
            Box(Modifier.size(72.dp).clip(CircleShape).background(MaterialTheme.colorScheme.primaryContainer), contentAlignment = Alignment.Center) {
                Icon(icon, contentDescription = null, tint = MaterialTheme.colorScheme.onPrimaryContainer, modifier = Modifier.size(34.dp))
            }
            Spacer(Modifier.height(24.dp))
        }
        Text(title, style = if (welcome) MaterialTheme.typography.headlineSmall else MaterialTheme.typography.headlineMedium, textAlign = TextAlign.Center)
        Text(
            body,
            style = MaterialTheme.typography.bodyLarge,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
            textAlign = TextAlign.Center,
            modifier = Modifier.padding(top = 12.dp, bottom = 32.dp),
        )
        content()
        Spacer(Modifier.height(24.dp))
    }
}

/** Says why, then opens Android's own question by itself. */
@Composable
private fun PermissionStep(
    permission: String,
    welcome: Boolean,
    icon: ImageVector,
    title: String,
    body: String,
    allow: String,
    granted: Boolean,
    onSkip: () -> Unit,
) {
    val context = LocalContext.current
    var refused by remember { mutableStateOf(false) }
    val ask = rememberLauncherForActivityResult(ActivityResultContracts.RequestPermission()) { ok -> refused = !ok }
    // Long enough to read the title before Android's question covers the screen.
    LaunchedEffect(permission) {
        delay(if (welcome) 1200 else 700)
        if (!granted) ask.launch(permission)
    }
    StepPage(icon, title, body, welcome) {
        when {
            granted -> Row(verticalAlignment = Alignment.CenterVertically) {
                Icon(Icons.Filled.CheckCircle, contentDescription = null, tint = successColor())
                Spacer(Modifier.width(8.dp))
                Text("Allowed", style = MaterialTheme.typography.titleMedium, color = successColor())
            }
            refused -> {
                Text(
                    "No problem. You can turn it on later in the phone's settings for Gusan.",
                    style = MaterialTheme.typography.bodyMedium,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                    textAlign = TextAlign.Center,
                    modifier = Modifier.padding(bottom = 16.dp),
                )
                BigButton("Continue", onClick = onSkip)
                TextButton(onClick = { openAppSettings(context) }) { Text("Open settings") }
            }
            else -> {
                BigButton(allow, onClick = { ask.launch(permission) })
                TextButton(onClick = onSkip) { Text("Not now") }
            }
        }
    }
}

@Composable
private fun KeyboardStep(readiness: Readiness, onSkip: () -> Unit) {
    val context = LocalContext.current
    var opened by remember { mutableStateOf(false) }
    // Back from the system's keyboard list with Gusan on: offer the switch at once, no extra tap.
    LifecycleResumeEffect(readiness.keyboardOn) {
        if (opened && readiness.keyboardOn && !readiness.keyboardChosen) showKeyboardPicker(context)
        onPauseOrDispose { }
    }
    StepPage(
        icon = Icons.Filled.Keyboard,
        title = if (!readiness.keyboardOn) "Add the Gusan keyboard" else "Switch to Gusan",
        body = if (!readiness.keyboardOn) {
            "The mic lives on the Gusan keyboard, so you can talk in any app. Turn on Gusan in the list, then come back."
        } else {
            "Pick Gusan in the list. You can switch back anytime with the keyboard icon at the bottom of your screen."
        },
    ) {
        Panel(Modifier.padding(bottom = 24.dp)) {
            Row(verticalAlignment = Alignment.Top) {
                Icon(Icons.Outlined.Lock, contentDescription = null, tint = MaterialTheme.colorScheme.onSurfaceVariant, modifier = Modifier.size(20.dp))
                Spacer(Modifier.width(12.dp))
                Text(
                    "Android shows a standard warning for every keyboard. Gusan never sends what you type; your voice goes to Gusan Cloud only after you tap the mic.",
                    style = MaterialTheme.typography.bodyMedium,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
        }
        if (!readiness.keyboardOn) {
            BigButton("Open keyboard list", onClick = { opened = true; openKeyboardSettings(context) })
        } else {
            BigButton("Choose Gusan", onClick = { showKeyboardPicker(context) })
        }
        TextButton(onClick = onSkip) { Text("Not now") }
    }
}

@Composable
private fun TryStep(onDone: () -> Unit) {
    var text by rememberSaveable { mutableStateOf("") }
    val focus = remember { FocusRequester() }
    LaunchedEffect(Unit) {
        delay(400)
        runCatching { focus.requestFocus() }
    }
    StepPage(icon = null, title = "Try it now", body = "Tap the blue mic above the keys and say:") {
        Panel(Modifier.padding(bottom = 16.dp)) {
            Text(
                "“Text Anna that the demo moves to Thursday at three.”",
                style = MaterialTheme.typography.titleMedium,
                textAlign = TextAlign.Center,
                modifier = Modifier.fillMaxWidth(),
            )
        }
        OutlinedTextField(
            value = text,
            onValueChange = { text = it },
            modifier = Modifier.fillMaxWidth().focusRequester(focus),
            minLines = 4,
            shape = RoundedCornerShape(16.dp),
            placeholder = { Text("Gusan writes here") },
        )
        Text(
            "Tip: select text and say “make it friendlier” or “translate to French”.",
            style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
            textAlign = TextAlign.Center,
            modifier = Modifier.padding(vertical = 16.dp),
        )
        BigButton(if (text.isBlank()) "Skip for now" else "Done", onClick = onDone)
    }
}
