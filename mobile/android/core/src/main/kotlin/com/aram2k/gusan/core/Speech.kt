package com.aram2k.gusan.core

import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put
import java.io.ByteArrayOutputStream
import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.util.Base64

object Audio {
    const val SAMPLE_RATE = 16_000
    const val BYTES_PER_SECOND = SAMPLE_RATE * 2

    /** A WAV file (16-bit mono) around raw little-endian PCM. */
    fun wav(pcm: ByteArray, sampleRate: Int = SAMPLE_RATE): ByteArray {
        val header = ByteBuffer.allocate(44).order(ByteOrder.LITTLE_ENDIAN)
        header.put("RIFF".toByteArray(Charsets.US_ASCII)).putInt(36 + pcm.size)
        header.put("WAVEfmt ".toByteArray(Charsets.US_ASCII))
        header.putInt(16).putShort(1).putShort(1).putInt(sampleRate).putInt(sampleRate * 2).putShort(2).putShort(16)
        header.put("data".toByteArray(Charsets.US_ASCII)).putInt(pcm.size)
        return ByteArrayOutputStream(44 + pcm.size).apply { write(header.array()); write(pcm) }.toByteArray()
    }

    /** How loud a block of 16-bit PCM is, 0 (silence) to 1. */
    fun level(pcm: ByteArray, length: Int = pcm.size): Float {
        val samples = length / 2
        if (samples == 0) return 0f
        var sum = 0.0
        for (i in 0 until samples) {
            val s = (pcm[2 * i].toInt() and 0xFF) or (pcm[2 * i + 1].toInt() shl 8)
            sum += s.toDouble() * s
        }
        val rms = Math.sqrt(sum / samples) / 32768.0
        return Math.min(1.0, rms * 6).toFloat()
    }
}

object SpeechLanguages {
    val all: List<Pair<String, String>> = listOf(
        "auto" to "Detect automatically", "en" to "English", "hy" to "Armenian", "ru" to "Russian", "fr" to "French",
        "de" to "German", "es" to "Spanish", "it" to "Italian", "pt" to "Portuguese", "ar" to "Arabic",
    )

    fun normalize(code: String?): String {
        val c = code.orEmpty().trim().lowercase()
        return if (all.any { it.first == c }) c else "auto"
    }

    /** "English" for "en"; "" for auto. */
    fun name(code: String?): String = normalize(code).let { c -> if (c == "auto") "" else all.first { it.first == c }.second }
}

class TranscribeClient(private val transport: HttpTransport = UrlConnectionTransport()) {

    fun request(wav: ByteArray, language: String, accessToken: String): HttpRequest {
        if (wav.size <= 44) throw GusanException(GusanException.Kind.NOTHING_HEARD)
        val audio = Base64.getEncoder().encodeToString(wav)
        if (audio.length > MAX_AUDIO_B64) throw GusanException(GusanException.Kind.TOO_LONG)
        val lang = SpeechLanguages.normalize(language)
        val body = buildJsonObject {
            put("audio", audio)
            put("sample_rate", Audio.SAMPLE_RATE)
            put("language", lang)
            put("provider", "gemini")
            put("languages", JsonArray(if (lang == "auto") emptyList() else listOf(JsonPrimitive(lang))))
            put("model", "fast")
        }
        return HttpRequest(
            Backend.TRANSCRIBE_URL,
            mapOf("Authorization" to "Bearer $accessToken", "Content-Type" to "application/json"),
            body.toString().toByteArray(),
            timeoutMs = 90_000,
        )
    }

    fun run(wav: ByteArray, language: String, accessToken: String): String =
        parse(transport.sendOrThrow(request(wav, language, accessToken)))

    companion object {
        const val MAX_AUDIO_B64 = 24 * 1024 * 1024

        fun parse(response: HttpResponse): String {
            when (response.status) {
                200 -> Unit
                401 -> throw GusanException(GusanException.Kind.UNAUTHORIZED)
                403 -> throw GusanException(GusanException.Kind.PRO_REQUIRED, "Voice needs Gusan Pro.")
                413 -> throw GusanException(GusanException.Kind.TOO_LONG, "Recording is too long. Try a shorter one.")
                429 -> throw GusanException(GusanException.Kind.DAILY_LIMIT, "Daily voice limit reached. Try again tomorrow.")
                503 -> throw GusanException(GusanException.Kind.SERVER_NOT_CONFIGURED)
                502 -> {
                    val detail = parseObject(response.body)?.string("detail").orEmpty()
                    if (detail.contains("no engine", ignoreCase = true)) throw GusanException(GusanException.Kind.NO_ENGINE)
                    throw GusanException(GusanException.Kind.FAILED, "Transcription failed. Try again.")
                }
                else -> throw GusanException(GusanException.Kind.FAILED, "Transcription failed. Try again.")
            }
            val text = parseObject(response.body)?.string("text")
                ?: throw GusanException(GusanException.Kind.INVALID_RESPONSE)
            return text.trim().ifEmpty { throw GusanException(GusanException.Kind.NOTHING_HEARD) }
        }
    }
}
