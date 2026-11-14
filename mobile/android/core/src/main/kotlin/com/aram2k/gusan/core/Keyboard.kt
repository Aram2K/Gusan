package com.aram2k.gusan.core

/** One key of the keyboard. weight is its width in letter-key widths. */
data class Key(val kind: Kind, val label: String = "", val weight: Float = 1f) {
    enum class Kind { CHAR, SHIFT, DELETE, NUMBERS, SYMBOLS, LETTERS, SPACE, RETURN, SWITCH_KEYBOARD }
}

enum class KeyboardPage { LETTERS, NUMBERS, SYMBOLS }

object KeyboardLayout {
    private fun chars(text: String) = text.split(" ").map { Key(Key.Kind.CHAR, it) }

    private fun bottom(left: Key, showSwitch: Boolean): List<Key> = buildList {
        add(left)
        add(Key(Key.Kind.CHAR, ","))
        if (showSwitch) add(Key(Key.Kind.SWITCH_KEYBOARD))
        add(Key(Key.Kind.SPACE, weight = if (showSwitch) 4f else 5f))
        add(Key(Key.Kind.CHAR, "."))
        add(Key(Key.Kind.RETURN, weight = 1.5f))
    }

    /** The rows of a page, top to bottom. showSwitch adds the key that changes to another keyboard. */
    fun rows(page: KeyboardPage, showSwitch: Boolean = false): List<List<Key>> = when (page) {
        KeyboardPage.LETTERS -> listOf(
            chars("q w e r t y u i o p"),
            chars("a s d f g h j k l"),
            listOf(Key(Key.Kind.SHIFT, weight = 1.5f)) + chars("z x c v b n m") + Key(Key.Kind.DELETE, weight = 1.5f),
            bottom(Key(Key.Kind.NUMBERS, "?123", 1.5f), showSwitch),
        )
        KeyboardPage.NUMBERS -> listOf(
            chars("1 2 3 4 5 6 7 8 9 0"),
            chars("@ # $ _ & - + ( ) /"),
            listOf(Key(Key.Kind.SYMBOLS, "=\\<", 1.5f)) + chars("* \" ' : ; ! ?") + Key(Key.Kind.DELETE, weight = 1.5f),
            bottom(Key(Key.Kind.LETTERS, "ABC", 1.5f), showSwitch),
        )
        KeyboardPage.SYMBOLS -> listOf(
            chars("~ ` | • √ π ÷ × ¶ ∆"),
            chars("£ ¢ € ¥ ^ ° = { } \\"),
            listOf(Key(Key.Kind.NUMBERS, "?123", 1.5f)) + chars("% © ® ™ ✓ [ ]") + Key(Key.Kind.DELETE, weight = 1.5f),
            bottom(Key(Key.Kind.LETTERS, "ABC", 1.5f), showSwitch),
        )
    }
}

enum class ShiftState {
    OFF, ONCE, LOCKED;

    /** After a tap on shift. A second tap soon after the first locks it. */
    fun tapped(doubleTap: Boolean): ShiftState = when {
        this == LOCKED -> OFF
        doubleTap && this == ONCE -> LOCKED
        this == ONCE -> OFF
        else -> ONCE
    }

    val on: Boolean get() = this != OFF
}

object TypingRules {
    /** Space typed right after "word " turns into ". ": true when the text in front of the cursor allows it. */
    fun doubleSpaceMakesPeriod(before: String): Boolean {
        if (before.length < 2 || before.last() != ' ') return false
        val prev = before[before.length - 2]
        return prev.isLetterOrDigit() || prev in ")]}\"'”’"
    }

    /**
     * What goes between the text in front of the cursor and words Gusan is about to insert: a space
     * after a word or the end of a sentence, nothing at the start of a line or after an opening bracket.
     */
    fun joiner(before: String, inserted: String): String {
        if (before.isEmpty() || inserted.isEmpty()) return ""
        val last = before.last()
        val first = inserted.first()
        if (last.isWhitespace() || first.isWhitespace()) return ""
        if (last in "([{“‘\"'/-–—@#") return ""
        if (first in ".,;:!?)]}…։") return ""
        return " "
    }

    /** How many UTF-16 units one press of delete removes: a whole character, never half of an emoji. */
    fun deleteLength(before: String): Int {
        if (before.isEmpty()) return 0
        val cp = before.codePointBefore(before.length)
        return Character.charCount(cp)
    }
}

/**
 * A result the keyboard put into a text field, and how to take it out again.
 * Undo is offered while the text in front of the cursor still ends with what was inserted.
 */
data class InsertUndo(val inserted: String, val original: String, val atMs: Long) {
    fun available(before: String, nowMs: Long): Boolean =
        inserted.isNotEmpty() && nowMs - atMs <= WINDOW_MS && before.endsWith(inserted)

    companion object {
        const val WINDOW_MS = 30_000L
    }
}

/** Where the keyboard's voice is: what the strip above the keys shows. */
sealed class VoiceState {
    object Idle : VoiceState()
    data class Listening(val startedAtMs: Long) : VoiceState()
    data class Working(val heard: String) : VoiceState()
    data class Done(val undo: InsertUndo) : VoiceState()
    data class Failed(val message: String, val action: Action = Action.NONE) : VoiceState()

    /** The button an error offers. */
    enum class Action { NONE, OPEN_APP, SEE_PLANS }

    companion object {
        fun failed(e: GusanException): Failed = Failed(
            e.message ?: e.kind.message,
            when (e.kind) {
                GusanException.Kind.NOT_SIGNED_IN, GusanException.Kind.UNAUTHORIZED -> Action.OPEN_APP
                GusanException.Kind.PRO_REQUIRED -> Action.SEE_PLANS
                else -> Action.NONE
            },
        )
    }
}

/** The longest the keyboard listens in one go: what one request to Gusan Cloud can carry, with room to spare. */
const val MAX_DICTATION_MS = 5 * 60_000L
