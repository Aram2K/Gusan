package com.aram2k.gusan.ui

import androidx.compose.foundation.BorderStroke
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
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.material3.darkColorScheme
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp

/** The colors of the iPhone app (ios/DESIGN.md), so Gusan looks like Gusan on both phones. */
object Brand {
    val Red = Color(0xFFD91E18)
    val Blue = Color(0xFF0033A0)
    val ProStart = Color(0xFF9333EA)
    val ProEnd = Color(0xFF7E22CE)
    val Success = Color(0xFF16A34A)
    val SuccessDark = Color(0xFF22C55E)
}

private val Light = lightColorScheme(
    primary = Color(0xFF2563EB),
    onPrimary = Color.White,
    primaryContainer = Color(0xFFDCE7FD),
    onPrimaryContainer = Color(0xFF0B2A6B),
    secondary = Color(0xFF6B5B45),
    onSecondary = Color.White,
    background = Color(0xFFF7F1E6),
    onBackground = Color(0xFF1F1B16),
    surface = Color(0xFFFFFBF4),
    onSurface = Color(0xFF1F1B16),
    surfaceVariant = Color(0xFFEDE3D2),
    onSurfaceVariant = Color(0xFF5C5245),
    surfaceContainer = Color(0xFFF2EADB),
    surfaceContainerHigh = Color(0xFFEDE3D2),
    outline = Color(0xFF8A7F70),
    outlineVariant = Color(0xFFD9CDB9),
    error = Color(0xFFDC2626),
    onError = Color.White,
)

private val Dark = darkColorScheme(
    primary = Color(0xFF6EA0FA),
    onPrimary = Color(0xFF06204F),
    primaryContainer = Color(0xFF1E3A75),
    onPrimaryContainer = Color(0xFFDCE7FD),
    secondary = Color(0xFFD6C6AC),
    onSecondary = Color(0xFF2B241A),
    background = Color(0xFF1E1A15),
    onBackground = Color(0xFFF0E8DA),
    surface = Color(0xFF2B241A),
    onSurface = Color(0xFFF0E8DA),
    surfaceVariant = Color(0xFF3A3125),
    onSurfaceVariant = Color(0xFFCDBFA8),
    surfaceContainer = Color(0xFF2B241A),
    surfaceContainerHigh = Color(0xFF362D21),
    outline = Color(0xFF9A8D78),
    outlineVariant = Color(0xFF4A4033),
    error = Color(0xFFF87171),
    onError = Color(0xFF450A0A),
)

@Composable
fun GusanTheme(content: @Composable () -> Unit) {
    MaterialTheme(colorScheme = if (isSystemInDarkTheme()) Dark else Light, content = content)
}

@Composable
fun successColor(): Color = if (isSystemInDarkTheme()) Brand.SuccessDark else Brand.Success

@Composable
fun AppCard(modifier: Modifier = Modifier, content: @Composable ColumnScope.() -> Unit) {
    Card(
        modifier = modifier.fillMaxWidth(),
        shape = RoundedCornerShape(16.dp),
        colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surface),
        border = BorderStroke(1.dp, MaterialTheme.colorScheme.outlineVariant),
    ) {
        Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(10.dp), content = content)
    }
}

@Composable
fun PrimaryButton(text: String, modifier: Modifier = Modifier, enabled: Boolean = true, onClick: () -> Unit) {
    Button(
        onClick = onClick,
        enabled = enabled,
        modifier = modifier.fillMaxWidth().heightIn(min = 52.dp),
        shape = RoundedCornerShape(14.dp),
        colors = ButtonDefaults.buttonColors(),
        contentPadding = PaddingValues(horizontal = 20.dp, vertical = 14.dp),
    ) {
        Text(text, style = MaterialTheme.typography.titleMedium)
    }
}

@Composable
fun ScreenTitle(text: String, modifier: Modifier = Modifier) {
    Text(text, modifier = modifier, style = MaterialTheme.typography.headlineMedium, fontWeight = FontWeight.Bold)
}

@Composable
fun SectionLabel(text: String) {
    Text(
        text.uppercase(),
        style = MaterialTheme.typography.labelMedium,
        color = MaterialTheme.colorScheme.onSurfaceVariant,
        modifier = Modifier.padding(start = 4.dp, top = 8.dp),
    )
}

@Composable
fun PlanBadge(pro: Boolean) {
    val shape = RoundedCornerShape(50)
    if (pro) {
        Box(Modifier.background(Brush.horizontalGradient(listOf(Brand.ProStart, Brand.ProEnd)), shape).padding(horizontal = 10.dp, vertical = 3.dp)) {
            Text("Pro", color = Color.White, style = MaterialTheme.typography.labelMedium, fontWeight = FontWeight.Bold)
        }
    } else {
        Box(Modifier.background(MaterialTheme.colorScheme.surfaceVariant, shape).padding(horizontal = 10.dp, vertical = 3.dp)) {
            Text("Free", color = MaterialTheme.colorScheme.onSurfaceVariant, style = MaterialTheme.typography.labelMedium)
        }
    }
}
