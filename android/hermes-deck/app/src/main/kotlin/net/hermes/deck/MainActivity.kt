package net.hermes.deck

import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.activity.viewModels
import net.hermes.deck.ui.DeckApp
import net.hermes.deck.ui.HermesDeckTheme

class MainActivity : ComponentActivity() {

    private val viewModel: DeckViewModel by viewModels()

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()
        setContent {
            HermesDeckTheme {
                DeckApp(viewModel)
            }
        }
    }
}
