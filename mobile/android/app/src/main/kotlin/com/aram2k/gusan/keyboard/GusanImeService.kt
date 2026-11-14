package com.aram2k.gusan.keyboard

import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import android.content.Intent
import android.inputmethodservice.InputMethodService
import android.media.AudioManager
import android.media.ToneGenerator
import android.net.Uri
import android.os.Build
import android.os.Handler
import android.os.Looper
import android.text.InputType
import android.view.HapticFeedbackConstants
import android.view.KeyEvent
import android.view.View
import android.view.inputmethod.EditorInfo
import android.view.inputmethod.InputMethodManager
import android.widget.LinearLayout
import androidx.core.view.ViewCompat
import androidx.core.view.WindowInsetsCompat
import com.aram2k.gusan.audio.MicRecorder
import com.aram2k.gusan.core.Audio
import com.aram2k.gusan.core.Backend
import com.aram2k.gusan.core.GusanException
import com.aram2k.gusan.core.InsertUndo
import com.aram2k.gusan.core.Key
import com.aram2k.gusan.core.KeyboardPage
import com.aram2k.gusan.core.MAX_DICTATION_MS
import com.aram2k.gusan.core.ShiftState
import com.aram2k.gusan.core.SmartAction
import com.aram2k.gusan.core.SmartEntry
import com.aram2k.gusan.core.SmartResult
import com.aram2k.gusan.core.TypingRules
import com.aram2k.gusan.core.VoiceState
import com.aram2k.gusan.graph
import com.aram2k.gusan.ui.MainActivity
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.MainScope
import kotlinx.coroutines.cancel
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.io.ByteArrayOutputStream
import java.util.UUID

/**
 * The Gusan keyboard: an ordinary keyboard with the Gusan strip on top. Tap the mic, say what you
 * want, and the finished text lands at the cursor (or replaces the selection), with Undo.
 */
class GusanImeService : InputMethodService(), KeyboardView.Listener, StripView.Listener {
    private val scope = MainScope()
    private val main = Handler(Looper.getMainLooper())
    private var keyboard: KeyboardView? = null
    private var strip: StripView? = null

    private var state: VoiceState = VoiceState.Idle
    private var notice = ""                     // a one-off line for the idle strip ("Copied to the clipboard")
    private val recorder = MicRecorder()
    private val audio = ByteArrayOutputStream()
    private var job: Job? = null
    private var selectedAtStart = ""

    private var manualShift = false
    private var lastShiftAt = 0L
    private var lastSpaceAt = 0L
    private var passwordField = false

    // ── views ────────────────────────────────────────────────────────────────

    override fun onCreateInputView(): View {
        val theme = KeyboardTheme.of(this)
        val density = resources.displayMetrics.density
        val root = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setBackgroundColor(theme.background)
        }
        val stripView = StripView(this, theme).also { it.listener = this }
        val keys = KeyboardView(this, theme).also { it.listener = this }
        root.addView(stripView, LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT, (48 * density).toInt()))
        root.addView(keys, LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT))
        // Where the system draws its navigation bar under the keyboard, keep the keys above it.
        ViewCompat.setOnApplyWindowInsetsListener(root) { view, insets ->
            view.setPadding(0, 0, 0, insets.getInsets(WindowInsetsCompat.Type.systemBars()).bottom)
            insets
        }
        strip = stripView
        keyboard = keys
        render()
        return root
    }

    override fun onStartInputView(info: EditorInfo, restarting: Boolean) {
        super.onStartInputView(info, restarting)
        val kind = info.inputType and InputType.TYPE_MASK_CLASS
        val variation = info.inputType and InputType.TYPE_MASK_VARIATION
        passwordField = (kind == InputType.TYPE_CLASS_TEXT && variation in PASSWORD_VARIATIONS) ||
            (kind == InputType.TYPE_CLASS_NUMBER && variation == InputType.TYPE_NUMBER_VARIATION_PASSWORD)
        keyboard?.apply {
            page = if (kind == InputType.TYPE_CLASS_NUMBER || kind == InputType.TYPE_CLASS_PHONE || kind == InputType.TYPE_CLASS_DATETIME) {
                KeyboardPage.NUMBERS
            } else {
                KeyboardPage.LETTERS
            }
            showSwitch = Build.VERSION.SDK_INT < 28 || shouldOfferSwitchingToNextInputMethod()
            returnLabel = returnLabel(info)
        }
        manualShift = false
        if (state is VoiceState.Done || state is VoiceState.Failed) state = VoiceState.Idle
        notice = ""
        autoShift()
        render()
    }

    override fun onFinishInputView(finishingInput: Boolean) {
        // The keyboard is gone: stop listening. A request already on its way finishes and is copied.
        if (state is VoiceState.Listening) cancelListening()
        super.onFinishInputView(finishingInput)
    }

    override fun onUpdateSelection(oldSelStart: Int, oldSelEnd: Int, newSelStart: Int, newSelEnd: Int, candidatesStart: Int, candidatesEnd: Int) {
        super.onUpdateSelection(oldSelStart, oldSelEnd, newSelStart, newSelEnd, candidatesStart, candidatesEnd)
        if (!manualShift) autoShift()
        val done = state as? VoiceState.Done
        if (done != null && !done.undo.available(textBefore(done.undo.inserted.length + 1), System.currentTimeMillis())) {
            state = VoiceState.Idle
        }
        if (state is VoiceState.Idle) render()
    }

    override fun onDestroy() {
        recorder.stop()
        scope.cancel()
        main.removeCallbacksAndMessages(null)
        super.onDestroy()
    }

    private fun returnLabel(info: EditorInfo): String {
        if (info.imeOptions and EditorInfo.IME_FLAG_NO_ENTER_ACTION != 0) return ""
        return when (info.imeOptions and EditorInfo.IME_MASK_ACTION) {
            EditorInfo.IME_ACTION_GO -> "Go"
            EditorInfo.IME_ACTION_SEARCH -> "Search"
            EditorInfo.IME_ACTION_SEND -> "Send"
            EditorInfo.IME_ACTION_NEXT -> "Next"
            EditorInfo.IME_ACTION_DONE -> "Done"
            else -> ""
        }
    }

    private fun render() {
        val hint = when {
            notice.isNotEmpty() -> notice
            passwordField -> "Voice is off in password fields"
            else -> {
                val words = selectedText().trim().split(Regex("\\s+")).count { it.isNotEmpty() }
                if (words > 0) "Selected · $words ${if (words == 1) "word" else "words"}. Say what to do with it."
                else "Tap the mic and say what you want"
            }
        }
        strip?.render(state, hint)
    }

    private fun setState(next: VoiceState) {
        state = next
        notice = ""
        render()
    }

    // ── typing ───────────────────────────────────────────────────────────────

    private fun textBefore(count: Int = 64): String =
        currentInputConnection?.getTextBeforeCursor(count, 0)?.toString().orEmpty()

    private fun selectedText(): String =
        currentInputConnection?.getSelectedText(0)?.toString().orEmpty()

    private fun autoShift() {
        val keys = keyboard ?: return
        if (keys.shift == ShiftState.LOCKED) return
        val info = currentInputEditorInfo
        val caps = graph.settings.autoCapitalize && info != null && info.inputType != InputType.TYPE_NULL &&
            (currentInputConnection?.getCursorCapsMode(info.inputType) ?: 0) != 0
        keys.shift = if (caps) ShiftState.ONCE else ShiftState.OFF
    }

    override fun onPress(key: Key) {
        val settings = graph.settings
        if (settings.haptics) keyboard?.performHapticFeedback(HapticFeedbackConstants.KEYBOARD_TAP)
        if (settings.keySounds) {
            val effect = when (key.kind) {
                Key.Kind.DELETE -> AudioManager.FX_KEYPRESS_DELETE
                Key.Kind.SPACE -> AudioManager.FX_KEYPRESS_SPACEBAR
                Key.Kind.RETURN -> AudioManager.FX_KEYPRESS_RETURN
                else -> AudioManager.FX_KEYPRESS_STANDARD
            }
            (getSystemService(Context.AUDIO_SERVICE) as AudioManager).playSoundEffect(effect, 0.5f)
        }
    }

    override fun onKey(key: Key) {
        val ic = currentInputConnection ?: return
        val keys = keyboard ?: return
        val now = System.currentTimeMillis()
        when (key.kind) {
            Key.Kind.CHAR -> {
                ic.commitText(if (keys.shift.on) key.label.uppercase() else key.label, 1)
                manualShift = false
                autoShift()
            }
            Key.Kind.SPACE -> {
                if (graph.settings.doubleSpacePeriod && now - lastSpaceAt < 700 && TypingRules.doubleSpaceMakesPeriod(textBefore())) {
                    ic.deleteSurroundingText(1, 0)
                    ic.commitText(". ", 1)
                    lastSpaceAt = 0
                } else {
                    ic.commitText(" ", 1)
                    lastSpaceAt = now
                }
                manualShift = false
                autoShift()
            }
            Key.Kind.DELETE -> {
                when {
                    selectedText().isNotEmpty() -> ic.commitText("", 1)
                    textBefore(2).isNotEmpty() -> ic.deleteSurroundingTextInCodePoints(1, 0)
                    // Nothing the editor will show us (a terminal, a game): press the real delete key.
                    else -> sendDownUpKeyEvents(KeyEvent.KEYCODE_DEL)
                }
                manualShift = false
                autoShift()
            }
            Key.Kind.RETURN -> {
                sendKeyChar('\n')
                manualShift = false
                autoShift()
            }
            Key.Kind.SHIFT -> {
                keys.shift = keys.shift.tapped(doubleTap = now - lastShiftAt < 350)
                lastShiftAt = now
                manualShift = true
            }
            Key.Kind.NUMBERS -> keys.page = KeyboardPage.NUMBERS
            Key.Kind.SYMBOLS -> keys.page = KeyboardPage.SYMBOLS
            Key.Kind.LETTERS -> keys.page = KeyboardPage.LETTERS
            Key.Kind.SWITCH_KEYBOARD -> {
                if (Build.VERSION.SDK_INT < 28 || !switchToNextInputMethod(false)) showKeyboardPicker()
            }
        }
        if (key.kind != Key.Kind.SPACE) lastSpaceAt = 0
    }

    override fun onLongPress(key: Key) {
        if (graph.settings.haptics) keyboard?.performHapticFeedback(HapticFeedbackConstants.LONG_PRESS)
        showKeyboardPicker()
    }

    private fun showKeyboardPicker() {
        (getSystemService(Context.INPUT_METHOD_SERVICE) as InputMethodManager).showInputMethodPicker()
    }

    // ── the strip ────────────────────────────────────────────────────────────

    override fun onLogo() = openApp()

    override fun onMic() {
        when (state) {
            is VoiceState.Listening -> finishListening()
            is VoiceState.Working -> Unit
            else -> startListening()
        }
    }

    override fun onCancel() {
        when (state) {
            is VoiceState.Listening -> cancelListening()
            is VoiceState.Working -> { job?.cancel(); setState(VoiceState.Idle) }
            else -> Unit
        }
    }

    override fun onAction() {
        when (val current = state) {
            is VoiceState.Done -> undo(current.undo)
            is VoiceState.Failed -> when (current.action) {
                VoiceState.Action.OPEN_APP -> openApp()
                VoiceState.Action.SEE_PLANS -> open(Intent(Intent.ACTION_VIEW, Uri.parse(Backend.PRICING)))
                VoiceState.Action.NONE -> Unit
            }
            else -> Unit
        }
    }

    private fun open(intent: Intent) {
        runCatching { startActivity(intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)) }
    }

    private fun openApp() = open(Intent(this, MainActivity::class.java))

    // ── voice ────────────────────────────────────────────────────────────────

    private fun startListening() {
        if (passwordField) return setState(VoiceState.Failed("Voice is off in password fields."))
        if (!MicRecorder.hasPermission(this)) {
            return setState(VoiceState.Failed("Allow the microphone in the Gusan app.", VoiceState.Action.OPEN_APP))
        }
        if (graph.auth.storage.read() == null) {
            return setState(VoiceState.failed(GusanException(GusanException.Kind.NOT_SIGNED_IN)))
        }
        selectedAtStart = selectedText()
        synchronized(audio) { audio.reset() }
        val startedAt = System.currentTimeMillis()
        if (graph.settings.sounds) chime()
        val started = recorder.start(
            this,
            onBlock = { block, length, level ->
                synchronized(audio) { audio.write(block, 0, length) }
                main.post {
                    if (state is VoiceState.Listening) {
                        strip?.pushLevel(level)
                        if (System.currentTimeMillis() - startedAt > MAX_DICTATION_MS) finishListening()
                    }
                }
            },
            onError = { main.post { if (state is VoiceState.Listening) cancelListening("The microphone stopped. Try again.") } },
        )
        if (!started) return setState(VoiceState.Failed("The microphone is busy. Try again."))
        if (graph.settings.haptics) keyboard?.performHapticFeedback(HapticFeedbackConstants.LONG_PRESS)
        setState(VoiceState.Listening(startedAt))
    }

    private fun chime() {
        runCatching {
            val tone = ToneGenerator(AudioManager.STREAM_NOTIFICATION, 50)
            tone.startTone(ToneGenerator.TONE_PROP_BEEP, 90)
            main.postDelayed({ tone.release() }, 300)
        }
    }

    private fun cancelListening(message: String = "") {
        recorder.stop()
        synchronized(audio) { audio.reset() }
        setState(if (message.isEmpty()) VoiceState.Idle else VoiceState.Failed(message))
    }

    private fun finishListening() {
        recorder.stop()
        val pcm = synchronized(audio) { audio.toByteArray().also { audio.reset() } }
        if (pcm.size < Audio.BYTES_PER_SECOND * 4 / 10) {
            return setState(VoiceState.failed(GusanException(GusanException.Kind.NOTHING_HEARD)))
        }
        if (graph.settings.haptics) keyboard?.performHapticFeedback(HapticFeedbackConstants.KEYBOARD_TAP)
        setState(VoiceState.Working(""))
        val app = graph
        val selected = selectedAtStart
        job = scope.launch {
            try {
                val token = withContext(Dispatchers.IO) { app.auth.accessToken() }
                val heard = withContext(Dispatchers.IO) { app.transcribe.run(Audio.wav(pcm), app.settings.language, token) }
                setState(VoiceState.Working(heard))
                val action = if (selected.isNotBlank()) SmartAction.VoiceOnText(heard, selected) else SmartAction.Smart(heard)
                val result = withContext(Dispatchers.IO) { app.smart.run(action, token) }
                insert(result, heard)
            } catch (e: CancellationException) {
                throw e
            } catch (e: GusanException) {
                if (e.kind == GusanException.Kind.UNAUTHORIZED) app.account.sessionLost()
                if (e.kind == GusanException.Kind.PRO_REQUIRED) app.account.refresh()
                setState(VoiceState.failed(e))
            } catch (e: Exception) {
                setState(VoiceState.failed(GusanException(GusanException.Kind.FAILED)))
            }
        }
    }

    private fun insert(result: SmartResult, heard: String) {
        val app = graph
        if (app.settings.saveHistory) {
            scope.launch(Dispatchers.IO) {
                app.history.add(SmartEntry(UUID.randomUUID().toString(), System.currentTimeMillis(), heard, result.text))
            }
        }
        val ic = currentInputConnection
        if (ic == null || !isInputViewShown) {
            // No text field to write into any more: the result is not lost.
            (getSystemService(Context.CLIPBOARD_SERVICE) as ClipboardManager).setPrimaryClip(ClipData.newPlainText("Gusan", result.text))
            state = VoiceState.Idle
            notice = "Copied. Paste it where you need it."
            render()
            return
        }
        val original = selectedText()
        val text = (if (original.isEmpty()) TypingRules.joiner(textBefore(), result.text) else "") + result.text
        ic.beginBatchEdit()
        ic.commitText(text, 1)
        ic.endBatchEdit()
        if (app.settings.haptics) keyboard?.performHapticFeedback(HapticFeedbackConstants.CONTEXT_CLICK)
        val undo = InsertUndo(text, original, System.currentTimeMillis())
        setState(VoiceState.Done(undo))
        main.postDelayed({ if ((state as? VoiceState.Done)?.undo === undo) setState(VoiceState.Idle) }, InsertUndo.WINDOW_MS)
    }

    private fun undo(undo: InsertUndo) {
        val ic = currentInputConnection
        if (ic != null && undo.available(textBefore(undo.inserted.length + 1), System.currentTimeMillis())) {
            ic.beginBatchEdit()
            ic.deleteSurroundingText(undo.inserted.length, 0)
            if (undo.original.isNotEmpty()) ic.commitText(undo.original, 1)
            ic.endBatchEdit()
        }
        setState(VoiceState.Idle)
    }

    private companion object {
        val PASSWORD_VARIATIONS = setOf(
            InputType.TYPE_TEXT_VARIATION_PASSWORD,
            InputType.TYPE_TEXT_VARIATION_WEB_PASSWORD,
            InputType.TYPE_TEXT_VARIATION_VISIBLE_PASSWORD,
        )
    }
}
