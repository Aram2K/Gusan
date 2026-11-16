package com.aram2k.gusan.data

import android.app.Activity
import android.net.Uri
import androidx.browser.customtabs.CustomTabsIntent
import androidx.credentials.CredentialManager
import androidx.credentials.CustomCredential
import androidx.credentials.GetCredentialRequest
import androidx.credentials.exceptions.GetCredentialCancellationException
import androidx.credentials.exceptions.GetCredentialException
import com.aram2k.gusan.BuildConfig
import com.aram2k.gusan.core.AuthClient
import com.google.android.libraries.identity.googleid.GetSignInWithGoogleOption
import com.google.android.libraries.identity.googleid.GoogleIdTokenCredential
import java.util.UUID

/**
 * "Continue with Google". With the Web client ID set, the phone's own Google account sheet slides up
 * and the person never leaves the app. Otherwise (or if the sheet can't be used on this phone) Google's
 * page opens in an in-app browser tab and comes back to gusan://auth-callback.
 */
object GoogleSignIn {
    val native: Boolean get() = BuildConfig.GOOGLE_WEB_CLIENT_ID.isNotEmpty()

    /** Returns null when signed in or cancelled, or what went wrong. */
    suspend fun signIn(activity: Activity, accounts: AccountRepository): String? {
        if (native) {
            val nonce = UUID.randomUUID().toString()
            val option = GetSignInWithGoogleOption.Builder(BuildConfig.GOOGLE_WEB_CLIENT_ID)
                .setNonce(AuthClient.hashNonce(nonce))
                .build()
            val request = GetCredentialRequest.Builder().addCredentialOption(option).build()
            try {
                val credential = CredentialManager.create(activity).getCredential(activity, request).credential
                if (credential is CustomCredential && credential.type == GoogleIdTokenCredential.TYPE_GOOGLE_ID_TOKEN_CREDENTIAL) {
                    val token = GoogleIdTokenCredential.createFrom(credential.data).idToken
                    return accounts.signInWithGoogleToken(token, nonce)
                }
            } catch (e: GetCredentialCancellationException) {
                return null
            } catch (e: GetCredentialException) {
                // No Google Play services, or this build isn't registered with Google: use the web page.
            }
        }
        inBrowser(activity, accounts.startGoogleSignIn())
        return null
    }

    fun inBrowser(activity: Activity, url: String) {
        runCatching {
            CustomTabsIntent.Builder().setShowTitle(false).setUrlBarHidingEnabled(true).build().launchUrl(activity, Uri.parse(url))
        }
    }
}
