package com.aram2k.gusan.ui

import android.content.Context
import android.content.Intent
import android.net.Uri
import androidx.compose.foundation.Image
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.imePadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.KeyboardActions
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
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
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import com.aram2k.gusan.R
import com.aram2k.gusan.core.Backend
import com.aram2k.gusan.data.SignUp
import com.aram2k.gusan.graph
import kotlinx.coroutines.launch

fun openUrl(context: Context, url: String) {
    runCatching { context.startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(url)).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)) }
}

fun openPricing(context: Context) = openUrl(context, Backend.PRICING)

@Composable
fun SignInScreen(onDone: () -> Unit) {
    val context = LocalContext.current
    val accounts = context.graph.account
    val account by accounts.state.collectAsStateWithLifecycle()
    val scope = rememberCoroutineScope()
    var creating by rememberSaveable { mutableStateOf(false) }
    var email by rememberSaveable { mutableStateOf("") }
    var password by rememberSaveable { mutableStateOf("") }
    var busy by remember { mutableStateOf(false) }
    var problem by remember { mutableStateOf<String?>(null) }
    var notice by remember { mutableStateOf<String?>(null) }

    // Signed in (here, or through Google coming back from the browser): this page has done its job.
    LaunchedEffect(account.signedIn) { if (account.signedIn) onDone() }

    fun submit() {
        if (busy) return
        if (email.isBlank() || password.isEmpty()) {
            problem = "Enter your email and password."
            return
        }
        busy = true
        problem = null
        notice = null
        scope.launch {
            if (creating) {
                when (val result = accounts.signUp(email, password)) {
                    is SignUp.Problem -> problem = result.message
                    SignUp.CheckEmail -> {
                        notice = "Check your inbox: we sent a link to ${email.trim()}. Click it, then sign in here."
                        creating = false
                        password = ""
                    }
                    SignUp.SignedIn -> Unit
                }
            } else {
                problem = accounts.signIn(email, password)
            }
            busy = false
        }
    }

    fun resetPassword() {
        if (email.isBlank()) {
            problem = "Enter your email first."
            return
        }
        busy = true
        problem = null
        scope.launch {
            problem = accounts.sendPasswordReset(email)
            if (problem == null) notice = "If ${email.trim()} has an account, a link to choose a new password is on its way."
            busy = false
        }
    }

    Column(
        Modifier.fillMaxSize().verticalScroll(rememberScrollState()).imePadding().padding(horizontal = 20.dp, vertical = 8.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp),
        horizontalAlignment = Alignment.CenterHorizontally,
    ) {
        Row(Modifier.fillMaxWidth()) {
            IconButton(onClick = onDone) { Icon(Icons.AutoMirrored.Filled.ArrowBack, contentDescription = "Back") }
        }
        Image(painterResource(R.drawable.gusan_logo), contentDescription = null, modifier = Modifier.size(88.dp))
        ScreenTitle(if (creating) "Create your account" else "Sign in to Gusan")
        Text(
            "One account for Gusan on your phone and your computer.",
            style = MaterialTheme.typography.bodyMedium,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
            textAlign = TextAlign.Center,
        )
        Spacer(Modifier.height(4.dp))
        OutlinedButton(
            onClick = { openUrl(context, accounts.startGoogleSignIn()) },
            enabled = !busy,
            modifier = Modifier.fillMaxWidth().heightIn(min = 52.dp),
            shape = RoundedCornerShape(14.dp),
        ) { Text("Continue with Google", style = MaterialTheme.typography.titleMedium) }
        Text("or", color = MaterialTheme.colorScheme.onSurfaceVariant, style = MaterialTheme.typography.bodySmall)
        OutlinedTextField(
            value = email,
            onValueChange = { email = it },
            label = { Text("Email") },
            singleLine = true,
            enabled = !busy,
            keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Email, imeAction = ImeAction.Next),
            modifier = Modifier.fillMaxWidth(),
        )
        OutlinedTextField(
            value = password,
            onValueChange = { password = it },
            label = { Text(if (creating) "Choose a password" else "Password") },
            singleLine = true,
            enabled = !busy,
            visualTransformation = PasswordVisualTransformation(),
            keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Password, imeAction = ImeAction.Done),
            keyboardActions = KeyboardActions(onDone = { submit() }),
            modifier = Modifier.fillMaxWidth(),
        )
        problem?.let { Text(it, color = MaterialTheme.colorScheme.error, style = MaterialTheme.typography.bodyMedium, modifier = Modifier.fillMaxWidth()) }
        notice?.let { Text(it, color = successColor(), style = MaterialTheme.typography.bodyMedium, modifier = Modifier.fillMaxWidth()) }
        if (busy) {
            CircularProgressIndicator(Modifier.size(28.dp))
        } else {
            PrimaryButton(if (creating) "Create account" else "Sign in", onClick = ::submit)
        }
        TextButton(onClick = { creating = !creating; problem = null }, enabled = !busy) {
            Text(if (creating) "I already have an account" else "Create an account")
        }
        if (!creating) TextButton(onClick = ::resetPassword, enabled = !busy) { Text("Forgot your password?") }
    }
}
