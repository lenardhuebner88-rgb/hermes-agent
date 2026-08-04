package net.hermes.deck

import android.os.Bundle
import androidx.activity.ComponentActivity

/**
 * Entry point for Android's share sheet. Filled in once the capture pipeline
 * exists; declared from the start so the manifest surface stays stable.
 */
class ShareCaptureActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        finish()
    }
}
