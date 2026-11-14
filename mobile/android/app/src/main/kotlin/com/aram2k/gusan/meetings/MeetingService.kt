package com.aram2k.gusan.meetings

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.media.MediaRecorder
import android.os.Build
import android.os.IBinder
import android.os.PowerManager
import androidx.core.app.NotificationCompat
import androidx.core.app.ServiceCompat
import com.aram2k.gusan.R
import com.aram2k.gusan.audio.MicRecorder
import com.aram2k.gusan.core.Audio
import com.aram2k.gusan.core.GusanException
import com.aram2k.gusan.core.Meeting
import com.aram2k.gusan.core.MeetingNotesClient
import com.aram2k.gusan.core.MeetingProcessor
import com.aram2k.gusan.graph
import com.aram2k.gusan.ui.MainActivity
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.io.BufferedOutputStream
import java.io.FileOutputStream
import java.io.OutputStream
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import java.util.UUID

/**
 * Records a meeting and then writes its transcript and notes. It runs as a foreground service, so
 * the recording carries on with the screen off or another app in front, and a notification says so.
 */
class MeetingService : Service() {
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main.immediate)
    private val recorder = MicRecorder(MediaRecorder.AudioSource.MIC)
    private var output: OutputStream? = null
    private var recordingId: String? = null
    private var startedAtMs = 0L
    private var wakeLock: PowerManager.WakeLock? = null
    private val jobs = HashMap<String, Job>()

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        when (intent?.action) {
            ACTION_START -> startRecording()
            ACTION_STOP -> stopRecording()
            ACTION_PROCESS -> intent.getStringExtra(EXTRA_ID)?.let(::process)
            ACTION_CANCEL -> intent.getStringExtra(EXTRA_ID)?.let { jobs[it]?.cancel() }
        }
        stopIfIdle()
        return START_NOT_STICKY
    }

    private fun startRecording() {
        if (recordingId != null) return
        val repo = graph.meetings
        val now = System.currentTimeMillis()
        val id = UUID.randomUUID().toString()
        val title = "Meeting, " + SimpleDateFormat("d MMM, HH:mm", Locale.getDefault()).format(Date(now))
        val meeting = Meeting(id, title, now, language = graph.settings.language, status = Meeting.Status.RECORDING)
        try {
            foreground(recordingNotification(), ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE)
        } catch (e: Exception) {
            // The system refused (no microphone permission, or started from the background).
            return
        }
        repo.store.save(meeting)
        val stream = BufferedOutputStream(FileOutputStream(repo.store.audioFile(id)), 64 * 1024)
        val started = recorder.start(
            this,
            onBlock = { block, length, level ->
                stream.write(block, 0, length)
                repo.recording.value = LiveRecording(id, now, level)
                if (System.currentTimeMillis() - now > MAX_RECORDING_MS) scope.launch { stopRecording() }
            },
            onError = { scope.launch { stopRecording() } },
        )
        if (!started) {
            stream.close()
            repo.store.delete(id)
            stopForegroundNow()
            return
        }
        output = stream
        recordingId = id
        startedAtMs = now
        repo.recording.value = LiveRecording(id, now)
        wakeLock = (getSystemService(Context.POWER_SERVICE) as PowerManager)
            .newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "gusan:meeting").apply { acquire(MAX_RECORDING_MS + 60_000) }
        scope.launch { repo.reload() }
    }

    private fun stopRecording() {
        val id = recordingId ?: return
        recordingId = null
        recorder.stop()
        runCatching { output?.close() }
        output = null
        wakeLock?.let { if (it.isHeld) it.release() }
        wakeLock = null
        val repo = graph.meetings
        repo.recording.value = null
        val seconds = repo.store.audioFile(id).length() / Audio.BYTES_PER_SECOND
        repo.store.load(id)?.let { repo.store.save(it.copy(status = Meeting.Status.RECORDED, durationMs = seconds * 1000)) }
        if (seconds < 2) {
            // A tap by mistake: nothing worth keeping.
            repo.store.delete(id)
            scope.launch { repo.reload() }
            return
        }
        process(id)
    }

    private fun process(id: String) {
        // Every start of this service must be answered with a foreground notification, also when the
        // meeting is already being worked on.
        foreground(workNotification("Writing the transcript…", 0, 0), ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC)
        if (jobs[id]?.isActive == true) return
        val app = graph
        val repo = app.meetings
        val job = scope.launch {
            val me = coroutineContext[Job]
            repo.work.value = repo.work.value + (id to Work(id, 0, 1))
            val processor = MeetingProcessor(repo.store, app.auth::accessToken, app.transcribe, MeetingNotesClient())
            val result = withContext(Dispatchers.IO) {
                processor.process(id, cancelled = { me?.isActive != true }) { meeting, done, total ->
                    repo.work.value = repo.work.value + (id to Work(id, done, total))
                    val text = if (meeting.status == Meeting.Status.WRITING_NOTES) "Writing the notes…" else "Writing the transcript…"
                    notifications().notify(NOTIFICATION_ID, workNotification(text, done, total))
                    scope.launch { repo.reload() }
                }
            }
            if (result?.error == GusanException.Kind.UNAUTHORIZED.message) app.account.sessionLost()
            if (result != null && (result.status == Meeting.Status.DONE || result.status == Meeting.Status.FAILED)) {
                notifications().notify(id.hashCode(), resultNotification(result))
            }
        }
        jobs[id] = job
        job.invokeOnCompletion {
            scope.launch {
                jobs.remove(id)
                repo.work.value = repo.work.value - id
                // A cancelled job may have left the meeting marked as busy.
                withContext(Dispatchers.IO) {
                    repo.store.load(id)?.takeIf { it.busy }?.let { repo.store.save(it.copy(status = Meeting.Status.RECORDED)) }
                }
                repo.reload()
                stopIfIdle()
            }
        }
    }

    private fun stopIfIdle() {
        if (recordingId != null) {
            foreground(recordingNotification(), ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE)
        } else if (jobs.isEmpty()) {
            stopForegroundNow()
            stopSelf()
        }
    }

    private fun foreground(notification: Notification, type: Int) {
        ServiceCompat.startForeground(this, NOTIFICATION_ID, notification, if (Build.VERSION.SDK_INT >= 29) type else 0)
    }

    private fun stopForegroundNow() = ServiceCompat.stopForeground(this, ServiceCompat.STOP_FOREGROUND_REMOVE)

    private fun notifications() = getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager

    private fun channels() {
        val manager = notifications()
        manager.createNotificationChannel(
            NotificationChannel(CHANNEL_RECORDING, getString(R.string.channel_recording), NotificationManager.IMPORTANCE_LOW)
                .apply { description = getString(R.string.channel_recording_about) },
        )
        manager.createNotificationChannel(
            NotificationChannel(CHANNEL_NOTES, getString(R.string.channel_notes), NotificationManager.IMPORTANCE_DEFAULT)
                .apply { description = getString(R.string.channel_notes_about) },
        )
    }

    private fun open(meetingId: String?): PendingIntent {
        val intent = Intent(this, MainActivity::class.java)
            .setAction(MainActivity.ACTION_OPEN_MEETING + (meetingId ?: ""))
            .putExtra(MainActivity.EXTRA_MEETING_ID, meetingId)
            .addFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP or Intent.FLAG_ACTIVITY_CLEAR_TOP)
        return PendingIntent.getActivity(this, 0, intent, PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE)
    }

    private fun recordingNotification(): Notification {
        channels()
        val stop = PendingIntent.getService(
            this, 1, Intent(this, MeetingService::class.java).setAction(ACTION_STOP), PendingIntent.FLAG_IMMUTABLE,
        )
        return NotificationCompat.Builder(this, CHANNEL_RECORDING)
            .setSmallIcon(R.drawable.ic_mic)
            .setContentTitle("Recording a meeting")
            .setContentText("Gusan is listening. Tap to open.")
            .setOngoing(true)
            .setUsesChronometer(true)
            .setWhen(if (startedAtMs > 0) startedAtMs else System.currentTimeMillis())
            .setContentIntent(open(recordingId))
            .addAction(R.drawable.ic_stop, "Stop and write notes", stop)
            .setForegroundServiceBehavior(NotificationCompat.FOREGROUND_SERVICE_IMMEDIATE)
            .build()
    }

    private fun workNotification(text: String, done: Int, total: Int): Notification {
        channels()
        return NotificationCompat.Builder(this, CHANNEL_RECORDING)
            .setSmallIcon(R.drawable.ic_mic)
            .setContentTitle(text)
            .setOngoing(true)
            .setOnlyAlertOnce(true)
            .setProgress(total, done, total == 0)
            .setContentIntent(open(null))
            .build()
    }

    private fun resultNotification(meeting: Meeting): Notification {
        channels()
        val ok = meeting.status == Meeting.Status.DONE
        return NotificationCompat.Builder(this, CHANNEL_NOTES)
            .setSmallIcon(R.drawable.ic_mic)
            .setContentTitle(if (ok) "Notes ready" else "Notes not written")
            .setContentText(if (ok) meeting.title else meeting.error)
            .setAutoCancel(true)
            .setContentIntent(open(meeting.id))
            .build()
    }

    override fun onDestroy() {
        if (recordingId != null) stopRecording()
        scope.cancel()
        super.onDestroy()
    }

    companion object {
        private const val ACTION_START = "com.aram2k.gusan.meeting.START"
        private const val ACTION_STOP = "com.aram2k.gusan.meeting.STOP"
        private const val ACTION_PROCESS = "com.aram2k.gusan.meeting.PROCESS"
        private const val ACTION_CANCEL = "com.aram2k.gusan.meeting.CANCEL"
        private const val EXTRA_ID = "id"
        private const val CHANNEL_RECORDING = "recording"
        private const val CHANNEL_NOTES = "notes"
        private const val NOTIFICATION_ID = 1
        private const val MAX_RECORDING_MS = 4 * 60 * 60 * 1000L

        private fun intent(context: Context, action: String, id: String? = null) =
            Intent(context, MeetingService::class.java).setAction(action).putExtra(EXTRA_ID, id)

        /** Call with the app on screen and the microphone allowed. */
        fun startRecording(context: Context) = context.startForegroundService(intent(context, ACTION_START))
        fun stopRecording(context: Context) { context.startService(intent(context, ACTION_STOP)) }
        fun process(context: Context, id: String) = context.startForegroundService(intent(context, ACTION_PROCESS, id))
        fun cancel(context: Context, id: String) { context.startService(intent(context, ACTION_CANCEL, id)) }
    }
}
