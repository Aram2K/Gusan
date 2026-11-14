package com.aram2k.gusan.ui

import android.content.Intent
import android.os.Bundle
import android.widget.Toast
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.compose.foundation.layout.padding
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.GraphicEq
import androidx.compose.material.icons.filled.History
import androidx.compose.material.icons.filled.Home
import androidx.compose.material.icons.filled.Settings
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.NavigationBar
import androidx.compose.material3.NavigationBarItem
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.lifecycle.lifecycleScope
import androidx.navigation.NavGraph.Companion.findStartDestination
import androidx.navigation.NavHostController
import androidx.navigation.compose.NavHost
import androidx.navigation.compose.composable
import androidx.navigation.compose.currentBackStackEntryAsState
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
        setContent {
            GusanTheme {
                GusanRoot(meetingToOpen.value) { meetingToOpen.value = null }
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
            // Closing the Google page comes back with no code: nothing to say. A refusal is said.
            link.getQueryParameter("error_description")?.let { Toast.makeText(this, it, Toast.LENGTH_LONG).show() }
            return
        }
        lifecycleScope.launch {
            val problem = graph.account.finishGoogleSignIn(code)
            Toast.makeText(this@MainActivity, problem ?: "You're signed in", Toast.LENGTH_LONG).show()
        }
    }

    companion object {
        const val ACTION_OPEN_MEETING = "com.aram2k.gusan.OPEN_MEETING."
        const val EXTRA_MEETING_ID = "meeting_id"
    }
}

private class Tab(val route: String, val label: String, val icon: ImageVector)

private val tabs = listOf(
    Tab("home", "Home", Icons.Filled.Home),
    Tab("meetings", "Meetings", Icons.Filled.GraphicEq),
    Tab("sessions", "Sessions", Icons.Filled.History),
    Tab("settings", "Settings", Icons.Filled.Settings),
)

private fun NavHostController.toTab(route: String) = navigate(route) {
    popUpTo(graph.findStartDestination().id) { saveState = true }
    launchSingleTop = true
    restoreState = true
}

@Composable
private fun GusanRoot(meetingToOpen: String?, onOpened: () -> Unit) {
    val nav = rememberNavController()
    val entry by nav.currentBackStackEntryAsState()
    val route = entry?.destination?.route

    LaunchedEffect(meetingToOpen) {
        if (meetingToOpen != null) {
            nav.toTab("meetings")
            nav.navigate("meeting/$meetingToOpen") { launchSingleTop = true }
            onOpened()
        }
    }

    Scaffold(
        containerColor = MaterialTheme.colorScheme.background,
        bottomBar = {
            if (tabs.any { it.route == route }) {
                NavigationBar(containerColor = MaterialTheme.colorScheme.surface) {
                    tabs.forEach { tab ->
                        NavigationBarItem(
                            selected = route == tab.route,
                            onClick = { nav.toTab(tab.route) },
                            icon = { Icon(tab.icon, contentDescription = null) },
                            label = { Text(tab.label) },
                        )
                    }
                }
            }
        },
    ) { padding ->
        NavHost(nav, startDestination = "home", modifier = Modifier.padding(padding)) {
            composable("home") {
                HomeScreen(onSignIn = { nav.navigate("signin") }, onMeetings = { nav.toTab("meetings") })
            }
            composable("meetings") {
                MeetingsScreen(onOpen = { nav.navigate("meeting/$it") }, onSignIn = { nav.navigate("signin") })
            }
            composable("meeting/{id}") { back ->
                MeetingDetailScreen(back.arguments?.getString("id").orEmpty(), onBack = { nav.popBackStack() })
            }
            composable("sessions") { SessionsScreen() }
            composable("settings") { SettingsScreen(onSignIn = { nav.navigate("signin") }) }
            composable("signin") { SignInScreen(onDone = { nav.popBackStack() }) }
        }
    }
}
