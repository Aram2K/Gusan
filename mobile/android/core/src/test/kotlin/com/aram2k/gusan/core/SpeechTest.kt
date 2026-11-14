package com.aram2k.gusan.core

import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonPrimitive
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import java.nio.ByteBuffer
import java.nio.ByteOrder

class SpeechTest {
    @Test fun wavHeader() {
        val wav = Audio.wav(ByteArray(8) { it.toByte() })
        assertEquals(52, wav.size)
        val b = ByteBuffer.wrap(wav).order(ByteOrder.LITTLE_ENDIAN)
        assertEquals("RIFF", String(wav, 0, 4))
        assertEquals(44, b.getInt(4))
        assertEquals("WAVEfmt ", String(wav, 8, 8))
        assertEquals(1, b.getShort(20).toInt())      // PCM
        assertEquals(1, b.getShort(22).toInt())      // mono
        assertEquals(16000, b.getInt(24))
        assertEquals(32000, b.getInt(28))
        assertEquals(16, b.getShort(34).toInt())
        assertEquals(8, b.getInt(40))
        assertEquals(7, wav.last().toInt())
    }

    @Test fun levelIsZeroForSilenceAndRisesWithSound() {
        assertEquals(0f, Audio.level(ByteArray(3200)), 0f)
        val loud = ByteBuffer.allocate(3200).order(ByteOrder.LITTLE_ENDIAN)
        repeat(1600) { loud.putShort(if (it % 2 == 0) 12000 else -12000) }
        assertTrue(Audio.level(loud.array()) > 0.9f)
        assertEquals(0f, Audio.level(ByteArray(0)), 0f)
    }

    @Test fun requestNamesTheLanguageOnlyWhenOneIsSet() {
        val client = TranscribeClient(FakeTransport { json(200, """{"text":" hello "}""") })
        val auto = client.request(Audio.wav(ByteArray(100)), "xx", "tok").json()
        assertEquals("auto", auto["language"]!!.jsonPrimitive.content)
        assertEquals(0, auto["languages"]!!.jsonArray.size)
        val hy = client.request(Audio.wav(ByteArray(100)), " HY ", "tok").json()
        assertEquals("hy", hy["language"]!!.jsonPrimitive.content)
        assertEquals("hy", hy["languages"]!!.jsonArray.single().jsonPrimitive.content)
        assertEquals("hello", client.run(Audio.wav(ByteArray(100)), "auto", "tok"))
    }

    @Test fun anEmptyRecordingIsNotSent() {
        val transport = FakeTransport { error("no request expected") }
        assertEquals(GusanException.Kind.NOTHING_HEARD, kindOf { TranscribeClient(transport).run(Audio.wav(ByteArray(0)), "auto", "t") })
    }

    @Test fun everyRefusalHasItsOwnWords() {
        fun kind(status: Int, body: String = "{}") = kindOf { TranscribeClient.parse(json(status, body)) }
        assertEquals(GusanException.Kind.UNAUTHORIZED, kind(401))
        assertEquals(GusanException.Kind.PRO_REQUIRED, kind(403))
        assertEquals(GusanException.Kind.TOO_LONG, kind(413))
        assertEquals(GusanException.Kind.DAILY_LIMIT, kind(429))
        assertEquals(GusanException.Kind.SERVER_NOT_CONFIGURED, kind(503))
        assertEquals(GusanException.Kind.NO_ENGINE, kind(502, """{"error":"stt_failed","detail":"no engine for this language"}"""))
        assertEquals(GusanException.Kind.FAILED, kind(502, """{"error":"stt_failed","detail":"boom"}"""))
        assertEquals(GusanException.Kind.NOTHING_HEARD, kind(200, """{"text":"  "}"""))
        assertEquals(GusanException.Kind.INVALID_RESPONSE, kind(200, "<html>"))
    }

    @Test fun networkTroubleIsToldApart() {
        fun kind(e: java.io.IOException) = kindOf {
            TranscribeClient(FakeTransport { throw e }).run(Audio.wav(ByteArray(100)), "auto", "t")
        }
        assertEquals(GusanException.Kind.TIMEOUT, kind(java.net.SocketTimeoutException()))
        assertEquals(GusanException.Kind.OFFLINE, kind(java.net.UnknownHostException()))
        assertEquals(GusanException.Kind.NETWORK, kind(java.io.IOException()))
    }
}
