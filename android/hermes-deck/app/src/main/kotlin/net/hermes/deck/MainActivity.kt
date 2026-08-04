package net.hermes.deck

import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import net.hermes.deck.ui.HermesDeckTheme
import net.hermes.deck.ui.LocalDeck

class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()
        setContent {
            HermesDeckTheme {
                DeckRoot()
            }
        }
    }
}

@Composable
private fun DeckRoot() {
    val deck = LocalDeck.current
    Box(
        modifier = Modifier.fillMaxSize().background(deck.background),
        contentAlignment = Alignment.Center,
    ) {
        Text("Hermes Deck")
    }
}
