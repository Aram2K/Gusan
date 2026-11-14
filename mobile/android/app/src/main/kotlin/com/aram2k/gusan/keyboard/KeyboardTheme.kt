package com.aram2k.gusan.keyboard

import android.content.Context
import android.content.res.Configuration

/** The keyboard's colors. The same tokens as the iPhone keyboard (ios/DESIGN.md). */
class KeyboardTheme(val dark: Boolean) {
    val background = if (dark) 0xFF2C2C2E.toInt() else 0xFFD1D3D9.toInt()
    val key = if (dark) 0xFF6B6B6B.toInt() else 0xFFFFFFFF.toInt()
    val keyPressed = if (dark) 0xFF8A8A8A.toInt() else 0xFFE4E6EB.toInt()
    val special = if (dark) 0xFF4A4A4A.toInt() else 0xFFACB3BC.toInt()
    val specialPressed = if (dark) 0xFF636363.toInt() else 0xFFC4C9D0.toInt()
    val text = if (dark) 0xFFFFFFFF.toInt() else 0xFF000000.toInt()
    val secondaryText = if (dark) 0xFFC7C7CC.toInt() else 0xFF4B5563.toInt()
    val accent = if (dark) 0xFF3B82F6.toInt() else 0xFF2563EB.toInt()
    val onAccent = 0xFFFFFFFF.toInt()
    val danger = if (dark) 0xFFEF4444.toInt() else 0xFFDC2626.toInt()
    val brandRed = if (dark) 0xFFE5483F.toInt() else 0xFFD91E18.toInt()
    val brandBlue = if (dark) 0xFF4A6FD8.toInt() else 0xFF0033A0.toInt()
    val shadow = if (dark) 0x66000000 else 0x33000000

    companion object {
        fun of(context: Context): KeyboardTheme = KeyboardTheme(
            context.resources.configuration.uiMode and Configuration.UI_MODE_NIGHT_MASK == Configuration.UI_MODE_NIGHT_YES,
        )
    }
}
