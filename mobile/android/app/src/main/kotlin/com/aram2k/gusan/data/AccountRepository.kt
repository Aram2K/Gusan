package com.aram2k.gusan.data

import com.aram2k.gusan.core.AuthClient
import com.aram2k.gusan.core.GusanException
import com.aram2k.gusan.core.Pkce
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

sealed class SignUp {
    object SignedIn : SignUp()
    object CheckEmail : SignUp()
    data class Problem(val message: String) : SignUp()
}

data class Account(val signedIn: Boolean, val email: String = "", val isPro: Boolean = false)

/** Who is signed in and on which plan, for every screen and the keyboard. */
class AccountRepository(
    private val auth: AuthClient,
    private val settings: Settings,
    private val scope: CoroutineScope,
) {
    private val _state = MutableStateFlow(current())
    val state: StateFlow<Account> = _state

    private fun current(): Account {
        val session = auth.storage.read()
        return Account(session != null, session?.email.orEmpty(), session != null && settings.knownPro)
    }

    /** Asks the server for the plan. Quiet when offline: the last known answer stays. */
    fun refresh() {
        scope.launch {
            val pro = withContext(Dispatchers.IO) {
                try {
                    auth.entitlement(auth.accessToken()).isPro
                } catch (e: GusanException) {
                    null
                }
            }
            if (pro != null) settings.knownPro = pro
            _state.value = current()
        }
    }

    /** Returns null when signed in, or what went wrong in words. */
    suspend fun signIn(email: String, password: String): String? = attempt { auth.signIn(email, password) }

    /** Creates an account: SignedIn, CheckEmail (a confirmation link was sent) or Problem. */
    suspend fun signUp(email: String, password: String): SignUp {
        var waiting = false
        val problem = attempt { waiting = auth.signUp(email, password) == null }
        return when {
            problem != null -> SignUp.Problem(problem)
            waiting -> SignUp.CheckEmail
            else -> SignUp.SignedIn
        }
    }

    /** Returns null when the email was sent, or what went wrong. */
    suspend fun sendPasswordReset(email: String): String? = withContext(Dispatchers.IO) {
        try {
            auth.sendPasswordReset(email); null
        } catch (e: GusanException) {
            e.message ?: e.kind.message
        }
    }

    suspend fun finishGoogleSignIn(code: String): String? {
        val verifier = settings.pendingPkce ?: return "Google sign-in was interrupted. Try again."
        settings.pendingPkce = null
        return attempt { auth.exchange(code, Pkce(verifier)) }
    }

    /** The address to open in the browser to sign in with Google. */
    fun startGoogleSignIn(): String {
        val pkce = Pkce.generate()
        settings.pendingPkce = pkce.verifier
        return AuthClient.authorizeUrl(pkce)
    }

    private suspend fun attempt(block: () -> Unit): String? {
        val problem = withContext(Dispatchers.IO) {
            try {
                block(); null
            } catch (e: GusanException) {
                e.message ?: e.kind.message
            }
        }
        if (problem == null) {
            _state.value = current()
            refresh()
        }
        return problem
    }

    fun signOut() {
        scope.launch {
            withContext(Dispatchers.IO) { auth.signOut() }
            settings.knownPro = false
            _state.value = current()
        }
    }

    /** Called when a request came back "sign in again": the stored sign-in is gone. */
    fun sessionLost() { _state.value = current() }
}
