package com.aram2k.gusan.core

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import java.security.MessageDigest

class NotesPromptTest {
    /** The prompt must be the desktop's, to the letter: notes_parity.json is written by notes_prompt.py itself. */
    @Test fun thePromptIsTheDesktopsForEverySample() {
        val samples = Json.parseToJsonElement(
            javaClass.getResourceAsStream("/gusan/notes_parity.json")!!.readBytes().toString(Charsets.UTF_8),
        ).jsonArray
        assertTrue(samples.size >= 10)
        for (item in samples) {
            val o = item.jsonObject
            fun s(key: String) = o[key]!!.jsonPrimitive.content
            val name = s("name") + "/" + s("language")
            val text = s("unit").repeat(s("count").toInt())
            val plan = NotesPrompt.planFor(text)
            assertEquals(name, s("tier"), plan.tier)
            assertEquals(name, s("words").toInt(), plan.words)
            assertEquals(name, s("lo").toInt(), plan.lo)
            assertEquals(name, s("hi").toInt(), plan.hi)
            assertEquals(name, s("max_tokens").toInt(), plan.maxTokens)
            val content = NotesPrompt.cloudUserContent(text, s("language"))
            assertEquals(name, s("head"), content.substringBefore("\n\nText:\n"))
            assertEquals(name, s("tail"), content.substringAfterLast("\n\n"))
            val sha = MessageDigest.getInstance("SHA-256").digest(content.toByteArray())
                .joinToString("") { "%02x".format(it) }
            assertEquals(name, s("sha256"), sha)
        }
    }

    @Test fun notesRequest() {
        val stream = "data: {\"choices\":[{\"delta\":{\"content\":\"Here are the notes:\\n\\n## Summary\\nAll good.\"}}]}\n\ndata: [DONE]\n"
        val transport = FakeTransport { json(200, stream, mapOf("Content-Type" to "text/event-stream")) }
        val notes = MeetingNotesClient(transport).notes("We agreed to ship on Friday.", "English", "m-1", "tok")
        assertEquals("## Summary\nAll good.", notes)
        val body = transport.requests.single().json()
        assertEquals("meeting_notes", body["mode"]!!.jsonPrimitive.content)
        assertEquals("m-1", body["note_ref"]!!.jsonPrimitive.content)
        assertEquals("true", body["stream"]!!.jsonPrimitive.content)
        assertEquals(1200, body["max_tokens"]!!.jsonPrimitive.content.toInt())
        val user = body["messages"]!!.jsonArray[1].jsonObject["content"]!!.jsonPrimitive.content
        assertTrue(user.contains("Write the notes in English, the language of this meeting"))
        assertTrue(user.contains("\n\nText:\nWe agreed to ship on Friday.\n\n"))
    }

    @Test fun anEmptyTranscriptIsNotSent() {
        val transport = FakeTransport { error("no request expected") }
        assertEquals(GusanException.Kind.NOTHING_HEARD, kindOf { MeetingNotesClient(transport).notes(" \n", "", "m", "t") })
    }

    @Test fun tidyAnswerTakesOffAFenceAndALeadIn() {
        assertEquals("## Summary\nx", NotesPrompt.tidyAnswer("```markdown\n## Summary\nx\n```"))
        assertEquals("## Summary\nx", NotesPrompt.tidyAnswer("Here are the meeting notes:\n\n## Summary\nx\n"))
        // A first paragraph that is the summary itself stays.
        assertEquals("The team met.\n\n## Key decisions\n- a", NotesPrompt.tidyAnswer("The team met.\n\n## Key decisions\n- a"))
    }

    @Test fun titles() {
        assertEquals("Q4 budget review", NotesPrompt.cleanTitle("Title: \"Q4 budget review.\""))
        assertEquals("Interview with BearingPoint", NotesPrompt.cleanTitle("Here is the title:\n**Interview with BearingPoint**"))
        assertEquals("Pricing call", NotesPrompt.cleanTitle("# Pricing call with the"))
        assertEquals("", NotesPrompt.cleanTitle("  \n"))
        assertEquals(10, NotesPrompt.cleanTitle("one two three four five six seven eight nine ten eleven twelve").split(" ").size)
        val transport = FakeTransport { json(200, """{"text":"Weekly sync with design"}""") }
        assertEquals("Weekly sync with design", MeetingNotesClient(transport).title("## Summary\nA sync.", "m-1", "t"))
        assertEquals("m-1", transport.requests.single().json()["note_ref"]!!.jsonPrimitive.content)
    }
}
