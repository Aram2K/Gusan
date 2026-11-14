package com.aram2k.gusan.data

import android.content.Context
import com.aram2k.gusan.core.SpeechLanguages

/** The person's choices. Read by the app and the keyboard. */
class Settings(context: Context) {
    private val prefs = context.getSharedPreferences("gusan_settings", Context.MODE_PRIVATE)

    var language: String
        get() = SpeechLanguages.normalize(prefs.getString("language", "auto"))
        set(value) = prefs.edit().putString("language", SpeechLanguages.normalize(value)).apply()

    var haptics: Boolean
        get() = prefs.getBoolean("haptics", true)
        set(value) = prefs.edit().putBoolean("haptics", value).apply()

    /** The short tone when the keyboard starts listening. */
    var sounds: Boolean
        get() = prefs.getBoolean("sounds", true)
        set(value) = prefs.edit().putBoolean("sounds", value).apply()

    var keySounds: Boolean
        get() = prefs.getBoolean("key_sounds", false)
        set(value) = prefs.edit().putBoolean("key_sounds", value).apply()

    var autoCapitalize: Boolean
        get() = prefs.getBoolean("auto_capitalize", true)
        set(value) = prefs.edit().putBoolean("auto_capitalize", value).apply()

    var doubleSpacePeriod: Boolean
        get() = prefs.getBoolean("double_space_period", true)
        set(value) = prefs.edit().putBoolean("double_space_period", value).apply()

    var saveHistory: Boolean
        get() = prefs.getBoolean("save_history", true)
        set(value) = prefs.edit().putBoolean("save_history", value).apply()

    /** What the server last said about the plan, so the screens don't flicker while it is asked again. */
    var knownPro: Boolean
        get() = prefs.getBoolean("known_pro", false)
        set(value) = prefs.edit().putBoolean("known_pro", value).apply()

    /** The secret half of a Google sign-in that is on its way through the browser. */
    var pendingPkce: String?
        get() = prefs.getString("pending_pkce", null)
        set(value) = prefs.edit().putString("pending_pkce", value).apply()
}
