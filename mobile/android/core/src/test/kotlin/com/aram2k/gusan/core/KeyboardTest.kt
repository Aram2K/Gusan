package com.aram2k.gusan.core

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import java.io.File

class KeyboardTest {
    @get:Rule val tmp = TemporaryFolder()

    @Test fun everyPageHasFourRowsOfTheSameWidth() {
        for (page in KeyboardPage.values()) for (switch in listOf(false, true)) {
            val rows = KeyboardLayout.rows(page, switch)
            assertEquals(4, rows.size)
            rows.forEach { row -> assertTrue("$page ${row.sumOf { it.weight.toDouble() }}", row.sumOf { it.weight.toDouble() } <= 10.0) }
            assertEquals(10.0, rows[0].sumOf { it.weight.toDouble() }, 0.0)
            assertEquals(10.0, rows[2].sumOf { it.weight.toDouble() }, 0.0)
            assertEquals(10.0, rows[3].sumOf { it.weight.toDouble() }, 0.0)
            assertEquals(switch, rows[3].any { it.kind == Key.Kind.SWITCH_KEYBOARD })
            assertTrue(rows.flatten().filter { it.kind == Key.Kind.CHAR }.all { it.label.length == 1 })
        }
        assertEquals("qwertyuiop", KeyboardLayout.rows(KeyboardPage.LETTERS)[0].joinToString("") { it.label })
    }

    @Test fun shift() {
        assertEquals(ShiftState.ONCE, ShiftState.OFF.tapped(false))
        assertEquals(ShiftState.OFF, ShiftState.ONCE.tapped(false))
        assertEquals(ShiftState.LOCKED, ShiftState.ONCE.tapped(true))
        assertEquals(ShiftState.OFF, ShiftState.LOCKED.tapped(true))
    }

    @Test fun twoSpacesMakeAPeriodOnlyAfterAWord() {
        assertTrue(TypingRules.doubleSpaceMakesPeriod("Hello "))
        assertTrue(TypingRules.doubleSpaceMakesPeriod("(done) "))
        for (before in listOf("", " ", "Hello", "Hello. ", "Hello  ", "wait, ")) assertFalse(before, TypingRules.doubleSpaceMakesPeriod(before))
    }

    @Test fun insertedWordsGetASpaceOnlyWhereOneBelongs() {
        assertEquals(" ", TypingRules.joiner("Hello", "world"))
        assertEquals(" ", TypingRules.joiner("Done.", "Next"))
        assertEquals("", TypingRules.joiner("Hello ", "world"))
        assertEquals("", TypingRules.joiner("line\n", "world"))
        assertEquals("", TypingRules.joiner("", "world"))
        assertEquals("", TypingRules.joiner("(", "world"))
        assertEquals("", TypingRules.joiner("Hello", ", world"))
        assertEquals("", TypingRules.joiner("Hello", ""))
    }

    @Test fun deleteTakesAWholeEmoji() {
        assertEquals(0, TypingRules.deleteLength(""))
        assertEquals(1, TypingRules.deleteLength("ab"))
        assertEquals(2, TypingRules.deleteLength("ok 😀"))
    }

    @Test fun undoIsOfferedWhileTheResultIsStillThere() {
        val undo = InsertUndo("Dear Anna,", "hey anna", atMs = 1_000)
        assertTrue(undo.available("To: x\nDear Anna,", 2_000))
        assertFalse(undo.available("Dear Anna, and more", 2_000))
        assertFalse(undo.available("Dear Anna,", 1_000 + InsertUndo.WINDOW_MS + 1))
        assertFalse(InsertUndo("", "x", 0).available("", 0))
    }

    @Test fun anErrorOffersTheStepThatFixesIt() {
        assertEquals(VoiceState.Action.OPEN_APP, VoiceState.failed(GusanException(GusanException.Kind.NOT_SIGNED_IN)).action)
        assertEquals(VoiceState.Action.SEE_PLANS, VoiceState.failed(GusanException(GusanException.Kind.PRO_REQUIRED, "Voice needs Gusan Pro.")).action)
        assertEquals("Voice needs Gusan Pro.", VoiceState.failed(GusanException(GusanException.Kind.PRO_REQUIRED, "Voice needs Gusan Pro.")).message)
        assertEquals(VoiceState.Action.NONE, VoiceState.failed(GusanException(GusanException.Kind.OFFLINE)).action)
    }

    @Test fun historyIsNewestFirstAndCapped() {
        val history = SmartHistory(File(tmp.root, "h/smart.json"), limit = 3)
        assertTrue(history.list().isEmpty())
        for (i in 1..5) history.add(SmartEntry("id$i", i.toLong(), "heard \"$i\"", "result\n$i"))
        assertEquals(listOf("id5", "id4", "id3"), history.list().map { it.id })
        assertEquals("result\n5", history.list().first().result)
        history.remove("id4")
        assertEquals(listOf("id5", "id3"), history.list().map { it.id })
        history.clear()
        assertTrue(history.list().isEmpty())
    }

    @Test fun statsCountThisWeeksWordsAndTheTimeSaved() {
        val day = 24 * 60 * 60 * 1000L
        val now = 100 * day
        val long = (1..400).joinToString(" ") { "word" }
        val entries = listOf(
            SmartEntry("a", now - day, "email anna about the demo", long),
            SmartEntry("b", now - 8 * day, "old", "one two three"),
        )
        val stats = SmartStats.of(entries, now)
        assertEquals(400, stats.wordsThisWeek)
        assertEquals(403, stats.wordsTotal)
        assertEquals(10, stats.minutesSavedThisWeek)     // 400 / 40 typed, minus 5 words spoken
        assertEquals(SmartStats(0, 0, 0), SmartStats.of(emptyList(), now))
    }

    @Test fun notesMarkdownBecomesBlocks() {
        val blocks = NotesMarkdown.parse(
            "## Summary\nThe team met\nand **agreed**.\n\n## Action items\n- [ ] Send the deck (Owner: Ani)\n- [x] Book a room\n" +
                "## Discussion\n### Pricing\n- Up **10%** in May\n  - nested\n1. numbered\n",
        )
        assertEquals(NoteBlock.Heading(2, listOf(Span("Summary"))), blocks[0])
        assertEquals(NoteBlock.Paragraph(listOf(Span("The team met and "), Span("agreed", true), Span("."))), blocks[1])
        assertEquals(NoteBlock.Bullet(listOf(Span("Send the deck (Owner: Ani)")), checked = false), blocks[3])
        assertEquals(NoteBlock.Bullet(listOf(Span("Book a room")), checked = true), blocks[4])
        assertEquals(NoteBlock.Heading(3, listOf(Span("Pricing"))), blocks[6])
        assertEquals(NoteBlock.Bullet(listOf(Span("Up "), Span("10%", true), Span(" in May"))), blocks[7])
        assertEquals(1, (blocks[8] as NoteBlock.Bullet).indent)
        assertEquals(NoteBlock.Bullet(listOf(Span("numbered"))), blocks[9])
        assertEquals(listOf(Span("2 ** 3")), NotesMarkdown.spans("2 ** 3"))
        assertEquals("SUMMARY\nShipped.\n☐ Call Ani", NotesMarkdown.plain("## Summary\nShipped.\n- [ ] Call **Ani**"))
    }
}
