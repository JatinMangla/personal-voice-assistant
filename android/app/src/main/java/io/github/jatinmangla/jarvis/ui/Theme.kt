package io.github.jatinmangla.jarvis.ui

import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.darkColorScheme
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.ui.graphics.Color

// The page's colours (src/static/index.html), so app and page look alike.
val Accent = Color(0xFF2563EB)
val Listen = Color(0xFFDC2626)
val Think = Color(0xFFD97706)
val Speak = Color(0xFF059669)

private val Light = lightColorScheme(
    primary = Accent, onPrimary = Color.White,
    background = Color(0xFFF6F7F9), onBackground = Color(0xFF16181D),
    surface = Color(0xFFF6F7F9), onSurface = Color(0xFF16181D),
    surfaceVariant = Color.White, onSurfaceVariant = Color(0xFF6B7280),
    outline = Color(0xFFE5E7EB), error = Listen,
)

private val Dark = darkColorScheme(
    primary = Accent, onPrimary = Color.White,
    background = Color(0xFF0F1115), onBackground = Color(0xFFECEEF2),
    surface = Color(0xFF0F1115), onSurface = Color(0xFFECEEF2),
    surfaceVariant = Color(0xFF181B21), onSurfaceVariant = Color(0xFF9AA1AD),
    outline = Color(0xFF262A33), error = Listen,
)

@Composable
fun JarvisTheme(content: @Composable () -> Unit) {
    MaterialTheme(colorScheme = if (isSystemInDarkTheme()) Dark else Light, content = content)
}
