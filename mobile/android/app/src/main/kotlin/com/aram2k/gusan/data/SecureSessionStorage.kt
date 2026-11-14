package com.aram2k.gusan.data

import android.content.Context
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import android.util.Base64
import com.aram2k.gusan.core.Session
import com.aram2k.gusan.core.SessionStorage
import java.security.KeyStore
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec

/**
 * The sign-in, encrypted with a key that never leaves the phone's keystore.
 * If it can't be read back (the key is gone after a restore, say) the person is simply signed out.
 */
class SecureSessionStorage(context: Context) : SessionStorage {
    private val prefs = context.getSharedPreferences("gusan_session", Context.MODE_PRIVATE)
    private var cached: Session? = null
    private var loaded = false

    private fun key(): SecretKey {
        val store = KeyStore.getInstance("AndroidKeyStore").apply { load(null) }
        (store.getKey(ALIAS, null) as? SecretKey)?.let { return it }
        val generator = KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, "AndroidKeyStore")
        generator.init(
            KeyGenParameterSpec.Builder(ALIAS, KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT)
                .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
                .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
                .setKeySize(256)
                .build(),
        )
        return generator.generateKey()
    }

    @Synchronized
    override fun read(): Session? {
        if (loaded) return cached
        loaded = true
        val stored = prefs.getString("session", null) ?: return null
        cached = runCatching {
            val bytes = Base64.decode(stored, Base64.NO_WRAP)
            val cipher = Cipher.getInstance(TRANSFORMATION)
            cipher.init(Cipher.DECRYPT_MODE, key(), GCMParameterSpec(128, bytes, 0, IV_BYTES))
            Session.fromJson(cipher.doFinal(bytes, IV_BYTES, bytes.size - IV_BYTES).toString(Charsets.UTF_8))
        }.getOrNull()
        if (cached == null) prefs.edit().remove("session").apply()
        return cached
    }

    @Synchronized
    override fun write(session: Session) {
        cached = session
        loaded = true
        runCatching {
            val cipher = Cipher.getInstance(TRANSFORMATION)
            cipher.init(Cipher.ENCRYPT_MODE, key())
            val bytes = cipher.iv + cipher.doFinal(session.toJson().toByteArray())
            prefs.edit().putString("session", Base64.encodeToString(bytes, Base64.NO_WRAP)).apply()
        }
    }

    @Synchronized
    override fun clear() {
        cached = null
        loaded = true
        prefs.edit().remove("session").apply()
    }

    private companion object {
        const val ALIAS = "gusan_session_key"
        const val TRANSFORMATION = "AES/GCM/NoPadding"
        const val IV_BYTES = 12
    }
}
