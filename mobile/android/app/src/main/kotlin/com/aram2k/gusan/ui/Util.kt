package com.aram2k.gusan.ui

import android.Manifest
import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.provider.Settings
import android.view.inputmethod.InputMethodManager
import android.widget.Toast
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.platform.LocalContext
import com.aram2k.gusan.audio.MicRecorder
import com.aram2k.gusan.core.Backend
import com.aram2k.gusan.core.Meeting
import com.aram2k.gusan.meetings.Work
import kotlinx.coroutines.delay

/** What the phone still needs before everything works. */
data class Readiness(val notifications: Boolean, val microphone: Boolean, val keyboardOn: Boolean, val keyboardChosen: Boolean) {
    val keyboardReady get() = keyboardOn && keyboardChosen

    companion object {
        fun of(context: Context): Readiness {
            val imm = context.getSystemService(Context.INPUT_METHOD_SERVICE) as InputMethodManager
            val on = imm.enabledInputMethodList.any { it.packageName == context.packageName }
            val current = Settings.Secure.getString(context.contentResolver, Settings.Secure.DEFAULT_INPUT_METHOD).orEmpty()
            return Readiness(notificationsAllowed(context), MicRecorder.hasPermission(context), on, on && current.startsWith(context.packageName + "/"))
        }
    }
}

fun notificationsAllowed(context: Context): Boolean =
    Build.VERSION.SDK_INT < 33 || context.checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) == PackageManager.PERMISSION_GRANTED

/** The setup, checked again twice a second: the person changes it on the system's own pages. */
@Composable
fun rememberReadiness(): Readiness {
    val context = LocalContext.current
    var readiness by remember { mutableStateOf(Readiness.of(context)) }
    LaunchedEffect(Unit) {
        while (true) {
            readiness = Readiness.of(context)
            delay(500)
        }
    }
    return readiness
}

fun openKeyboardSettings(context: Context) {
    context.startActivity(Intent(Settings.ACTION_INPUT_METHOD_SETTINGS).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
}

fun showKeyboardPicker(context: Context) {
    (context.getSystemService(Context.INPUT_METHOD_SERVICE) as InputMethodManager).showInputMethodPicker()
}

fun openAppSettings(context: Context) {
    context.startActivity(
        Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS, Uri.parse("package:" + context.packageName)).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK),
    )
}

fun openUrl(context: Context, url: String) {
    runCatching { context.startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(url)).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)) }
}

fun openPricing(context: Context) = openUrl(context, Backend.PRICING)

fun copyText(context: Context, text: String) {
    (context.getSystemService(Context.CLIPBOARD_SERVICE) as ClipboardManager).setPrimaryClip(ClipData.newPlainText("Gusan", text))
    // Android 13 and later show their own "Copied" bubble.
    if (Build.VERSION.SDK_INT < 33) Toast.makeText(context, "Copied", Toast.LENGTH_SHORT).show()
}

fun shareText(context: Context, subject: String, text: String) {
    val send = Intent(Intent.ACTION_SEND).setType("text/plain").putExtra(Intent.EXTRA_SUBJECT, subject).putExtra(Intent.EXTRA_TEXT, text)
    context.startActivity(Intent.createChooser(send, null).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
}

/** 75 -> "1:15", 3723 -> "1:02:03". */
fun clock(seconds: Long): String {
    val h = seconds / 3600
    val m = seconds % 3600 / 60
    val s = seconds % 60
    return if (h > 0) "%d:%02d:%02d".format(h, m, s) else "%d:%02d".format(m, s)
}

/** What a meeting's row and page say about where it is. */
fun statusText(meeting: Meeting, work: Work?): String = when {
    meeting.status == Meeting.Status.RECORDING -> "Recording"
    work != null && work.total > 1 -> "Transcribing · ${minOf(work.done + 1, work.total)} of ${work.total}"
    work != null && work.total == 0 -> "Writing notes"
    work != null || meeting.status == Meeting.Status.TRANSCRIBING -> "Transcribing"
    meeting.status == Meeting.Status.WRITING_NOTES -> "Writing notes"
    meeting.status == Meeting.Status.DONE -> ""
    meeting.status == Meeting.Status.FAILED -> "Needs attention"
    else -> "No notes yet"
}
