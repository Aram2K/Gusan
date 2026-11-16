package com.aram2k.gusan.ui

import androidx.compose.foundation.background
import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ColumnScope
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.Typography
import androidx.compose.material3.darkColorScheme
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp

object Brand {
    val Record = Color(0xFFE5322D)
    val RecordDark = Color(0xFFFF6B63)
    val ProStart = Color(0xFF7C3AED)
    val ProEnd = Color(0xFF2B5BEA)
    val Success = Color(0xFF12A150)
    val SuccessDark = Color(0xFF4ADE80)
}

private val Light = lightColorScheme(
    primary = Color(0xFF2B5BEA),
    onPrimary = Color.White,
    primaryContainer = Color(0xFFE3EAFE),
    onPrimaryContainer = Color(0xFF0B2A80),
    secondary = Color(0xFF5A5F6B),
    onSecondary = Color.White,
    secondaryContainer = Color(0xFFECEEF2),
    onSecondaryContainer = Color(0xFF1D2027),
    background = Color(0xFFF6F7F9),
    onBackground = Color(0xFF14161A),
    surface = Color(0xFFF6F7F9),
    onSurface = Color(0xFF14161A),
    surfaceVariant = Color(0xFFECEEF2),
    onSurfaceVariant = Color(0xFF5A5F6B),
    surfaceContainerLowest = Color.White,
    surfaceContainerLow = Color.White,
    surfaceContainer = Color(0xFFFFFFFF),
    surfaceContainerHigh = Color(0xFFF0F2F5),
    surfaceContainerHighest = Color(0xFFE6E9EE),
    outline = Color(0xFF9AA0AC),
    outlineVariant = Color(0xFFE2E5EA),
    error = Color(0xFFD93025),
    onError = Color.White,
)

private val Dark = darkColorScheme(
    primary = Color(0xFF8AA9FF),
    onPrimary = Color(0xFF0B1A44),
    primaryContainer = Color(0xFF1E2F66),
    onPrimaryContainer = Color(0xFFDCE5FF),
    secondary = Color(0xFFB4B7BF),
    onSecondary = Color(0xFF1D2027),
    secondaryContainer = Color(0xFF2A2C32),
    onSecondaryContainer = Color(0xFFE4E6EB),
    background = Color(0xFF0F1012),
    onBackground = Color(0xFFF1F2F4),
    surface = Color(0xFF0F1012),
    onSurface = Color(0xFFF1F2F4),
    surfaceVariant = Color(0xFF24262B),
    onSurfaceVariant = Color(0xFFA9ADB6),
    surfaceContainerLowest = Color(0xFF151619),
    surfaceContainerLow = Color(0xFF18191C),
    surfaceContainer = Color(0xFF1B1C20),
    surfaceContainerHigh = Color(0xFF24262B),
    surfaceContainerHighest = Color(0xFF2E3036),
    outline = Color(0xFF6C717C),
    outlineVariant = Color(0xFF2B2D33),
    error = Color(0xFFFF8A80),
    onError = Color(0xFF450A0A),
)

private val Type = Typography().let { t ->
    t.copy(
        displaySmall = t.displaySmall.copy(fontWeight = FontWeight.Bold, letterSpacing = (-0.5).sp),
        headlineLarge = t.headlineLarge.copy(fontWeight = FontWeight.Bold, letterSpacing = (-0.5).sp),
        headlineMedium = t.headlineMedium.copy(fontWeight = FontWeight.Bold, letterSpacing = (-0.25).sp),
        headlineSmall = t.headlineSmall.copy(fontWeight = FontWeight.SemiBold),
        titleLarge = t.titleLarge.copy(fontWeight = FontWeight.SemiBold),
        titleMedium = t.titleMedium.copy(fontWeight = FontWeight.SemiBold),
    )
}

@Composable
fun GusanTheme(content: @Composable () -> Unit) {
    MaterialTheme(colorScheme = if (isSystemInDarkTheme()) Dark else Light, typography = Type, content = content)
}

@Composable
fun recordColor(): Color = if (isSystemInDarkTheme()) Brand.RecordDark else Brand.Record

@Composable
fun successColor(): Color = if (isSystemInDarkTheme()) Brand.SuccessDark else Brand.Success

/** A white rounded block on the grey page: the one container the app uses. */
@Composable
fun Panel(modifier: Modifier = Modifier, padding: PaddingValues = PaddingValues(16.dp), content: @Composable ColumnScope.() -> Unit) {
    Surface(modifier = modifier.fillMaxWidth(), shape = RoundedCornerShape(20.dp), color = MaterialTheme.colorScheme.surfaceContainer) {
        Column(Modifier.padding(padding), verticalArrangement = Arrangement.spacedBy(8.dp), content = content)
    }
}

@Composable
fun BigButton(
    text: String,
    modifier: Modifier = Modifier,
    enabled: Boolean = true,
    color: Color = MaterialTheme.colorScheme.primary,
    contentColor: Color = MaterialTheme.colorScheme.onPrimary,
    onClick: () -> Unit,
) {
    Button(
        onClick = onClick,
        enabled = enabled,
        modifier = modifier.fillMaxWidth().heightIn(min = 56.dp),
        shape = RoundedCornerShape(28.dp),
        colors = ButtonDefaults.buttonColors(containerColor = color, contentColor = contentColor),
        contentPadding = PaddingValues(horizontal = 24.dp, vertical = 16.dp),
    ) { Text(text, style = MaterialTheme.typography.titleMedium) }
}

@Composable
fun PlanBadge(pro: Boolean) {
    val shape = RoundedCornerShape(50)
    if (pro) {
        Box(Modifier.background(Brush.horizontalGradient(listOf(Brand.ProStart, Brand.ProEnd)), shape).padding(horizontal = 10.dp, vertical = 3.dp)) {
            Text("PRO", color = Color.White, style = TextStyle(fontSize = 11.sp, fontWeight = FontWeight.Bold, letterSpacing = 0.8.sp))
        }
    } else {
        Box(Modifier.background(MaterialTheme.colorScheme.surfaceVariant, shape).padding(horizontal = 10.dp, vertical = 3.dp)) {
            Text("FREE", color = MaterialTheme.colorScheme.onSurfaceVariant, style = TextStyle(fontSize = 11.sp, fontWeight = FontWeight.Bold, letterSpacing = 0.8.sp))
        }
    }
}
