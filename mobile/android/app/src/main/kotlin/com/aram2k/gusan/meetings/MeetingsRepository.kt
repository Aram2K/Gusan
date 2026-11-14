package com.aram2k.gusan.meetings

import com.aram2k.gusan.core.Meeting
import com.aram2k.gusan.core.MeetingStore
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

/** The meeting being recorded right now. */
data class LiveRecording(val id: String, val startedAtMs: Long, val level: Float = 0f)

/** How far a meeting's transcript is: done of total pieces (total 0 while the notes are written). */
data class Work(val id: String, val done: Int, val total: Int)

/** The meetings on this phone, and what is happening to them, for the screens to watch. */
class MeetingsRepository(val store: MeetingStore, private val scope: CoroutineScope) {
    private val _meetings = MutableStateFlow<List<Meeting>>(emptyList())
    val meetings: StateFlow<List<Meeting>> = _meetings

    val recording = MutableStateFlow<LiveRecording?>(null)
    val work = MutableStateFlow<Map<String, Work>>(emptyMap())

    init {
        scope.launch {
            withContext(Dispatchers.IO) { store.recoverInterrupted() }
            reload()
        }
    }

    suspend fun reload() {
        _meetings.value = withContext(Dispatchers.IO) { store.list() }
    }

    fun rename(id: String, title: String) {
        val name = title.trim()
        if (name.isEmpty()) return
        scope.launch {
            withContext(Dispatchers.IO) { store.load(id)?.let { store.save(it.copy(title = name, titled = true)) } }
            reload()
        }
    }

    fun delete(id: String) {
        scope.launch {
            withContext(Dispatchers.IO) { store.delete(id) }
            reload()
        }
    }
}
