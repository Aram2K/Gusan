package com.aram2k.gusan.core

/** The little Markdown that meeting notes use, as blocks a screen can draw. */
sealed class NoteBlock {
    data class Heading(val level: Int, val text: List<Span>) : NoteBlock()
    data class Bullet(val text: List<Span>, val checked: Boolean? = null, val indent: Int = 0) : NoteBlock()
    data class Paragraph(val text: List<Span>) : NoteBlock()
}

data class Span(val text: String, val bold: Boolean = false)

object NotesMarkdown {
    private val heading = Regex("^(#{1,6})\\s+(.*?)\\s*#*$")
    private val bullet = Regex("^(\\s*)(?:[-*•]|\\d+[.)])\\s+(?:\\[([ xX])]\\s*)?(.*)$")

    fun parse(markdown: String): List<NoteBlock> {
        val blocks = ArrayList<NoteBlock>()
        val paragraph = ArrayList<String>()
        fun flush() {
            if (paragraph.isNotEmpty()) blocks.add(NoteBlock.Paragraph(spans(paragraph.joinToString(" "))))
            paragraph.clear()
        }
        for (raw in markdown.replace("\r\n", "\n").split("\n")) {
            val line = raw.trimEnd()
            if (line.isBlank()) { flush(); continue }
            val h = heading.find(line.trimStart())
            val b = bullet.find(line)
            when {
                h != null -> { flush(); blocks.add(NoteBlock.Heading(h.groupValues[1].length, spans(h.groupValues[2]))) }
                b != null -> {
                    flush()
                    val box = b.groupValues[2]
                    blocks.add(NoteBlock.Bullet(
                        spans(b.groupValues[3]),
                        checked = if (box.isEmpty()) null else box != " ",
                        indent = if (b.groupValues[1].length >= 2) 1 else 0,
                    ))
                }
                else -> paragraph.add(line.trim())
            }
        }
        flush()
        return blocks
    }

    /** "a **b** c" as plain and bold pieces. Stars without a partner stay as they are. */
    fun spans(text: String): List<Span> {
        val out = ArrayList<Span>()
        var rest = text
        while (true) {
            val open = rest.indexOf("**")
            val close = if (open < 0) -1 else rest.indexOf("**", open + 2)
            if (open < 0 || close < 0) break
            if (open > 0) out.add(Span(rest.substring(0, open)))
            if (close > open + 2) out.add(Span(rest.substring(open + 2, close), bold = true))
            rest = rest.substring(close + 2)
        }
        if (rest.isNotEmpty()) out.add(Span(rest))
        return out
    }

    /** The notes as plain text, for sharing into an app that doesn't read Markdown. */
    fun plain(markdown: String): String = parse(markdown).joinToString("\n") { block ->
        when (block) {
            is NoteBlock.Heading -> "\n" + block.text.joinToString("") { it.text }.uppercase()
            is NoteBlock.Bullet -> (if (block.indent > 0) "    " else "") +
                (when (block.checked) { null -> "• "; true -> "☑ "; false -> "☐ " }) + block.text.joinToString("") { it.text }
            is NoteBlock.Paragraph -> block.text.joinToString("") { it.text }
        }
    }.trim()
}
