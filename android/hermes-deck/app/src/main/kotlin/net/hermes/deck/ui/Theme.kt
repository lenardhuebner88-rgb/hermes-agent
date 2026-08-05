package net.hermes.deck.ui

import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Typography
import androidx.compose.material3.darkColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.runtime.CompositionLocalProvider
import androidx.compose.runtime.staticCompositionLocalOf
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp

/**
 * The deck's visual language: a near-black violet ground, glass cards with large
 * radii, one saturated accent, and a colour-coded edge per project.
 *
 * Material3's own colour roles are too coarse for the layered-glass look, so the
 * palette lives in [DeckColors] and is reached through [LocalDeck]. The M3 scheme
 * is still populated so stock components (ripples, text fields) look right.
 */
data class DeckColors(
    val background: Color = Color(0xFF0B0711),
    /** Card fill. Deliberately lighter than the ground, never pure grey. */
    val surface: Color = Color(0xFF17121F),
    val surfaceRaised: Color = Color(0xFF1E1729),
    /** Hairline that separates a card from the ground without a hard border. */
    val outline: Color = Color(0x1AFFFFFF),
    val accent: Color = Color(0xFF8B5CF6),
    val accentSoft: Color = Color(0xFF2A1F45),
    val textPrimary: Color = Color(0xFFECE9F1),
    val textSecondary: Color = Color(0xFF9B93AC),
    val textFaint: Color = Color(0xFF6B6479),
    val danger: Color = Color(0xFFF87171),
    val warning: Color = Color(0xFFFBBF24),
    val success: Color = Color(0xFF34D399),
    val info: Color = Color(0xFF38BDF8),
) {
    /**
     * The hero card's wash. Two stops only — a busier gradient reads as noise.
     * Deliberately deeper than the accent: on the device the brighter first
     * draft dominated the screen and flattened everything below it.
     */
    val heroBrush: Brush
        get() = Brush.linearGradient(listOf(Color(0xFF241046), Color(0xFF4C1D95)))

    val accentBrush: Brush
        get() = Brush.linearGradient(listOf(Color(0xFF8B5CF6), Color(0xFFA855F7)))

    /**
     * Stable per-project edge colours. Index by a hash of the project id so a
     * project keeps its colour across launches without storing anything.
     */
    val projectEdges: List<Color>
        get() = listOf(
            Color(0xFFF472B6),
            Color(0xFFFBBF24),
            Color(0xFF34D399),
            Color(0xFF38BDF8),
            Color(0xFFA78BFA),
            Color(0xFFFB923C),
            Color(0xFF22D3EE),
            Color(0xFFE879F9),
        )

    fun edgeFor(key: String): Color {
        val edges = projectEdges
        // Math.floorMod-style guard: key.hashCode() can be negative.
        val index = ((key.hashCode() % edges.size) + edges.size) % edges.size
        return edges[index]
    }
}

val LocalDeck = staticCompositionLocalOf { DeckColors() }

private val deckTypography: Typography
    get() {
        val base = Typography()
        return base.copy(
            headlineMedium = base.headlineMedium.copy(
                fontWeight = FontWeight.Bold,
                letterSpacing = (-0.5).sp,
            ),
            titleLarge = base.titleLarge.copy(fontWeight = FontWeight.SemiBold),
            titleMedium = base.titleMedium.copy(fontWeight = FontWeight.SemiBold),
            labelSmall = base.labelSmall.copy(letterSpacing = 0.8.sp),
        )
    }

@Composable
fun HermesDeckTheme(content: @Composable () -> Unit) {
    val deck = DeckColors()
    // The app is dark-only by design; the light scheme would fight the glass
    // treatment. isSystemInDarkTheme is read so the intent is explicit rather
    // than accidental.
    @Suppress("UNUSED_VARIABLE")
    val systemDark = isSystemInDarkTheme()

    val scheme = darkColorScheme(
        primary = deck.accent,
        onPrimary = Color.White,
        primaryContainer = deck.accentSoft,
        onPrimaryContainer = deck.textPrimary,
        background = deck.background,
        onBackground = deck.textPrimary,
        surface = deck.surface,
        onSurface = deck.textPrimary,
        surfaceVariant = deck.surfaceRaised,
        onSurfaceVariant = deck.textSecondary,
        outline = deck.outline,
        error = deck.danger,
    )

    CompositionLocalProvider(LocalDeck provides deck) {
        MaterialTheme(colorScheme = scheme, typography = deckTypography, content = content)
    }
}

/** Shared metrics so cards, sheets and the nav bar agree on their geometry. */
object DeckMetrics {
    val cardRadius = 24.dp
    val heroRadius = 28.dp
    val chipRadius = 14.dp
    val screenPadding = 20.dp
    val cardPadding = 16.dp
    val gap = 12.dp
}

val labelStyle = TextStyle(
    fontSize = 11.sp,
    fontWeight = FontWeight.SemiBold,
    letterSpacing = 1.sp,
)
