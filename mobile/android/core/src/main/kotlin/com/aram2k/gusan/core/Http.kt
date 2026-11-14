package com.aram2k.gusan.core

import java.io.IOException
import java.net.HttpURLConnection
import java.net.SocketTimeoutException
import java.net.URL
import java.net.UnknownHostException

class HttpRequest(
    val url: String,
    val headers: Map<String, String> = emptyMap(),
    val body: ByteArray? = null,
    val timeoutMs: Int = 15_000,
    val method: String = "POST",
)

class HttpResponse(val status: Int, val body: ByteArray, val headers: Map<String, String> = emptyMap()) {
    val text: String get() = body.toString(Charsets.UTF_8)
    fun header(name: String): String? = headers.entries.firstOrNull { it.key.equals(name, ignoreCase = true) }?.value
}

/** A blocking HTTP call. Callers run it off the main thread. */
fun interface HttpTransport {
    @Throws(IOException::class)
    fun send(request: HttpRequest): HttpResponse
}

class UrlConnectionTransport : HttpTransport {
    override fun send(request: HttpRequest): HttpResponse {
        val connection = URL(request.url).openConnection() as HttpURLConnection
        try {
            connection.requestMethod = request.method
            connection.connectTimeout = minOf(request.timeoutMs, 15_000)
            connection.readTimeout = request.timeoutMs
            connection.useCaches = false
            request.headers.forEach { (name, value) -> connection.setRequestProperty(name, value) }
            request.body?.let { body ->
                connection.doOutput = true
                connection.setFixedLengthStreamingMode(body.size)
                connection.outputStream.use { it.write(body) }
            }
            val status = connection.responseCode
            val stream = if (status >= 400) connection.errorStream else connection.inputStream
            val bytes = stream?.use { it.readBytes() } ?: ByteArray(0)
            val headers = connection.headerFields.orEmpty()
                .filterKeys { it != null }
                .mapValues { it.value.joinToString(",") }
            return HttpResponse(status, bytes, headers)
        } finally {
            connection.disconnect()
        }
    }
}

/** What went wrong, in words a person can act on. The same wording as the iPhone and desktop apps. */
class GusanException(val kind: Kind, message: String = kind.message) : Exception(message) {
    enum class Kind(val message: String) {
        EMPTY_INPUT("Enter some text first."),
        NOT_SIGNED_IN("Open the Gusan app and sign in."),
        UNAUTHORIZED("Sign in again in the Gusan app."),
        PRO_REQUIRED("This needs Gusan Pro."),
        ALLOWANCE_USED("You've used this month's free meeting note. Gusan Pro has no limit."),
        DAILY_LIMIT("Daily limit reached. It resets at midnight UTC."),
        SERVER_NOT_CONFIGURED("Gusan Cloud isn't available right now. Try again later."),
        TOO_LONG("That's too long. Try a shorter one."),
        NO_ENGINE("No transcription engine is available for this language. Try again later."),
        NOTHING_HEARD("Nothing heard. Try speaking again."),
        FAILED("Something went wrong. Try again."),
        OFFLINE("No internet connection."),
        NETWORK("Network error reaching Gusan Cloud."),
        TIMEOUT("The request timed out. Try again."),
        INVALID_RESPONSE("The server returned an invalid response."),
        THOUGHT_ONLY("The AI model spent its whole answer thinking and never answered. Ask again."),
        SIGN_IN_FAILED("Sign-in failed. Try again."),
    }
}

internal fun HttpTransport.sendOrThrow(request: HttpRequest): HttpResponse =
    try {
        send(request)
    } catch (e: SocketTimeoutException) {
        throw GusanException(GusanException.Kind.TIMEOUT)
    } catch (e: UnknownHostException) {
        throw GusanException(GusanException.Kind.OFFLINE)
    } catch (e: IOException) {
        throw GusanException(GusanException.Kind.NETWORK)
    }

object Backend {
    const val BASE_URL = "https://hftcelxzfoubheqeoool.supabase.co"
    const val PUBLISHABLE_KEY = "sb_publishable_aVAVHwDgUygeu_1QHyxg5w_3GGP_k5p"
    const val TRANSCRIBE_URL = "$BASE_URL/functions/v1/transcribe-proxy"
    const val SMART_ACTION_URL = "$BASE_URL/functions/v1/smart-action"
    const val AUTH_REDIRECT = "gusan://auth-callback"
    const val WEBSITE = "https://gusan.aibuben.xyz"
    const val PRICING = "https://gusan.aibuben.xyz/#pricing"
}
