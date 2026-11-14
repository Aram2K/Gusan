package com.aram2k.gusan.core

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.jsonObject

/**
 * The meeting notes prompt: how long the notes should be, and the words that ask for them.
 *
 * A port of the desktop's notes_prompt.py. The instruction text itself is not copied by hand: it is
 * exported from that file (scripts/export_notes_prompt.py -> gusan/notes_prompt.json), and
 * NotesPromptTest checks that whole prompts come out the same as the desktop's.
 */
object NotesPrompt {
    data class Plan(
        val tier: String,
        val words: Int,
        val summary: String,
        val discussion: String,
        val lo: Int,
        val hi: Int,
        val maxTokens: Int,
    )

    const val MAX_TOKENS_CEILING = 6000

    private val tiers = listOf("short" to 0, "medium" to 800, "long" to 3000, "very_long" to 10000)
    private val summaries = mapOf(
        "short" to "2-4 sentences: what the meeting was about and what came of it.",
        "medium" to "One paragraph of 4-6 sentences: what kind of meeting it was and who took part (when known), the main " +
            "points covered, and the outcome.",
        "long" to "Two short paragraphs (150-250 words in all): first what the meeting was, who took part (when known) and " +
            "what it set out to do; then what was covered and the outcome - what was decided, promised and left open.",
        "very_long" to "Two or three short paragraphs (250-350 words in all): what the meeting was and who took part (when " +
            "known); what was covered and how the discussion developed; the outcome - what was decided, promised " +
            "and left open. Someone who was not there should get the whole story from this section alone.",
    )
    private val topics = mapOf(
        "medium" to "3-6 topics, 1-3 bullets each",
        "long" to "5-10 topics, 2-4 bullets each",
        "very_long" to "8-14 topics (put minor ones together under 'Other points'), 2-4 bullets each",
    )
    private val narrative = listOf(
        0 to 0, 150 to 90, 400 to 170, 800 to 320, 2000 to 560, 3000 to 700, 6000 to 1000, 10000 to 1200, 20000 to 1400,
    )
    private const val LOW_SHARE = 0.55
    private val maxTokens = mapOf("short" to 1200, "medium" to 2200, "long" to 3600, "very_long" to 5000)
    private const val LATIN_LAST = 0x24F

    private class Exported(val instruction: String, val titleInstruction: String, val titleInputChars: Int)

    private val exported: Exported by lazy {
        val stream = NotesPrompt::class.java.getResourceAsStream("/gusan/notes_prompt.json")
            ?: error("gusan/notes_prompt.json is missing from the app")
        val o = Json.parseToJsonElement(stream.use { it.readBytes().toString(Charsets.UTF_8) }).jsonObject
        Exported(
            o.string("instruction")!!,
            o.string("title_instruction")!!,
            (o["title_input_chars"] as JsonPrimitive).content.toInt(),
        )
    }

    private fun isCjk(cp: Int): Boolean =
        cp in 0x3040..0x30FF || cp in 0x3400..0x4DBF || cp in 0x4E00..0x9FFF || cp in 0xF900..0xFAFF

    private fun countWords(text: String, skip: (Int) -> Boolean = { false }): Int {
        var words = 0
        var inWord = false
        var i = 0
        while (i < text.length) {
            val cp = text.codePointAt(i)
            i += Character.charCount(cp)
            if (isSpace(cp) || skip(cp)) inWord = false else if (!inWord) { inWord = true; words++ }
        }
        return words
    }

    // What Python's str.split() splits on.
    private fun isSpace(cp: Int): Boolean =
        Character.isWhitespace(cp) || Character.isSpaceChar(cp) || cp in 0x1C..0x1F || cp == 0x85

    /** The size of a text in English-like words (Chinese and Japanese: about 1.7 characters a word). */
    fun effectiveWords(text: String): Int {
        var cjk = 0
        var i = 0
        while (i < text.length) {
            val cp = text.codePointAt(i)
            i += Character.charCount(cp)
            if (isCjk(cp)) cjk++
        }
        if (cjk > 0) return (countWords(text, ::isCjk) + cjk / 1.7).toInt()
        return maxOf(countWords(text), text.codePointCount(0, text.length) / 6)
    }

    /** The share (0-1) of a text's letters that are not Latin: those need more room to be written in. */
    fun nonLatinShare(text: String): Double {
        var letters = 0
        var other = 0
        var seen = 0
        var i = 0
        while (i < text.length && seen < 60000) {
            val cp = text.codePointAt(i)
            i += Character.charCount(cp)
            seen++
            if (Character.isLetter(cp)) {
                letters++
                if (cp > LATIN_LAST) other++
            }
        }
        return if (letters == 0) 0.0 else other / letters.toDouble()
    }

    private fun narrativeMost(words: Int): Double {
        var prev = narrative[0]
        for (row in narrative.drop(1)) {
            if (words <= row.first) {
                return prev.second + (row.second - prev.second) * (words - prev.first) / (row.first - prev.first).toDouble()
            }
            prev = row
        }
        return narrative.last().second.toDouble()
    }

    private fun roundWords(n: Double): Int {
        val step = if (n >= 100) 10 else 5
        return Math.rint(n / step).toInt() * step
    }

    fun planFor(text: String): Plan = planFromWords(effectiveWords(text), nonLatinShare(text))

    fun planFromWords(words: Int, scriptShare: Double = 0.0): Plan {
        var tier = "short"
        for ((name, floor) in tiers) if (words >= floor) tier = name
        var lo = 0
        var hi = 0
        if (tier != "short") {
            val most = narrativeMost(words)
            hi = roundWords(most)
            lo = roundWords(most * LOW_SHARE)
        }
        val base = maxTokens.getValue(tier)
        val tokens = maxOf(base, minOf(MAX_TOKENS_CEILING, (base * (1.0 + scriptShare.coerceIn(0.0, 1.0))).toInt()))
        return Plan(tier, words, summaries.getValue(tier), topics[tier].orEmpty(), lo, hi, tokens)
    }

    private fun discussion(plan: Plan): String {
        if (plan.discussion.isEmpty()) return ""
        return "\n## Discussion\n" +
            "What was actually said, by topic, in the order it came up: ${plan.discussion}. Each topic is a line '### <short topic title>' " +
            "followed by bullets of one or two sentences (30 words at most) with the concrete details that matter: figures, " +
            "dates, names, options that were weighed, who held which position when speakers are labelled, the reasons behind " +
            "a decision, objections, next steps. Add the detail BEHIND the decisions and action items - do not repeat them " +
            "word for word. A topic with nothing concrete to report gets no heading. No nested bullets.\n"
    }

    private fun length(plan: Plan): String {
        if (plan.hi == 0) {
            val small = if (plan.words < 150) " It is very short: a sentence or two for the Summary." else ""
            return "this transcript is about ${plan.words} words. Keep the notes short, always shorter than the transcript, and never " +
                "pad them.$small Never leave out a decision, action item or open question to save space."
        }
        val prose = if (plan.discussion.isNotEmpty()) "Summary and the Discussion together" else "Summary"
        return "this transcript is about ${plan.words} words. The $prose should take about ${plan.lo}-${plan.hi} words (the decisions, action items and open " +
            "questions come on top: never leave one out to save space). Never pad, and never cut a topic short just to fit."
    }

    private fun languageRule(language: String): String =
        if (language.isNotEmpty()) {
            "- Write the notes in $language, the language of this meeting (a few greetings or phrases in another " +
                "language don't change that); the headings stay in English.\n"
        } else {
            "- Write the notes in the language the meeting was held in (the one used for most of the transcript, " +
                "when several were - a few greetings or phrases in another language don't count); the headings stay " +
                "in English.\n"
        }

    fun cloudInstruction(plan: Plan, language: String = ""): String =
        exported.instruction
            .replace("{{SUMMARY}}", plan.summary)
            .replace("{{DISCUSSION}}", discussion(plan))
            .replace("{{LENGTH}}", length(plan))
            .replace("{{LANGUAGE_RULE}}", languageRule(language))

    /** Said again after the transcript: on a long input the top of the prompt is far away. */
    fun closing(plan: Plan, language: String = ""): String {
        val sections = "## Summary, ## Key decisions, ## Action items, ## Open questions" +
            if (plan.discussion.isNotEmpty()) ", ## Discussion" else ""
        val size = if (plan.hi == 0) "" else
            " The ${if (plan.discussion.isNotEmpty()) "Summary and Discussion" else "Summary"} take about ${plan.lo}-${plan.hi} words together."
        return "Now write the notes ($sections) in ${language.ifEmpty { "the language of the meeting" }}; the headings stay in English, exactly as written.$size " +
            "Leave out a section with nothing in it. Owners are people's names or left out."
    }

    /** The whole user turn: the instruction, the transcript, the closing reminder. */
    fun cloudUserContent(text: String, language: String = ""): String {
        val t = pythonStrip(text)
        val plan = planFor(t)
        return "${cloudInstruction(plan, language)}\n\nText:\n$t\n\n${closing(plan, language)}"
    }

    private fun pythonStrip(text: String): String {
        var start = 0
        var end = text.length
        while (start < end && isSpace(text[start].code)) start++
        while (end > start && isSpace(text[end - 1].code)) end--
        return text.substring(start, end)
    }

    const val SYSTEM =
        "You are an articulate, intelligent executive assistant. You write with clarity, depth, and polish. " +
            "Never add conversational commentary, preambles, or explanations."

    fun messages(transcript: String, language: String = ""): List<ChatMessage> =
        listOf(ChatMessage("system", SYSTEM), ChatMessage("user", cloudUserContent(transcript, language)))

    fun titleMessages(notes: String): List<ChatMessage> = listOf(
        ChatMessage("system", SYSTEM),
        ChatMessage("user", "${exported.titleInstruction}\n\nText:\n${notes.trim().take(exported.titleInputChars)}"),
    )

    /**
     * An answer as the notes it holds: without a code fence around the whole of it or a line such as
     * "Here are the meeting notes:" in front of the first heading.
     */
    fun tidyAnswer(markdown: String): String {
        var lines = markdown.trim().split("\n")
        if (lines.size >= 2 && lines.first().trimStart().startsWith("```") && lines.last().trim() == "```") {
            lines = lines.subList(1, lines.size - 1)
        }
        val firstHeading = lines.indexOfFirst { it.trimStart().startsWith("#") }
        if (firstHeading in 1..3 && lines.take(firstHeading).all { it.isBlank() || it.trimEnd().endsWith(":") }) {
            lines = lines.drop(firstHeading)
        }
        return lines.joinToString("\n").trim()
    }

    private val titleLead = Regex("^(here(?:'s| is)|sure|okay|ok|title|suggested|proposed)\\b.*:\\s*$", RegexOption.IGNORE_CASE)
    private val titlePrefix = Regex("^\\s*(?:meeting\\s+)?(?:title|name)\\s*[:\\-–—]\\s*", RegexOption.IGNORE_CASE)
    private val trailingWords = setOf("and", "or", "of", "the", "to", "for", "with", "on", "in", "a", "an", "about", "at", "by", "from")

    /** What an engine answered, as a title - or "" when it is not one. */
    fun cleanTitle(raw: String): String {
        val lines = raw.replace("```", "\n").lines().map { it.trim() }.filter { it.isNotEmpty() && !titleLead.matches(it) }
        var s = lines.firstOrNull() ?: return ""
        s = titlePrefix.replace(s, "")
        s = s.replace(Regex("^[#>\\-*•\\s]+"), "").replace("**", "").replace("__", "").replace("`", "")
        s = s.trim(' ', '\t', '"', '\'', '“', '”', '‘', '’', '«', '»')
        s = s.split(Regex("\\s+")).filter { it.isNotEmpty() }.joinToString(" ")
        s = s.trimEnd('.', ':', ';', ',', '!', '…', '-', '–', '—', ' ')
        var words = s.split(" ").filter { it.isNotEmpty() }.take(10)
        s = words.joinToString(" ")
        if (s.length > 80) s = s.take(80).substringBeforeLast(" ")
        words = s.split(" ").filter { it.isNotEmpty() }
        while (words.isNotEmpty() && words.last().lowercase().trim(',', ';', ':') in trailingWords) words = words.dropLast(1)
        s = words.joinToString(" ").trimEnd('.', ',', ':', ';', '-', '–', '—', ' ')
        return if (s.length < 3) "" else s
    }
}

class MeetingNotesClient(private val transport: HttpTransport = UrlConnectionTransport()) {
    private val smart = SmartActionClient(transport)

    /** The notes of a meeting. noteRef names the meeting, so writing them again doesn't use another free note. */
    fun notes(transcript: String, language: String, noteRef: String, accessToken: String): String {
        if (transcript.isBlank()) throw GusanException(GusanException.Kind.NOTHING_HEARD, "Nothing was said in this recording.")
        val plan = NotesPrompt.planFor(transcript.trim())
        val request = smart.request(
            NotesPrompt.messages(transcript, language), "meeting_notes", plan.maxTokens, accessToken,
            extra = mapOf("note_ref" to noteRef), timeoutMs = 180_000,
            // Streamed, as on the desktop: a long meeting's notes take minutes, and a connection with
            // nothing coming down it for that long gets closed on the way.
            stream = true,
        )
        return NotesPrompt.tidyAnswer(SmartActionClient.parse(transport.sendOrThrow(request)))
    }

    /** A calendar-style name for the meeting, read off its notes. "" when the engine gave none. */
    fun title(notes: String, noteRef: String, accessToken: String): String {
        val request = smart.request(
            NotesPrompt.titleMessages(notes), "meeting_notes", 160, accessToken,
            extra = mapOf("note_ref" to noteRef), timeoutMs = 30_000,
        )
        return NotesPrompt.cleanTitle(SmartActionClient.parse(transport.sendOrThrow(request)))
    }
}
