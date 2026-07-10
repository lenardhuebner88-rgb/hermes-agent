package net.hermes.dictate

import android.annotation.SuppressLint
import android.os.Bundle
import android.webkit.CookieManager
import android.webkit.RenderProcessGoneDetail
import android.webkit.WebResourceRequest
import android.webkit.WebResourceResponse
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.Toast
import androidx.activity.ComponentActivity
import java.io.ByteArrayInputStream
import java.util.concurrent.Executors

/**
 * One-time sign-in for the cloud opt-in path. The WebView is origin-locked to the Hermes host
 * (pattern proven in hermes-voice); the session cookie it establishes lives in the app-wide
 * CookieManager, which [WebViewCookieStore] shares with the native transcription client.
 * Closes itself as soon as a post-login page confirms an authenticated session.
 */
class LoginActivity : ComponentActivity() {

    private lateinit var webView: WebView
    private val probeExecutor = Executors.newSingleThreadExecutor()

    /** Set once a successful probe scheduled the close; stops duplicate probes/toasts. */
    private var closing = false

    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        webView = WebView(this)
        setContentView(webView)

        CookieManager.getInstance().setAcceptCookie(true)
        webView.settings.apply {
            javaScriptEnabled = true
            domStorageEnabled = true
        }
        webView.webViewClient = object : WebViewClient() {
            override fun shouldOverrideUrlLoading(
                view: WebView,
                request: WebResourceRequest,
            ): Boolean {
                val url = request.url
                // Origin lock: anything but the Hermes origin is refused, nothing external opens.
                return !DictateConfig.originIsAllowed(url.scheme, url.host, url.port)
            }

            override fun shouldInterceptRequest(
                view: WebView,
                request: WebResourceRequest,
            ): WebResourceResponse? {
                // shouldOverrideUrlLoading misses POST navigations and all subresource/fetch
                // loads — enforce the origin lock on EVERY request the WebView makes.
                // Accepted residual (Codex review 2026-07-10): this hook sees only the FIRST
                // URL of a redirect chain, so a same-origin subresource could still 3xx to an
                // external host. Exploiting that requires the self-hosted, operator-controlled
                // Hermes server itself to serve malicious redirects — at which point the
                // session backend is already compromised. Main-frame redirects stay blocked
                // via shouldOverrideUrlLoading; fully closing the gap would mean proxying all
                // traffic or disabling JS, which the fetch-based login form needs.
                val url = request.url
                if (DictateConfig.originIsAllowed(url.scheme, url.host, url.port)) return null
                return WebResourceResponse(
                    "text/plain",
                    "utf-8",
                    ByteArrayInputStream(ByteArray(0)),
                )
            }

            override fun onPageFinished(view: WebView, url: String) {
                // Once the gate redirects away from /login, verify the session and close.
                if (!closing && android.net.Uri.parse(url).path != "/login") probeAndFinish()
            }

            override fun onRenderProcessGone(
                view: WebView,
                detail: RenderProcessGoneDetail,
            ): Boolean {
                finish()
                return true
            }
        }
        webView.loadUrl(DictateConfig.LOGIN_URL)
    }

    override fun onDestroy() {
        probeExecutor.shutdownNow()
        webView.destroy()
        super.onDestroy()
    }

    private fun probeAndFinish() {
        if (probeExecutor.isShutdown) return
        probeExecutor.execute {
            if (SessionProbe.check() != true) return@execute
            runOnUiThread {
                if (closing || isFinishing || isDestroyed) return@runOnUiThread
                closing = true
                Toast.makeText(this, R.string.toast_signed_in, Toast.LENGTH_SHORT).show()
                finish()
            }
        }
    }
}
