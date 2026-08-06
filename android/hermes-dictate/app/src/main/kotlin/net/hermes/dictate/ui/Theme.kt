package net.hermes.dictate.ui

import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Typography
import androidx.compose.material3.darkColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.runtime.CompositionLocalProvider
import androidx.compose.runtime.staticCompositionLocalOf
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp

/**
 * The settings screen's palette, analog to `hermes-deck`'s [net.hermes.deck.ui.DeckColors]: the
 * app's identity lives here as named roles rather than as raw Material colour-scheme slots, and
 * is reached through [LocalDictate]. The dark-only M3 scheme in [DictateTheme] is still populated
 * so stock components (ripples, segmented buttons, chips) pick up the right tint.
 */
data class DictateColors(
    val background: Color = DictateSlate,
    val surface: Color = DictateSurfaceRaised,
    val border: Color = DictateBorder,
    val textPrimary: Color = DictateTextPrimary,
    val textDim: Color = DictateTextDim,
    val accent: Color = DictateAccent,
    val success: Color = DictateSuccess,
    val error: Color = DictateError,
    val cloud: Color = DictateCloudCyan,
)

val LocalDictate = staticCompositionLocalOf { DictateColors() }

private val dictateTypography: Typography
    get() {
        val base = Typography()
        return base.copy(
            titleLarge = base.titleLarge.copy(fontWeight = FontWeight.Bold),
            titleMedium = base.titleMedium.copy(fontWeight = FontWeight.SemiBold, fontSize = 15.sp),
            labelSmall = base.labelSmall.copy(letterSpacing = 0.8.sp),
        )
    }

@Composable
fun DictateTheme(content: @Composable () -> Unit) {
    val dictate = DictateColors()
    val scheme = darkColorScheme(
        primary = dictate.accent,
        onPrimary = dictate.background,
        background = dictate.background,
        onBackground = dictate.textPrimary,
        surface = dictate.surface,
        onSurface = dictate.textPrimary,
        surfaceVariant = dictate.surface,
        onSurfaceVariant = dictate.textDim,
        outline = dictate.border,
        error = dictate.error,
    )
    CompositionLocalProvider(LocalDictate provides dictate) {
        MaterialTheme(colorScheme = scheme, typography = dictateTypography, content = content)
    }
}

/** Shared metrics so every card on the screen agrees on radius and rhythm. */
object DictateMetrics {
    val cardRadius = 20.dp
    val screenPadding = 20.dp
    val cardPadding = 16.dp
    val sectionGap = 20.dp
    val rowGap = 12.dp
}
