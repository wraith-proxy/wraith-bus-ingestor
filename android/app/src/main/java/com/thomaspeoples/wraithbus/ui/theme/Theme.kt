package com.thomaspeoples.wraithbus.ui.theme

import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.darkColorScheme
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable

private val LightColors = lightColorScheme(
    primary = Coral,
    onPrimary = Navy,
    secondary = Green,
    background = Cream,
    onBackground = Ink,
    surface = Cream,
    onSurface = Ink,
    onSurfaceVariant = Muted,
)

private val DarkColors = darkColorScheme(
    primary = Coral,
    onPrimary = Navy,
    secondary = Green,
    background = NavyDeep,
    onBackground = Cream,
    surface = Navy,
    onSurface = Cream,
    onSurfaceVariant = Muted,
)

@Composable
fun WraithBusTheme(content: @Composable () -> Unit) {
    val colorScheme = if (isSystemInDarkTheme()) DarkColors else LightColors
    MaterialTheme(
        colorScheme = colorScheme,
        typography = Typography,
        content = content,
    )
}
