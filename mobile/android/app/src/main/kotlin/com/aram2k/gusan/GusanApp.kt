package com.aram2k.gusan

import android.app.Application
import android.content.Context
import com.aram2k.gusan.core.AuthClient
import com.aram2k.gusan.core.MeetingStore
import com.aram2k.gusan.core.SmartActionClient
import com.aram2k.gusan.core.SmartHistory
import com.aram2k.gusan.core.TranscribeClient
import com.aram2k.gusan.data.AccountRepository
import com.aram2k.gusan.data.SecureSessionStorage
import com.aram2k.gusan.data.Settings
import com.aram2k.gusan.meetings.MeetingsRepository
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import java.io.File

class GusanApp : Application() {
    lateinit var graph: AppGraph
        private set

    override fun onCreate() {
        super.onCreate()
        graph = AppGraph(this)
    }
}

/** Everything the app and the keyboard share. They run in one process, so they see the same sign-in. */
class AppGraph(context: Context) {
    val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main.immediate)
    val settings = Settings(context)
    val auth = AuthClient(SecureSessionStorage(context))
    val transcribe = TranscribeClient()
    val smart = SmartActionClient()
    val history = SmartHistory(File(context.filesDir, "smart/history.json"))
    val account = AccountRepository(auth, settings, scope)
    val meetings = MeetingsRepository(MeetingStore(File(context.filesDir, "meetings")), scope)
}

val Context.graph: AppGraph get() = (applicationContext as GusanApp).graph
