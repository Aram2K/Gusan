package com.aram2k.gusan.ui

import android.content.Intent
import android.os.Bundle
import android.widget.Toast
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.material3.MaterialTheme
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.mutableStateOf
import androidx.compose.ui.Modifier
import androidx.lifecycle.lifecycleScope
import androidx.navigation.compose.NavHost
import androidx.navigation.compose.composable
import androidx.navigation.compose.rememberNavController
import com.aram2k.gusan.graph
import kotlinx.coroutines.launch

class MainActivity : ComponentActivity() {
    /** A meeting a notification asked to open. */
    private val meetingToOpen = mutableStateOf<String?>(null)

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()
        handle(intent)
        graph.account.refresh()
        val firstRun = !graph.settings.onboardingDone
        setContent {
            GusanTheme {
                Box(Modifier.fillMaxSize().background(MaterialTheme.colorScheme.background)) {
                    GusanNav(firstRun, meetingToOpen.value) { meetingToOpen.value = null }
                }
            }
        }
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        setIntent(intent)
        handle(intent)
    }

    private fun handle(intent: Intent?) {
        intent ?: return
        intent.getStringExtra(EXTRA_MEETING_ID)?.let {
            meetingToOpen.value = it
            intent.removeExtra(EXTRA_MEETING_ID)
        }
        val link = intent.data ?: return
        if (link.scheme != "gusan" || link.host != "auth-callback") return
        intent.data = null
        val code = link.getQueryParameter("code")
        if (code.isNullOrEmpty()) {
            link.getQueryParameter("error_description")?.let { Toast.makeText(this, it, Toast.LENGTH_LONG).show() }
            return
        }
        lifecycleScope.launch {
            graph.account.finishGoogleSignIn(code)?.let { Toast.makeText(this@MainActivity, it, Toast.LENGTH_LONG).show() }
        }
    }

    companion object {
        const val ACTION_OPEN_MEETING = "com.aram2k.gusan.OPEN_MEETING."
        const val EXTRA_MEETING_ID = "meeting_id"
    }
}

@Composable
private fun GusanNav(firstRun: Boolean, meetingToOpen: String?, onOpened: () -> Unit) {
    val nav = rememberNavController()
    LaunchedEffect(meetingToOpen) {
        if (meetingToOpen != null) {
            nav.navigate("meeting/$meetingToOpen") { launchSingleTop = true }
            onOpened()
        }
    }
    NavHost(nav, startDestination = if (firstRun) "welcome" else "home") {
        composable("welcome") {
            Onboarding(firstRun = true) { nav.navigate("home") { popUpTo("welcome") { inclusive = true } } }
        }
        composable("setup") { Onboarding(firstRun = false) { nav.popBackStack() } }
        composable("home") {
            HomeScreen(
                onSettings = { nav.navigate("settings") },
                onSetup = { nav.navigate("setup") },
                onSignIn = { nav.navigate("signin") },
                onMeeting = { nav.navigate("meeting/$it") },
                onRecording = { nav.navigate("recording") { launchSingleTop = true } },
            )
        }
        composable("recording") {
            RecordingScreen(
                onMinimize = { nav.popBackStack() },
                onStopped = { id ->
                    nav.popBackStack()
                    if (id != null) nav.navigate("meeting/$id")
                },
            )
        }
        composable("meeting/{id}") { back -> MeetingDetailScreen(back.arguments?.getString("id").orEmpty(), onBack = { nav.popBackStack() }) }
        composable("settings") { SettingsScreen(onBack = { nav.popBackStack() }, onSignIn = { nav.navigate("signin") }) }
        composable("signin") { SignInScreen(onDone = { nav.popBackStack() }) }
    }
}
