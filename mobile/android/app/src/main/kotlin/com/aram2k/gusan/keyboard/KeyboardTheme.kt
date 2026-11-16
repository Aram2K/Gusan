package com.aram2k.gusan.keyboard

import android.content.Context
import android.content.res.Configuration

/** The keyboard's colors: flat, quiet keys like the phone's own keyboard, one accent for Gusan. */
class KeyboardTheme(val dark: Boolean) {
    val background = if (dark) 0xFF1B1C1F.toInt() else 0xFFE8EAEE.toInt()
    val key = if (dark) 0xFF3A3B40.toInt() else 0xFFFFFFFF.toInt()
    val keyPressed = if (dark) 0xFF55565C.toInt() else 0xFFD5D8DE.toInt()
    val special = if (dark) 0xFF2A2B2F.toInt() else 0xFFCDD1D8.toInt()
    val specialPressed = if (dark) 0xFF44454B.toInt() else 0xFFB8BDC6.toInt()
    val text = if (dark) 0xFFF2F3F5.toInt() else 0xFF15161A.toInt()
    val secondaryText = if (dark) 0xFFB4B7BF.toInt() else 0xFF5A5F6B.toInt()
    val accent = if (dark) 0xFF8AA9FF.toInt() else 0xFF2B5BEA.toInt()
    val onAccent = if (dark) 0xFF0B1A44.toInt() else 0xFFFFFFFF.toInt()
    val danger = if (dark) 0xFFFF8A80.toInt() else 0xFFD93025.toInt()
    val recording = if (dark) 0xFFFF6B63.toInt() else 0xFFE5322D.toInt()
    val chip = if (dark) 0xFF2A2B2F.toInt() else 0xFFFFFFFF.toInt()

    companion object {
        fun of(context: Context): KeyboardTheme = KeyboardTheme(
            context.resources.configuration.uiMode and Configuration.UI_MODE_NIGHT_MASK == Configuration.UI_MODE_NIGHT_YES,
        )
    }
}
