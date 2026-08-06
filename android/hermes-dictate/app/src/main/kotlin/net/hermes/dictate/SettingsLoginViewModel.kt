package net.hermes.dictate

import android.os.Handler
import android.os.Looper
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import androidx.lifecycle.ViewModel
import java.util.concurrent.Executors

/**
 * Hoists the cloud-login probe out of the view layer (defect 2 from the audit: the old screen
 * mutated a status TextView but never touched the login button, so "angemeldet ✓" and an active
 * "ANMELDEN" button could show at once). [status] is the single source of truth the composable
 * renders from — there is no second, view-owned copy of "is the button visible" to drift.
 *
 * Kept off `viewModelScope`/coroutines on purpose: [SessionProbe.check] is the same blocking
 * `HttpURLConnection` call the old Activity ran on its own executor, and mirroring that here
 * avoids pulling in a coroutines dependency for one network call.
 */
class SettingsLoginViewModel : ViewModel() {
    private val probeExecutor = Executors.newSingleThreadExecutor()
    private val mainHandler = Handler(Looper.getMainLooper())

    var status: LoginStatus by mutableStateOf(LoginStatus.Checking)
        private set

    /** Re-probes the session; call from `onResume` — the user comes back here from [LoginActivity]. */
    fun refresh() {
        status = LoginStatus.Checking
        if (probeExecutor.isShutdown) return
        probeExecutor.execute {
            val signedIn = SessionProbe.check()
            mainHandler.post { status = LoginStatus.fromProbe(signedIn) }
        }
    }

    override fun onCleared() {
        probeExecutor.shutdownNow()
        super.onCleared()
    }
}
