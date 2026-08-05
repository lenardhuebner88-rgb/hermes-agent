package net.hermes.deck

import android.content.Intent
import android.net.Uri
import android.os.Build
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.activity.viewModels
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.material3.Text
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.sp
import kotlinx.coroutines.launch
import net.hermes.deck.model.Attachment
import net.hermes.deck.ui.CaptureSheet
import net.hermes.deck.ui.HermesDeckTheme
import net.hermes.deck.ui.LocalDeck

/**
 * Android's share sheet, wired straight into capture.
 *
 * This is the path that matters on a phone: a screenshot or a snippet from any
 * other app becomes a Buzz thread without opening the deck first. Shared images
 * are uploaded before the sheet is confirmed, because the sending app's URI
 * permission does not outlive this activity — reading it later would fail with
 * a security exception long after the user thought they were done.
 */
class ShareCaptureActivity : ComponentActivity() {

    private val viewModel: DeckViewModel by viewModels()

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()

        val sharedText = intent?.getStringExtra(Intent.EXTRA_TEXT).orEmpty()
        val subject = intent?.getStringExtra(Intent.EXTRA_SUBJECT).orEmpty()
        val uris = incomingUris()

        setContent {
            HermesDeckTheme {
                val deck = LocalDeck.current
                val hasIdentity by viewModel.hasIdentity.collectAsState()
                var attachments by remember { mutableStateOf<List<Attachment>>(emptyList()) }
                var uploading by remember { mutableStateOf(uris.isNotEmpty()) }
                var uploadError by remember { mutableStateOf<String?>(null) }

                LaunchedEffect(Unit) {
                    if (uris.isEmpty() || !hasIdentity) {
                        uploading = false
                        return@LaunchedEffect
                    }
                    launch {
                        val collected = ArrayList<Attachment>()
                        for (uri in uris) {
                            val mime = contentResolver.getType(uri) ?: "application/octet-stream"
                            val bytes = runCatching {
                                contentResolver.openInputStream(uri)?.use { it.readBytes() }
                            }.getOrNull()
                            if (bytes == null) {
                                uploadError = "Eine geteilte Datei war nicht lesbar."
                                continue
                            }
                            viewModel.uploadAttachment(bytes, mime, fileNameOf(uri))
                                .onSuccess { collected.add(it) }
                                .onFailure { uploadError = it.message ?: "Upload fehlgeschlagen" }
                        }
                        attachments = collected
                        uploading = false
                    }
                }

                Box(Modifier.fillMaxSize().background(deck.background)) {
                    if (!hasIdentity) {
                        Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
                            Text(
                                "Erst im Deck anmelden, dann klappt das Teilen.",
                                color = deck.textSecondary,
                                fontSize = 14.sp,
                            )
                        }
                        LaunchedEffect(Unit) {
                            viewModel.say("Erst im Deck anmelden.")
                        }
                    } else if (uploading) {
                        Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
                            Text("Anhang wird zu Buzz geladen …", color = deck.textSecondary, fontSize = 14.sp)
                        }
                    } else {
                        uploadError?.let { LaunchedEffect(it) { viewModel.say(it) } }
                        CaptureSheet(
                            viewModel = viewModel,
                            onDismiss = { finish() },
                            // A shared subject makes a better title than the body;
                            // otherwise the first line of the text becomes the title.
                            initialTitle = subject.ifBlank { sharedText.lineSequence().firstOrNull().orEmpty() },
                            initialBody = if (subject.isBlank()) {
                                sharedText.substringAfter('\n', "")
                            } else {
                                sharedText
                            },
                            initialAttachments = attachments,
                        )
                    }
                }
            }
        }
    }

    private fun incomingUris(): List<Uri> {
        val source = intent ?: return emptyList()
        return when (source.action) {
            Intent.ACTION_SEND -> listOfNotNull(extra(source, Intent.EXTRA_STREAM))
            Intent.ACTION_SEND_MULTIPLE -> extraList(source, Intent.EXTRA_STREAM)
            else -> emptyList()
        }
    }

    @Suppress("DEPRECATION")
    private fun extra(intent: Intent, name: String): Uri? =
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            intent.getParcelableExtra(name, Uri::class.java)
        } else {
            intent.getParcelableExtra(name)
        }

    @Suppress("DEPRECATION")
    private fun extraList(intent: Intent, name: String): List<Uri> =
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            intent.getParcelableArrayListExtra(name, Uri::class.java).orEmpty()
        } else {
            intent.getParcelableArrayListExtra<Uri>(name).orEmpty()
        }

    private fun fileNameOf(uri: Uri): String =
        uri.lastPathSegment?.substringAfterLast('/')?.takeIf { it.isNotBlank() } ?: "geteilt"
}
