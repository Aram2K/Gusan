package com.aram2k.gusan.core

import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class SmartActionTest {
    @Test fun thePromptsTheDesktopUsesAreAllThere() {
        assertEquals(setOf("smart_auto", "write_email", "make_todo_list", "translate", "summarize"), Prompts.modes)
    }

    @Test fun speechWithNothingSelectedUsesTheSmartPromptWithTheWordsLast() {
        val messages = Prompts.messages(SmartAction.Smart("  email anna the demo moves to thursday "))
        assertEquals("system", messages.first().role)
        assertEquals(ChatMessage("user", "email anna the demo moves to thursday"), messages.last())
        assertTrue(messages.size > 2)       // the examples in between
    }

    @Test fun speechOverSelectedTextSendsBothAsData() {
        val messages = Prompts.messages(SmartAction.VoiceOnText("make it formal", "hey \"you\"\n"))
        assertEquals(2, messages.size)
        assertEquals("""{"speech":"make it formal","text":"hey \"you\""}""", messages[1].content)
    }

    @Test fun nothingToWorkOnIsRefusedBeforeTheNetwork() {
        assertEquals(GusanException.Kind.EMPTY_INPUT, kindOf { Prompts.messages(SmartAction.Smart("  ")) })
        assertEquals(GusanException.Kind.EMPTY_INPUT, kindOf { Prompts.messages(SmartAction.VoiceOnText("go", " ")) })
        assertEquals(GusanException.Kind.EMPTY_INPUT, kindOf { Prompts.messages(SmartAction.Custom(" ", "text")) })
    }

    @Test fun aTypedInstructionWorksWithOrWithoutText() {
        assertEquals("Instruction: shorter\n\nText:\nhello", Prompts.messages(SmartAction.Custom("shorter", " hello ")).last().content)
        assertTrue(Prompts.messages(SmartAction.Custom("write a haiku", "")).last().content.endsWith("(No text yet - write it from the instruction.)"))
    }

    @Test fun requestBody() {
        val transport = FakeTransport { json(200, """{"text":"Hi Anna,","sources":[]}""") }
        val result = SmartActionClient(transport).run(SmartAction.Smart("email anna"), "tok")
        assertEquals(SmartResult("Hi Anna,", SmartResult.Kind.INSERT), result)
        val request = transport.requests.single()
        assertEquals(Backend.SMART_ACTION_URL, request.url)
        assertEquals("Bearer tok", request.headers["Authorization"])
        val body = request.json()
        assertEquals("smart_auto", body["mode"]!!.jsonPrimitive.content)
        assertEquals(2000, body["max_tokens"]!!.jsonPrimitive.content.toInt())
        assertEquals("email anna", body["messages"]!!.jsonArray.last().jsonObject["content"]!!.jsonPrimitive.content)
    }

    @Test fun anEditReplacesAndDictationInserts() {
        fun run(answer: String) = SmartActionClient.parseVoiceOnText(answer)
        assertEquals(SmartResult("Dear Sir,", SmartResult.Kind.EDIT), run("""{"kind":"edit","text":"Dear Sir,"}"""))
        assertEquals(SmartResult("See you at five.", SmartResult.Kind.INSERT), run("""{"kind":"insert","text":" See you at five. "}"""))
        assertEquals(SmartResult("x", SmartResult.Kind.EDIT), run("```json\n{\"kind\":\"edit\",\"text\":\"x\"}\n```"))
        // A model that forgot the JSON: its words are still usable.
        assertEquals(SmartResult("Just the words", SmartResult.Kind.INSERT), run("Just the words"))
        assertEquals(GusanException.Kind.FAILED, kindOf { run("""{"kind":"edit","text":""}""") })
    }

    @Test fun refusals() {
        fun kind(status: Int, body: String = "{}") = kindOf { SmartActionClient.parse(json(status, body)) }
        assertEquals(GusanException.Kind.UNAUTHORIZED, kind(401))
        assertEquals(GusanException.Kind.PRO_REQUIRED, kind(403))
        assertEquals(GusanException.Kind.PRO_REQUIRED, kind(402, """{"error":"pro_required"}"""))
        assertEquals(GusanException.Kind.ALLOWANCE_USED, kind(402, """{"error":"allowance_used","allowance":"meeting_notes"}"""))
        assertEquals(GusanException.Kind.TOO_LONG, kind(413))
        assertEquals(GusanException.Kind.DAILY_LIMIT, kind(429))
        assertEquals(GusanException.Kind.SERVER_NOT_CONFIGURED, kind(503))
        assertEquals(GusanException.Kind.FAILED, kind(502))
        assertEquals(GusanException.Kind.INVALID_RESPONSE, kind(200, "[]"))
    }

    @Test fun thinkingIsCutOff() {
        assertEquals("answer", SmartActionClient.stripThink("<think>hmm</think>\nanswer"))
        assertEquals("", SmartActionClient.stripThink("<think>never ends"))
        assertEquals("plain", SmartActionClient.stripThink(" plain "))
        assertEquals("answer", SmartActionClient.stripThink("thinking...</think> answer", templateOpened = true))
        // a closing tag quoted inside code is not the end of the thinking
        assertEquals("real", SmartActionClient.stripThink("a `</think>` b</think>real", templateOpened = true))
        assertEquals("keep </think> this", SmartActionClient.stripThink("keep </think> this"))
        assertEquals(GusanException.Kind.THOUGHT_ONLY, kindOf { SmartActionClient.parse(json(200, """{"text":"<think>only"}""")) })
        assertEquals("out", SmartActionClient.parse(json(200, """{"text":"x</think>out"}""", mapOf("x-answer-family" to "qwen"))))
    }

    @Test fun aStreamedAnswerIsPutTogether() {
        val body = "data: {\"choices\":[{\"delta\":{\"role\":\"assistant\"}}]}\n\n" +
            "data: {\"choices\":[{\"delta\":{\"content\":\"## Sum\"}}]}\n\n" +
            ": keep-alive\n\n" +
            "data: {\"choices\":[{\"delta\":{\"content\":\"mary\\nDone.\"}}]}\n\n" +
            "data: {\"choices\":[]}\n\ndata: [DONE]\n\n"
        assertEquals("## Summary\nDone.", SmartActionClient.parse(json(200, body, mapOf("Content-Type" to "text/event-stream"))))
    }
}
