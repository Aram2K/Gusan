package com.aram2k.gusan.audio

import android.Manifest
import android.annotation.SuppressLint
import android.content.Context
import android.content.pm.PackageManager
import android.media.AudioFormat
import android.media.AudioRecord
import android.media.MediaRecorder
import com.aram2k.gusan.core.Audio
import kotlin.concurrent.thread

/** The microphone as 16 kHz mono PCM, handed over in blocks of a tenth of a second. */
class MicRecorder(private val source: Int = MediaRecorder.AudioSource.VOICE_RECOGNITION) {
    @Volatile private var running = false
    private var worker: Thread? = null

    /**
     * Starts recording. onBlock gets each block (the array is reused: copy what you keep) and how loud
     * it was, on a background thread. onError is called if the microphone stops answering.
     * Returns false when the microphone can't be opened (another app has it, or no permission).
     */
    @SuppressLint("MissingPermission")
    fun start(context: Context, onBlock: (ByteArray, Int, Float) -> Unit, onError: () -> Unit = {}): Boolean {
        if (running) return true
        if (!hasPermission(context)) return false
        val minBuffer = AudioRecord.getMinBufferSize(Audio.SAMPLE_RATE, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT)
        if (minBuffer <= 0) return false
        val record = try {
            AudioRecord(source, Audio.SAMPLE_RATE, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT, maxOf(minBuffer, BLOCK * 4))
        } catch (e: IllegalArgumentException) {
            return false
        }
        if (record.state != AudioRecord.STATE_INITIALIZED) {
            record.release()
            return false
        }
        try {
            record.startRecording()
        } catch (e: IllegalStateException) {
            record.release()
            return false
        }
        if (record.recordingState != AudioRecord.RECORDSTATE_RECORDING) {
            record.release()
            return false
        }
        running = true
        worker = thread(name = "gusan-mic") {
            val block = ByteArray(BLOCK)
            var failed = false
            try {
                while (running) {
                    val read = record.read(block, 0, block.size)
                    if (read > 0) onBlock(block, read, Audio.level(block, read))
                    else if (read < 0) { failed = true; break }
                }
            } finally {
                runCatching { record.stop() }
                record.release()
                if (failed && running) { running = false; onError() }
            }
        }
        return true
    }

    /** Stops and waits until the last block has been handed over. */
    fun stop() {
        running = false
        worker?.let { if (it != Thread.currentThread()) it.join(1500) }
        worker = null
    }

    companion object {
        const val BLOCK = Audio.BYTES_PER_SECOND / 10

        fun hasPermission(context: Context): Boolean =
            context.checkSelfPermission(Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED
    }
}
