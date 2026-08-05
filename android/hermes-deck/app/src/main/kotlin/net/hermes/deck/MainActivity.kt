package net.hermes.deck

import android.content.Intent
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
        offerSetupFrom(intent)
        setContent {
            HermesDeckTheme {
                DeckApp(viewModel)
            }
        }
    }

    /**
     * The activity is `singleTask`, so a setup link tapped while the deck is
     * already open arrives here instead of through [onCreate].
     */
    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        setIntent(intent)
        offerSetupFrom(intent)
    }

    private fun offerSetupFrom(intent: Intent?) {
        if (intent?.action != Intent.ACTION_VIEW) return
        val data = intent.data?.toString() ?: return
        // Clear it right away: the intent stays attached to the activity, so
        // without this the same link would be re-offered after every rotation.
        intent.data = null
        viewModel.offerSetup(data)
    }
}
