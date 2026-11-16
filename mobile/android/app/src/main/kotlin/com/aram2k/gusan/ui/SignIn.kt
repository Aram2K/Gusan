package com.aram2k.gusan.ui

import android.app.Activity
import android.content.Context
import android.content.ContextWrapper
import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.Image
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.imePadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.statusBarsPadding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.KeyboardActions
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material.icons.filled.Email
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
import com.aram2k.gusan.data.GoogleSignIn
import com.aram2k.gusan.data.SignUp
import com.aram2k.gusan.graph
import kotlinx.coroutines.launch

tailrec fun Context.findActivity(): Activity? = when (this) {
    is Activity -> this
    is ContextWrapper -> baseContext.findActivity()
    else -> null
}

/** Google first (one tap), email underneath. Used by the setup and by Settings. */
@Composable
fun SignInContent(modifier: Modifier = Modifier) {
    val context = LocalContext.current
    val accounts = context.graph.account
    val scope = rememberCoroutineScope()
    var withEmail by rememberSaveable { mutableStateOf(false) }
    var creating by rememberSaveable { mutableStateOf(false) }
    var email by rememberSaveable { mutableStateOf("") }
    var password by rememberSaveable { mutableStateOf("") }
    var busy by remember { mutableStateOf(false) }
    var problem by remember { mutableStateOf<String?>(null) }
    var notice by remember { mutableStateOf<String?>(null) }

    fun google() {
        val activity = context.findActivity() ?: return
        busy = true
        problem = null
        scope.launch {
            problem = GoogleSignIn.signIn(activity, accounts)
            busy = false
        }
    }

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
                        notice = "Almost there. Open the link we sent to ${email.trim()}, then sign in."
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

    fun reset() {
        if (email.isBlank()) {
            problem = "Enter your email first."
            return
        }
        busy = true
        scope.launch {
            problem = accounts.sendPasswordReset(email)
            if (problem == null) notice = "Check ${email.trim()} for a link to choose a new password."
            busy = false
        }
    }

    Column(modifier.fillMaxWidth(), verticalArrangement = Arrangement.spacedBy(12.dp), horizontalAlignment = Alignment.CenterHorizontally) {
        OutlinedButton(
            onClick = ::google,
            enabled = !busy,
            modifier = Modifier.fillMaxWidth().heightIn(min = 56.dp),
            shape = RoundedCornerShape(28.dp),
            border = BorderStroke(1.dp, MaterialTheme.colorScheme.outlineVariant),
        ) {
            Image(painterResource(R.drawable.google_g), contentDescription = null, modifier = Modifier.size(20.dp))
            Spacer(Modifier.width(12.dp))
            Text("Continue with Google", style = MaterialTheme.typography.titleMedium, color = MaterialTheme.colorScheme.onSurface)
        }
        if (!withEmail) {
            OutlinedButton(
                onClick = { withEmail = true },
                enabled = !busy,
                modifier = Modifier.fillMaxWidth().heightIn(min = 56.dp),
                shape = RoundedCornerShape(28.dp),
                border = BorderStroke(1.dp, MaterialTheme.colorScheme.outlineVariant),
            ) {
                Icon(Icons.Filled.Email, contentDescription = null, tint = MaterialTheme.colorScheme.onSurface)
                Spacer(Modifier.width(12.dp))
                Text("Continue with email", style = MaterialTheme.typography.titleMedium, color = MaterialTheme.colorScheme.onSurface)
            }
        } else {
            OutlinedTextField(
                value = email, onValueChange = { email = it }, label = { Text("Email") }, singleLine = true, enabled = !busy,
                shape = RoundedCornerShape(16.dp),
                keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Email, imeAction = ImeAction.Next),
                modifier = Modifier.fillMaxWidth(),
            )
            OutlinedTextField(
                value = password, onValueChange = { password = it }, label = { Text(if (creating) "Choose a password" else "Password") },
                singleLine = true, enabled = !busy, shape = RoundedCornerShape(16.dp),
                visualTransformation = PasswordVisualTransformation(),
                keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Password, imeAction = ImeAction.Done),
                keyboardActions = KeyboardActions(onDone = { submit() }),
                modifier = Modifier.fillMaxWidth(),
            )
            BigButton(if (creating) "Create account" else "Sign in", enabled = !busy, onClick = ::submit)
            Row {
                TextButton(onClick = { creating = !creating; problem = null }, enabled = !busy) {
                    Text(if (creating) "I have an account" else "New here? Create an account")
                }
                if (!creating) TextButton(onClick = ::reset, enabled = !busy) { Text("Forgot password?") }
            }
        }
        if (busy) CircularProgressIndicator(Modifier.size(24.dp), strokeWidth = 2.dp)
        problem?.let { Text(it, color = MaterialTheme.colorScheme.error, style = MaterialTheme.typography.bodyMedium, textAlign = TextAlign.Center) }
        notice?.let { Text(it, color = successColor(), style = MaterialTheme.typography.bodyMedium, textAlign = TextAlign.Center) }
    }
}

/** Sign in from Settings, after signing out. */
@Composable
fun SignInScreen(onDone: () -> Unit) {
    val context = LocalContext.current
    val account by context.graph.account.state.collectAsStateWithLifecycle()
    LaunchedEffect(account.signedIn) { if (account.signedIn) onDone() }
    Column(
        Modifier.fillMaxSize().statusBarsPadding().verticalScroll(rememberScrollState()).imePadding().padding(horizontal = 24.dp),
        horizontalAlignment = Alignment.CenterHorizontally,
        verticalArrangement = Arrangement.spacedBy(16.dp),
    ) {
        Row(Modifier.fillMaxWidth().padding(top = 8.dp)) {
            IconButton(onClick = onDone) { Icon(Icons.AutoMirrored.Filled.ArrowBack, contentDescription = "Back") }
        }
        Image(painterResource(R.drawable.gusan_logo), contentDescription = null, modifier = Modifier.size(72.dp))
        Text("Sign in to Gusan", style = MaterialTheme.typography.headlineMedium)
        Text(
            "One account for your phone and your computer.",
            style = MaterialTheme.typography.bodyLarge,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
            textAlign = TextAlign.Center,
        )
        SignInContent(Modifier.padding(top = 8.dp))
    }
}
