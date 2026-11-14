package com.aram2k.gusan.core

import kotlinx.serialization.json.jsonPrimitive
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import java.nio.ByteBuffer
import java.nio.ByteOrder

class MeetingsTest {
    @get:Rule val tmp = TemporaryFolder()

    /** seconds of sound, with silence in [quietFrom, quietTo) seconds. */
    private fun pcm(seconds: Double, quietFrom: Double = -1.0, quietTo: Double = -1.0): ByteArray {
        val samples = (seconds * Audio.SAMPLE_RATE).toInt()
        val b = ByteBuffer.allocate(samples * 2).order(ByteOrder.LITTLE_ENDIAN)
        for (i in 0 until samples) {
            val t = i / Audio.SAMPLE_RATE.toDouble()
            b.putShort(if (t >= quietFrom && t < quietTo) 0 else if (i % 2 == 0) 9000 else -9000)
        }
        return b.array()
    }

    private val notesStream = "data: {\"choices\":[{\"delta\":{\"content\":\"## Summary\\nShipped.\"}}]}\n\ndata: [DONE]\n"

    private fun cloud(transcripts: MutableList<String>, fail: (HttpRequest) -> HttpResponse? = { null }) = FakeTransport { request ->
        fail(request) ?: when {
            request.url == Backend.TRANSCRIBE_URL -> json(200, """{"text":"${transcripts.removeAt(0)}"}""")
            request.json()["max_tokens"]!!.jsonPrimitive.content == "160" -> json(200, """{"text":"Title: Launch review"}""")
            else -> json(200, notesStream, mapOf("Content-Type" to "text/event-stream"))
        }
    }

    private fun processor(store: MeetingStore, transport: HttpTransport, pieceSeconds: Int = 10) = MeetingProcessor(
        store, { "tok" }, TranscribeClient(transport), MeetingNotesClient(transport),
        maxPieceBytes = pieceSeconds.toLong() * Audio.BYTES_PER_SECOND, searchBytes = 4 * Audio.BYTES_PER_SECOND,
    )

    @Test fun aShortRecordingIsOnePiece() {
        val audio = pcm(3.0)
        assertEquals(listOf(0L until audio.size.toLong()), MeetingChunker.pieces(audio.size.toLong()) { _, _ -> error("not read") })
    }

    @Test fun aLongRecordingIsCutInTheQuietAndNothingIsLostOrRepeated() {
        val audio = pcm(25.0, quietFrom = 8.0, quietTo = 8.6)
        val read = { offset: Long, length: Int -> audio.copyOfRange(offset.toInt(), offset.toInt() + length) }
        val pieces = MeetingChunker.pieces(audio.size.toLong(), 10L * Audio.BYTES_PER_SECOND, 4 * Audio.BYTES_PER_SECOND, read)
        assertEquals(0L, pieces.first().first)
        assertEquals(audio.size.toLong() - 1, pieces.last().last)
        pieces.zipWithNext { a, b -> assertEquals(a.last + 1, b.first) }
        pieces.forEach { assertTrue(it.last - it.first + 1 <= 10L * Audio.BYTES_PER_SECOND); assertEquals(0L, it.first % 2) }
        val firstCut = (pieces[0].last + 1) / Audio.BYTES_PER_SECOND.toDouble()
        assertTrue("cut at $firstCut s", firstCut in 8.0..8.6)
    }

    @Test fun storeKeepsMeetingsNewestFirst() {
        val store = MeetingStore(tmp.root)
        store.save(Meeting("a", "First", 100))
        store.save(Meeting("b", "Second \"quoted\"\nline", 200, notes = "## Summary\nx", titled = true))
        assertEquals(listOf("b", "a"), store.list().map { it.id })
        assertEquals("Second \"quoted\"\nline", store.load("b")!!.title)
        assertTrue(store.load("b")!!.titled)
        store.delete("b")
        assertNull(store.load("b"))
    }

    @Test fun aMeetingBecomesATranscriptNotesAndATitle() {
        val store = MeetingStore(tmp.root)
        store.save(Meeting("m", "Meeting", 1, language = "en"))
        store.audioFile("m").writeBytes(pcm(25.0, 8.0, 8.6))
        val transport = cloud(mutableListOf("one", "two", "three"))
        val seen = ArrayList<String>()
        val done = processor(store, transport).process("m") { m, d, t -> seen.add("${m.status} $d/$t") }!!
        assertEquals(done.error, Meeting.Status.DONE, done.status)
        assertEquals("one\n\ntwo\n\nthree", done.transcript)
        assertEquals("## Summary\nShipped.", done.notes)
        assertEquals("Launch review", done.title)
        assertEquals(done, store.load("m"))
        assertEquals("TRANSCRIBING 0/3", seen.first())
        assertTrue(seen.contains("WRITING_NOTES 0/0"))
        assertEquals("en", transport.requests.first().json()["language"]!!.jsonPrimitive.content)
    }

    @Test fun afterAFailureTryingAgainCarriesOnWhereItStopped() {
        val store = MeetingStore(tmp.root)
        store.save(Meeting("m", "Meeting", 1))
        store.audioFile("m").writeBytes(pcm(25.0, 8.0, 8.6))
        var calls = 0
        val failing = cloud(mutableListOf("one")) { r -> if (r.url == Backend.TRANSCRIBE_URL && ++calls == 2) json(429, "{}") else null }
        val failed = processor(store, failing).process("m")!!
        assertEquals(Meeting.Status.FAILED, failed.status)
        assertEquals("Daily voice limit reached. Try again tomorrow.", failed.error)

        val retry = cloud(mutableListOf("two", "three"))
        val done = processor(store, retry).process("m")!!
        assertEquals("one\n\ntwo\n\nthree", done.transcript)
        assertEquals(2, retry.requests.count { it.url == Backend.TRANSCRIBE_URL })
        assertEquals("", done.error)
    }

    @Test fun aSilentPieceDoesNotFailTheMeeting_butASilentMeetingSaysSo() {
        val store = MeetingStore(tmp.root)
        store.save(Meeting("m", "Meeting", 1))
        store.audioFile("m").writeBytes(pcm(25.0, 8.0, 8.6))
        val done = processor(store, cloud(mutableListOf("one", "", "three"))).process("m")!!
        assertEquals("one\n\nthree", done.transcript)

        store.save(Meeting("s", "Meeting", 2))
        store.audioFile("s").writeBytes(pcm(3.0))
        val silent = processor(store, cloud(mutableListOf(""))).process("s")!!
        assertEquals(Meeting.Status.FAILED, silent.status)
        assertEquals("Nothing was said in this recording.", silent.error)
    }

    @Test fun aNameThePersonGaveIsKept_andNotesSurviveATitleThatFails() {
        val store = MeetingStore(tmp.root)
        store.save(Meeting("m", "Board meeting", 1, titled = true))
        store.audioFile("m").writeBytes(pcm(3.0))
        val transport = cloud(mutableListOf("one"))
        assertEquals("Board meeting", processor(store, transport).process("m")!!.title)
        assertEquals(2, transport.requests.size)        // no title request

        store.save(Meeting("n", "Meeting", 2))
        store.audioFile("n").writeBytes(pcm(3.0))
        val noTitle = cloud(mutableListOf("one")) { r ->
            if (r.url != Backend.TRANSCRIBE_URL && r.json()["max_tokens"]!!.jsonPrimitive.content == "160") json(502, "{}") else null
        }
        val done = processor(store, noTitle).process("n")!!
        assertEquals(Meeting.Status.DONE, done.status)
        assertEquals("Meeting", done.title)
    }

    @Test fun theFreeNoteBeingUsedIsSaidInWords_andTheTranscriptIsKept() {
        val store = MeetingStore(tmp.root)
        store.save(Meeting("m", "Meeting", 1))
        store.audioFile("m").writeBytes(pcm(3.0))
        val transport = cloud(mutableListOf("one")) { r ->
            if (r.url == Backend.SMART_ACTION_URL) json(402, """{"error":"allowance_used","allowance":"meeting_notes"}""") else null
        }
        val failed = processor(store, transport).process("m")!!
        assertEquals(GusanException.Kind.ALLOWANCE_USED.message, failed.error)
        assertEquals("one", failed.transcript)
    }

    @Test fun cancellingLeavesARecordingThatCanBeFinishedLater() {
        val store = MeetingStore(tmp.root)
        store.save(Meeting("m", "Meeting", 1))
        store.audioFile("m").writeBytes(pcm(25.0, 8.0, 8.6))
        var asked = 0
        val stopped = processor(store, cloud(mutableListOf("one", "two", "three"))).process("m", cancelled = { ++asked > 1 })!!
        assertEquals(Meeting.Status.RECORDED, stopped.status)
        assertEquals("one", store.piece("m", 0))
    }

    @Test fun aMeetingCutShortByTheAppClosingCanBeFinished() {
        val store = MeetingStore(tmp.root)
        store.save(Meeting("m", "Meeting", 1, status = Meeting.Status.RECORDING))
        store.audioFile("m").writeBytes(pcm(4.0))
        store.save(Meeting("d", "Done", 2, status = Meeting.Status.DONE))
        assertEquals(1, store.recoverInterrupted())
        assertEquals(Meeting.Status.RECORDED, store.load("m")!!.status)
        assertEquals(4000L, store.load("m")!!.durationMs)
        assertEquals(Meeting.Status.DONE, store.load("d")!!.status)
    }
}
