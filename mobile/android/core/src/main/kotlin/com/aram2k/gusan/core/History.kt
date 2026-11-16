package com.aram2k.gusan.core

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonArray
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put
import java.io.File

data class SmartEntry(val id: String, val atMs: Long, val heard: String, val result: String)

/** The last things Gusan wrote from the keyboard, newest first. Kept on the phone only. */
class SmartHistory(private val file: File, private val limit: Int = 100) {

    @Synchronized
    fun list(): List<SmartEntry> {
        if (!file.exists()) return emptyList()
        val array = runCatching { Json.parseToJsonElement(file.readText()) as? JsonArray }.getOrNull() ?: return emptyList()
        return array.mapNotNull { item ->
            val o = item as? JsonObject ?: return@mapNotNull null
            SmartEntry(
                id = o.string("id") ?: return@mapNotNull null,
                atMs = (o["at_ms"] as? JsonPrimitive)?.content?.toLongOrNull() ?: 0L,
                heard = o.string("heard").orEmpty(),
                result = o.string("result").orEmpty(),
            )
        }
    }

    @Synchronized
    fun add(entry: SmartEntry) = write(listOf(entry) + list())

    @Synchronized
    fun remove(id: String) = write(list().filterNot { it.id == id })

    @Synchronized
    fun clear() { file.delete() }

    private fun write(entries: List<SmartEntry>) {
        file.parentFile?.mkdirs()
        file.writeText(buildJsonArray {
            entries.take(limit).forEach { e ->
                add(buildJsonObject { put("id", e.id); put("at_ms", e.atMs); put("heard", e.heard); put("result", e.result) })
            }
        }.toString())
    }
}

/** How much the keyboard's voice has done for someone: shown on Home. */
data class SmartStats(val wordsThisWeek: Int, val wordsTotal: Int, val minutesSavedThisWeek: Int) {
    companion object {
        const val TYPING_WPM = 40.0      // a typical phone typist
        const val SPEAKING_WPM = 150.0   // ordinary speech
        private const val WEEK_MS = 7L * 24 * 60 * 60 * 1000

        fun words(text: String): Int = text.split(Regex("\\s+")).count { it.isNotEmpty() }

        fun of(entries: List<SmartEntry>, nowMs: Long): SmartStats {
            val week = entries.filter { nowMs - it.atMs in 0 until WEEK_MS }
            val weekWords = week.sumOf { words(it.result) }
            val spokenWeek = week.sumOf { words(it.heard) }
            val saved = weekWords / TYPING_WPM - spokenWeek / SPEAKING_WPM
            return SmartStats(weekWords, entries.sumOf { words(it.result) }, maxOf(0, Math.round(saved).toInt()))
        }
    }
}
