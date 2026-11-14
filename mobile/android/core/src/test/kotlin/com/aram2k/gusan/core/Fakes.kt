package com.aram2k.gusan.core

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.jsonObject

/** Answers requests from a script and remembers what was asked. */
class FakeTransport(private val answer: (HttpRequest) -> HttpResponse) : HttpTransport {
    val requests = ArrayList<HttpRequest>()
    override fun send(request: HttpRequest): HttpResponse {
        requests.add(request)
        return answer(request)
    }
}

fun json(status: Int, body: String, headers: Map<String, String> = emptyMap()) =
    HttpResponse(status, body.toByteArray(), headers)

fun HttpRequest.json(): JsonObject = Json.parseToJsonElement(body!!.toString(Charsets.UTF_8)).jsonObject

fun kindOf(block: () -> Unit): GusanException.Kind? =
    try { block(); null } catch (e: GusanException) { e.kind }
