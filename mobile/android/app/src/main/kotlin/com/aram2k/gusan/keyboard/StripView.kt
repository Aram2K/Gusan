package com.aram2k.gusan.keyboard

import android.annotation.SuppressLint
import android.content.Context
import android.content.res.ColorStateList
import android.graphics.Canvas
import android.graphics.Paint
import android.graphics.Typeface
import android.graphics.drawable.GradientDrawable
import android.text.TextUtils
import android.util.TypedValue
import android.view.Gravity
import android.view.View
import android.widget.FrameLayout
import android.widget.ImageView
import android.widget.LinearLayout
import android.widget.ProgressBar
import android.widget.TextView
import com.aram2k.gusan.R
import com.aram2k.gusan.core.VoiceState

/** The Gusan strip above the keys: the mic, what Gusan is doing, and Undo. */
@SuppressLint("ViewConstructor")
class StripView(context: Context, private val theme: KeyboardTheme) : LinearLayout(context) {

    interface Listener {
        fun onLogo()
        fun onMic()
        fun onCancel()
        /** Undo after a result, or the button an error offers. */
        fun onAction()
    }

    var listener: Listener? = null

    private val density = resources.displayMetrics.density
    private fun dp(value: Int) = (value * density).toInt()

    private val logo = ImageView(context).apply {
        setImageResource(R.drawable.gusan_logo)
        contentDescription = "Open Gusan"
        setOnClickListener { listener?.onLogo() }
    }
    private val spinner = ProgressBar(context).apply {
        isIndeterminate = true
        indeterminateTintList = ColorStateList.valueOf(theme.accent)
    }
    private val label = TextView(context).apply {
        setTextColor(theme.secondaryText)
        setTextSize(TypedValue.COMPLEX_UNIT_SP, 14f)
        maxLines = 2
        ellipsize = TextUtils.TruncateAt.END
        gravity = Gravity.CENTER_VERTICAL
    }
    private val wave = WaveView(context, theme.recording)
    private val action = TextView(context).apply {
        setTextColor(theme.accent)
        setTextSize(TypedValue.COMPLEX_UNIT_SP, 14f)
        typeface = Typeface.DEFAULT_BOLD
        gravity = Gravity.CENTER
        setPadding(dp(12), 0, dp(12), 0)
        minWidth = dp(48)
        setOnClickListener { listener?.onAction() }
    }
    private val cancel = ImageView(context).apply {
        setImageResource(R.drawable.ic_close)
        imageTintList = ColorStateList.valueOf(theme.text)
        background = GradientDrawable().apply { shape = GradientDrawable.OVAL; setColor(theme.chip) }
        setPadding(dp(8), dp(8), dp(8), dp(8))
        contentDescription = "Cancel"
        setOnClickListener { listener?.onCancel() }
    }
    private val mic = ImageView(context).apply {
        setImageResource(R.drawable.ic_mic)
        imageTintList = ColorStateList.valueOf(theme.onAccent)
        background = GradientDrawable().apply { shape = GradientDrawable.OVAL; setColor(theme.accent) }
        setPadding(dp(7), dp(7), dp(7), dp(7))
        setOnClickListener { listener?.onMic() }
    }

    init {
        orientation = HORIZONTAL
        gravity = Gravity.CENTER_VERTICAL
        setPadding(dp(10), dp(4), dp(10), dp(2))
        setBackgroundColor(theme.background)
        addView(logo, LayoutParams(dp(30), dp(30)))
        addView(spinner, LayoutParams(dp(22), dp(22)).apply { marginStart = dp(4) })
        val middle = FrameLayout(context)
        middle.addView(label, FrameLayout.LayoutParams(FrameLayout.LayoutParams.MATCH_PARENT, FrameLayout.LayoutParams.MATCH_PARENT))
        middle.addView(wave, FrameLayout.LayoutParams(FrameLayout.LayoutParams.MATCH_PARENT, FrameLayout.LayoutParams.MATCH_PARENT))
        addView(middle, LayoutParams(0, LayoutParams.MATCH_PARENT, 1f).apply { marginStart = dp(10); marginEnd = dp(6) })
        addView(action, LayoutParams(LayoutParams.WRAP_CONTENT, LayoutParams.MATCH_PARENT))
        addView(cancel, LayoutParams(dp(36), dp(36)).apply { marginEnd = dp(8) })
        addView(mic, LayoutParams(dp(38), dp(38)))
        render(VoiceState.Idle, "")
    }

    private fun show(view: View, visible: Boolean) { view.visibility = if (visible) VISIBLE else GONE }

    /** hint: what the idle strip says ("Tap the mic and say what you want"). */
    fun render(state: VoiceState, hint: String) {
        show(logo, state !is VoiceState.Listening && state !is VoiceState.Working)
        show(spinner, state is VoiceState.Working)
        show(wave, state is VoiceState.Listening)
        show(label, state !is VoiceState.Listening)
        show(cancel, state is VoiceState.Listening || state is VoiceState.Working)
        show(mic, state !is VoiceState.Working)
        label.setTextColor(if (state is VoiceState.Failed) theme.danger else theme.secondaryText)
        var button = ""
        when (state) {
            is VoiceState.Idle -> label.text = hint
            is VoiceState.Listening -> wave.clear()
            is VoiceState.Working -> label.text = state.heard.ifEmpty { "Listening back…" }
            is VoiceState.Done -> { label.text = "Inserted"; button = "Undo" }
            is VoiceState.Failed -> {
                label.text = state.message
                button = when (state.action) {
                    VoiceState.Action.OPEN_APP -> "Open Gusan"
                    VoiceState.Action.SEE_PLANS -> "See plans"
                    VoiceState.Action.NONE -> ""
                }
            }
        }
        action.text = button
        show(action, button.isNotEmpty())
        mic.contentDescription = if (state is VoiceState.Listening) "Finish and send" else "Speak to Gusan"
        // While listening the mic turns red: the one place a person looks to know Gusan hears them.
        (mic.background as GradientDrawable).setColor(if (state is VoiceState.Listening) theme.recording else theme.accent)
        mic.setImageResource(if (state is VoiceState.Listening) R.drawable.ic_check else R.drawable.ic_mic)
        mic.imageTintList = ColorStateList.valueOf(if (state is VoiceState.Listening) 0xFFFFFFFF.toInt() else theme.onAccent)
    }

    fun pushLevel(level: Float) = wave.push(level)

    /** The last moments of sound as bars, newest at the right, across the whole strip. */
    private class WaveView(context: Context, color: Int) : View(context) {
        private val levels = FloatArray(BARS)
        private var count = 0
        private val paint = Paint(Paint.ANTI_ALIAS_FLAG).apply { this.color = color; strokeCap = Paint.Cap.ROUND }

        fun setColor(color: Int) { paint.color = color; invalidate() }

        fun clear() { levels.fill(0f); count = 0; invalidate() }

        fun push(level: Float) {
            levels[count % BARS] = level
            count++
            invalidate()
        }

        override fun onDraw(canvas: Canvas) {
            val step = width / BARS.toFloat()
            paint.strokeWidth = step * 0.55f
            val mid = height / 2f
            for (i in 0 until BARS) {
                // Bar i from the left is the level heard BARS - 1 - i blocks ago.
                val age = BARS - 1 - i
                val value = if (age < count) levels[((count - 1 - age) % BARS + BARS) % BARS] else 0f
                val half = maxOf(step * 0.3f, value * mid * 0.9f)
                val x = step * (i + 0.5f)
                canvas.drawLine(x, mid - half, x, mid + half, paint)
            }
        }

        private companion object { const val BARS = 48 }
    }
}
