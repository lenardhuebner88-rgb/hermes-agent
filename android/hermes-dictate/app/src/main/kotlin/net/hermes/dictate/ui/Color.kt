package net.hermes.dictate.ui

import androidx.compose.ui.graphics.Color

/**
 * The app's existing identity, lifted verbatim from `res/values/colors.xml` (that file is owned
 * by a parallel builder and is not touched here). Kept as plain [Color] constants — Compose
 * cannot reference `@color/…` resources without an XML round-trip, and the values are simple
 * enough that duplicating them here is cheaper than that indirection.
 */
val DictateSlate = Color(0xFF10131A)
val DictateSurfaceRaised = Color(0xFF161B26)
val DictateBorder = Color(0xFF334155)
val DictateTextPrimary = Color(0xFFE8ECF4)
val DictateTextDim = Color(0xFF8B94A7)
val DictateAccent = Color(0xFF8B7CF6)
val DictateSuccess = Color(0xFF4ADE80)
val DictateError = Color(0xFFF87171)
val DictateCloudCyan = Color(0xFF38BDF8)
