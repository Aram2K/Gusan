package com.aram2k.gusan.core

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.put
import java.io.File
import java.io.RandomAccessFile

data class Meeting(
    val id: String,
    val title: String,
    val createdAtMs: Long,
    val durationMs: Long = 0,
    val language: String = "auto",
    val status: Status = Status.RECORDED,
    val error: String = "",
    val notes: String = "",
    val transcript: String = "",
    /** True once the person named it: an automatic title never replaces theirs. */
    val titled: Boolean = false,
) {
    enum class Status { RECORDING, RECORDED, TRANSCRIBING, WRITING_NOTES, DONE, FAILED }

    val busy: Boolean get() = status == Status.TRANSCRIBING || status == Status.WRITING_NOTES

    fun toJson(): String = buildJsonObject {
        put("id", id); put("title", title); put("created_at_ms", createdAtMs); put("duration_ms", durationMs)
        put("language", language); put("status", status.name); put("error", error); put("notes", notes)
        put("transcript", transcript); put("titled", titled)
    }.toString()

    companion object {
        fun fromJson(text: String): Meeting? = runCatching {
            val o = Json.parseToJsonElement(text).jsonObject
            fun long(key: String) = (o[key] as? JsonPrimitive)?.content?.toLongOrNull() ?: 0L
            Meeting(
                id = o.string("id")!!,
                title = o.string("title").orEmpty(),
                createdAtMs = long("created_at_ms"),
                durationMs = long("duration_ms"),
                language = o.string("language") ?: "auto",
                status = runCatching { Status.valueOf(o.string("status").orEmpty()) }.getOrDefault(Status.RECORDED),
                error = o.string("error").orEmpty(),
                notes = o.string("notes").orEmpty(),
                transcript = o.string("transcript").orEmpty(),
                titled = (o["titled"] as? JsonPrimitive)?.content == "true",
            )
        }.getOrNull()
    }
}

/** Meetings on the phone: one folder each, holding meeting.json, audio.pcm and the pieces transcribed so far. */
class MeetingStore(private val root: File) {
    private fun dir(id: String) = File(root, id)
    fun audioFile(id: String) = File(dir(id), "audio.pcm")
    private fun pieceFile(id: String, index: Int) = File(dir(id), "piece-$index.txt")

    @Synchronized
    fun save(meeting: Meeting) {
        val dir = dir(meeting.id).apply { mkdirs() }
        val tmp = File(dir, "meeting.json.tmp")
        tmp.writeText(meeting.toJson())
        val target = File(dir, "meeting.json")
        if (!tmp.renameTo(target)) { target.delete(); tmp.renameTo(target) }
    }

    @Synchronized
    fun load(id: String): Meeting? =
        File(dir(id), "meeting.json").takeIf { it.exists() }?.let { Meeting.fromJson(it.readText()) }

    /** Newest first. */
    @Synchronized
    fun list(): List<Meeting> =
        root.listFiles().orEmpty().filter { it.isDirectory }.mapNotNull { load(it.name) }.sortedByDescending { it.createdAtMs }

    @Synchronized
    fun delete(id: String) { dir(id).deleteRecursively() }

    fun piece(id: String, index: Int): String? = pieceFile(id, index).takeIf { it.exists() }?.readText()
    fun savePiece(id: String, index: Int, text: String) = pieceFile(id, index).writeText(text)
    fun clearPieces(id: String) {
        dir(id).listFiles().orEmpty().filter { it.name.startsWith("piece-") }.forEach { it.delete() }
    }

    /**
     * After the app was closed in the middle of something: a meeting still marked as recording or
     * being written is set back to "recorded", so it can be finished from its page.
     */
    @Synchronized
    fun recoverInterrupted(): Int {
        var fixed = 0
        for (m in list()) {
            if (m.status == Meeting.Status.RECORDING || m.busy) {
                val seconds = audioFile(m.id).length() / Audio.BYTES_PER_SECOND
                save(m.copy(status = Meeting.Status.RECORDED, durationMs = maxOf(m.durationMs, seconds * 1000), error = ""))
                fixed++
            }
        }
        return fixed
    }
}

/**
 * Cuts a long recording into pieces Gusan Cloud accepts (it takes about 9 minutes of audio at once).
 * A piece ends at the quietest moment of its last 20 seconds, so a word is not cut in two.
 */
object MeetingChunker {
    const val MAX_PIECE_BYTES = 7L * 60 * Audio.BYTES_PER_SECOND
    const val SEARCH_BYTES = 20 * Audio.BYTES_PER_SECOND
    private const val WINDOW_BYTES = Audio.BYTES_PER_SECOND / 5      // 200 ms

    /** [start, end) byte ranges. read(offset, length) returns that part of the PCM. */
    fun pieces(
        totalBytes: Long,
        maxPieceBytes: Long = MAX_PIECE_BYTES,
        searchBytes: Int = SEARCH_BYTES,
        read: (offset: Long, length: Int) -> ByteArray,
    ): List<LongRange> {
        val total = totalBytes - totalBytes % 2
        val out = ArrayList<LongRange>()
        var start = 0L
        while (total - start > maxPieceBytes) {
            val searchFrom = start + maxPieceBytes - searchBytes
            val cut = searchFrom + quietest(read(searchFrom, searchBytes))
            out.add(start until cut)
            start = cut
        }
        if (total > start) out.add(start until total)
        return out
    }

    /**
     * The byte offset (even) of the middle of the quietest 200 ms in a block of PCM. When several
     * moments are as quiet as each other (or there is no pause at all) the last one wins: longer
     * pieces, fewer requests.
     */
    internal fun quietest(pcm: ByteArray): Int {
        if (pcm.size < WINDOW_BYTES * 2) return pcm.size - pcm.size % 2
        var best = Double.MAX_VALUE
        var bestAt = pcm.size - pcm.size % 2
        var at = 0
        while (at + WINDOW_BYTES <= pcm.size) {
            var sum = 0.0
            var i = at
            while (i < at + WINDOW_BYTES) {
                val s = (pcm[i].toInt() and 0xFF) or (pcm[i + 1].toInt() shl 8)
                sum += s.toDouble() * s
                i += 2
            }
            if (sum <= best) { best = sum; bestAt = at + WINDOW_BYTES / 2 }
            at += WINDOW_BYTES / 2
        }
        return bestAt - bestAt % 2
    }

    fun readFile(file: File): (Long, Int) -> ByteArray = { offset, length ->
        RandomAccessFile(file, "r").use { raf ->
            val bytes = ByteArray(minOf(length.toLong(), maxOf(0L, raf.length() - offset)).toInt())
            raf.seek(offset)
            raf.readFully(bytes)
            bytes
        }
    }
}

/** Turns a recorded meeting into a transcript, notes and a title. Blocking: run it off the main thread. */
class MeetingProcessor(
    private val store: MeetingStore,
    private val accessToken: () -> String,
    private val transcribe: TranscribeClient = TranscribeClient(),
    private val notes: MeetingNotesClient = MeetingNotesClient(),
    private val maxPieceBytes: Long = MeetingChunker.MAX_PIECE_BYTES,
    private val searchBytes: Int = MeetingChunker.SEARCH_BYTES,
) {
    /** done of total pieces are transcribed (total 0: writing the notes). */
    fun interface Progress { fun update(meeting: Meeting, done: Int, total: Int) }

    /**
     * Runs what is still missing. Pieces already transcribed are kept, so trying again after a failure
     * (no network, the daily limit) carries on where it stopped. Never throws: a failure is saved in the
     * meeting (status FAILED, error) and returned.
     */
    fun process(id: String, cancelled: () -> Boolean = { false }, progress: Progress = Progress { _, _, _ -> }): Meeting? {
        var meeting = store.load(id) ?: return null
        fun save(next: Meeting): Meeting { store.save(next); meeting = next; return next }
        try {
            if (meeting.transcript.isBlank()) {
                val audio = store.audioFile(id)
                if (audio.length() < Audio.BYTES_PER_SECOND / 2) {
                    throw GusanException(GusanException.Kind.NOTHING_HEARD, "This recording is too short to transcribe.")
                }
                val read = MeetingChunker.readFile(audio)
                val pieces = MeetingChunker.pieces(audio.length(), maxPieceBytes, searchBytes, read)
                save(meeting.copy(status = Meeting.Status.TRANSCRIBING, error = ""))
                val texts = ArrayList<String>()
                for ((index, range) in pieces.withIndex()) {
                    progress.update(meeting, index, pieces.size)
                    if (cancelled()) return save(meeting.copy(status = Meeting.Status.RECORDED))
                    val text = store.piece(id, index) ?: run {
                        val pcm = read(range.first, (range.last - range.first + 1).toInt())
                        val heard = try {
                            transcribe.run(Audio.wav(pcm), meeting.language, accessToken())
                        } catch (e: GusanException) {
                            // A piece of silence is not a failure of the meeting.
                            if (e.kind == GusanException.Kind.NOTHING_HEARD) "" else throw e
                        }
                        store.savePiece(id, index, heard)
                        heard
                    }
                    if (text.isNotBlank()) texts.add(text.trim())
                }
                val transcript = texts.joinToString("\n\n")
                if (transcript.isBlank()) {
                    throw GusanException(GusanException.Kind.NOTHING_HEARD, "Nothing was said in this recording.")
                }
                save(meeting.copy(transcript = transcript))
                progress.update(meeting, pieces.size, pieces.size)
            }
            if (cancelled()) return save(meeting.copy(status = Meeting.Status.RECORDED))
            if (meeting.notes.isBlank()) {
                save(meeting.copy(status = Meeting.Status.WRITING_NOTES, error = ""))
                progress.update(meeting, 0, 0)
                val language = SpeechLanguages.name(meeting.language)
                val written = notes.notes(meeting.transcript, language, id, accessToken())
                if (written.isBlank()) throw GusanException(GusanException.Kind.FAILED, "The notes came back empty. Try again.")
                save(meeting.copy(notes = written))
            }
            if (!meeting.titled) {
                // A name is a nicety: the notes are done whether or not one comes back.
                val title = runCatching { notes.title(meeting.notes, id, accessToken()) }.getOrDefault("")
                val latest = store.load(id) ?: meeting
                if (title.isNotEmpty() && !latest.titled) meeting = meeting.copy(title = title)
                else meeting = meeting.copy(title = latest.title, titled = latest.titled)
            }
            return save(meeting.copy(status = Meeting.Status.DONE, error = ""))
        } catch (e: GusanException) {
            return save(meeting.copy(status = Meeting.Status.FAILED, error = e.message ?: e.kind.message))
        } catch (e: Exception) {
            return save(meeting.copy(status = Meeting.Status.FAILED, error = GusanException.Kind.FAILED.message))
        }
    }
}
