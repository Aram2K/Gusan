package com.aram2k.gusan.keyboard

import android.annotation.SuppressLint
import android.content.Context
import android.content.res.Configuration
import android.graphics.Canvas
import android.graphics.Paint
import android.graphics.Path
import android.graphics.RectF
import android.graphics.Typeface
import android.os.Handler
import android.os.Looper
import android.view.MotionEvent
import android.view.View
import com.aram2k.gusan.core.Key
import com.aram2k.gusan.core.KeyboardLayout
import com.aram2k.gusan.core.KeyboardPage
import com.aram2k.gusan.core.ShiftState
import kotlin.math.abs
import kotlin.math.min

/** The keys. It draws them and says which one was pressed; the service decides what a press does. */
@SuppressLint("ViewConstructor")
class KeyboardView(context: Context, private var theme: KeyboardTheme) : View(context) {

    interface Listener {
        /** A key went down (for the click and the buzz). */
        fun onPress(key: Key)
        /** A key was typed: on release, or again and again while delete is held. */
        fun onKey(key: Key)
        fun onLongPress(key: Key)
    }

    var listener: Listener? = null

    var page: KeyboardPage = KeyboardPage.LETTERS
        set(value) { if (field != value) { field = value; rebuild() } }
    var shift: ShiftState = ShiftState.OFF
        set(value) { if (field != value) { field = value; invalidate() } }
    var showSwitch: Boolean = false
        set(value) { if (field != value) { field = value; rebuild() } }
    /** "" draws the return arrow; otherwise the word for what return does here ("Go", "Send" ...). */
    var returnLabel: String = ""
        set(value) { if (field != value) { field = value; invalidate() } }

    private class Placed(val key: Key, val bounds: RectF, val row: Int)

    private val density = resources.displayMetrics.density
    private var placed: List<Placed> = emptyList()
    private val pressed = HashMap<Int, Placed>()        // pointer id -> the key under that finger
    private val timers = Handler(Looper.getMainLooper())
    private var repeating: Runnable? = null
    private var longPress: Runnable? = null
    private var longPressed = false

    private val fill = Paint(Paint.ANTI_ALIAS_FLAG)
    private val label = Paint(Paint.ANTI_ALIAS_FLAG).apply { textAlign = Paint.Align.CENTER }
    private val stroke = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        style = Paint.Style.STROKE
        strokeCap = Paint.Cap.ROUND
        strokeJoin = Paint.Join.ROUND
    }
    private val path = Path()

    fun setTheme(next: KeyboardTheme) { theme = next; invalidate() }

    private fun dp(value: Float) = value * density

    private val rowHeight: Float
        get() = dp(if (resources.configuration.orientation == Configuration.ORIENTATION_LANDSCAPE) 42f else 54f)

    override fun onMeasure(widthMeasureSpec: Int, heightMeasureSpec: Int) {
        val width = MeasureSpec.getSize(widthMeasureSpec)
        setMeasuredDimension(width, (rowHeight * 4 + dp(6f)).toInt())
    }

    override fun onSizeChanged(w: Int, h: Int, oldw: Int, oldh: Int) = rebuild()

    private fun rebuild() {
        pressed.clear()
        stopTimers()
        val side = dp(3f)
        // On a wide screen (a tablet, a phone on its side) the keys stop growing and stay in the middle.
        val board = min(width.toFloat(), dp(720f))
        val left = (width - board) / 2
        val unit = (board - side * 2) / 10f
        val out = ArrayList<Placed>()
        KeyboardLayout.rows(page, showSwitch).forEachIndexed { rowIndex, row ->
            val rowWidth = row.sumOf { it.weight.toDouble() }.toFloat() * unit
            var x = left + side + (board - side * 2 - rowWidth) / 2
            val top = dp(3f) + rowIndex * rowHeight
            for (key in row) {
                val w = key.weight * unit
                out.add(Placed(key, RectF(x, top, x + w, top + rowHeight), rowIndex))
                x += w
            }
        }
        placed = out
        invalidate()
    }

    override fun onDraw(canvas: Canvas) {
        canvas.drawColor(theme.background)
        val gapX = dp(3f)
        val gapY = dp(5f)
        val radius = dp(6f)
        for (p in placed) {
            val r = RectF(p.bounds.left + gapX, p.bounds.top + gapY, p.bounds.right - gapX, p.bounds.bottom - gapY)
            val down = pressed.values.any { it === p }
            val special = p.key.kind != Key.Kind.CHAR && p.key.kind != Key.Kind.SPACE
            val accent = p.key.kind == Key.Kind.RETURN
            fill.color = theme.shadow
            canvas.drawRoundRect(r.left, r.top + dp(1f), r.right, r.bottom + dp(1f), radius, radius, fill)
            fill.color = when {
                accent -> if (down) blend(theme.accent, 0xFF000000.toInt(), 0.2f) else theme.accent
                p.key.kind == Key.Kind.SHIFT && shift.on -> theme.key
                special -> if (down) theme.specialPressed else theme.special
                else -> if (down) theme.keyPressed else theme.key
            }
            canvas.drawRoundRect(r, radius, radius, fill)
            val ink = if (accent) theme.onAccent else theme.text
            when (p.key.kind) {
                Key.Kind.CHAR -> text(canvas, r, if (shift.on) p.key.label.uppercase() else p.key.label, dp(23f), ink)
                Key.Kind.NUMBERS, Key.Kind.SYMBOLS, Key.Kind.LETTERS -> text(canvas, r, p.key.label, dp(15f), ink)
                Key.Kind.RETURN -> if (returnLabel.isEmpty()) returnArrow(canvas, r, ink) else text(canvas, r, returnLabel, dp(15f), ink, bold = true)
                Key.Kind.SHIFT -> shiftArrow(canvas, r, ink)
                Key.Kind.DELETE -> deleteMark(canvas, r, ink)
                Key.Kind.SWITCH_KEYBOARD -> globe(canvas, r, ink)
                Key.Kind.SPACE -> Unit
            }
        }
    }

    private fun text(canvas: Canvas, r: RectF, value: String, size: Float, color: Int, bold: Boolean = false) {
        label.color = color
        label.textSize = size
        label.typeface = if (bold) Typeface.DEFAULT_BOLD else Typeface.DEFAULT
        canvas.drawText(value, r.centerX(), r.centerY() - (label.descent() + label.ascent()) / 2, label)
    }

    private fun line(color: Int, filled: Boolean = false): Paint = stroke.apply {
        this.color = color
        strokeWidth = dp(1.8f)
        style = if (filled) Paint.Style.FILL_AND_STROKE else Paint.Style.STROKE
    }

    private fun shiftArrow(canvas: Canvas, r: RectF, color: Int) {
        val s = dp(9f)
        val cx = r.centerX()
        val cy = r.centerY()
        path.reset()
        path.moveTo(cx, cy - s)
        path.lineTo(cx + s, cy)
        path.lineTo(cx + s * 0.45f, cy)
        path.lineTo(cx + s * 0.45f, cy + s * 0.85f)
        path.lineTo(cx - s * 0.45f, cy + s * 0.85f)
        path.lineTo(cx - s * 0.45f, cy)
        path.lineTo(cx - s, cy)
        path.close()
        canvas.drawPath(path, line(color, filled = shift.on))
        if (shift == ShiftState.LOCKED) {
            canvas.drawLine(cx - s * 0.45f, cy + s * 1.3f, cx + s * 0.45f, cy + s * 1.3f, line(color))
        }
    }

    private fun deleteMark(canvas: Canvas, r: RectF, color: Int) {
        val h = dp(8f)
        val cx = r.centerX() + dp(2f)
        val cy = r.centerY()
        path.reset()
        path.moveTo(cx - h * 1.7f, cy)
        path.lineTo(cx - h * 0.8f, cy - h)
        path.lineTo(cx + h * 1.2f, cy - h)
        path.lineTo(cx + h * 1.2f, cy + h)
        path.lineTo(cx - h * 0.8f, cy + h)
        path.close()
        val paint = line(color)
        canvas.drawPath(path, paint)
        val x = h * 0.38f
        canvas.drawLine(cx - x + dp(1f), cy - x, cx + x + dp(1f), cy + x, paint)
        canvas.drawLine(cx - x + dp(1f), cy + x, cx + x + dp(1f), cy - x, paint)
    }

    private fun returnArrow(canvas: Canvas, r: RectF, color: Int) {
        val s = dp(8f)
        val cx = r.centerX()
        val cy = r.centerY()
        val paint = line(color)
        path.reset()
        path.moveTo(cx + s, cy - s * 0.8f)
        path.lineTo(cx + s, cy + s * 0.3f)
        path.lineTo(cx - s, cy + s * 0.3f)
        path.moveTo(cx - s * 0.35f, cy - s * 0.35f)
        path.lineTo(cx - s, cy + s * 0.3f)
        path.lineTo(cx - s * 0.35f, cy + s * 0.95f)
        canvas.drawPath(path, paint)
    }

    private fun globe(canvas: Canvas, r: RectF, color: Int) {
        val s = dp(9f)
        val cx = r.centerX()
        val cy = r.centerY()
        val paint = line(color).apply { strokeWidth = dp(1.5f) }
        canvas.drawCircle(cx, cy, s, paint)
        canvas.drawOval(cx - s * 0.45f, cy - s, cx + s * 0.45f, cy + s, paint)
        canvas.drawLine(cx - s, cy, cx + s, cy, paint)
    }

    private fun blend(a: Int, b: Int, t: Float): Int {
        fun ch(shift: Int) = ((a shr shift and 0xFF) * (1 - t) + (b shr shift and 0xFF) * t).toInt()
        return (0xFF shl 24) or (ch(16) shl 16) or (ch(8) shl 8) or ch(0)
    }

    /** The key at a point. A touch in a gap or beside a short row goes to the nearest key of that row. */
    private fun keyAt(x: Float, y: Float): Placed? {
        if (placed.isEmpty()) return null
        val row = ((y - dp(3f)) / rowHeight).toInt().coerceIn(0, 3)
        return placed.filter { it.row == row }.minByOrNull {
            if (x >= it.bounds.left && x < it.bounds.right) 0f else min(abs(x - it.bounds.left), abs(x - it.bounds.right))
        }
    }

    private fun stopTimers() {
        repeating?.let { timers.removeCallbacks(it) }
        longPress?.let { timers.removeCallbacks(it) }
        repeating = null
        longPress = null
    }

    private fun down(pointer: Int, x: Float, y: Float) {
        val p = keyAt(x, y) ?: return
        // A second finger coming down types the first finger's key: that is how fast typing with two thumbs works.
        pressed.entries.toList().forEach { (id, held) ->
            if (held.key.kind != Key.Kind.DELETE && held.key.kind != Key.Kind.SHIFT) {
                pressed.remove(id)
                if (!longPressed) listener?.onKey(held.key)
            }
        }
        stopTimers()
        longPressed = false
        pressed[pointer] = p
        listener?.onPress(p.key)
        when (p.key.kind) {
            Key.Kind.DELETE -> {
                listener?.onKey(p.key)
                val again = object : Runnable {
                    override fun run() {
                        if (pressed[pointer] === p) {
                            listener?.onKey(p.key)
                            timers.postDelayed(this, 55)
                        }
                    }
                }
                repeating = again
                timers.postDelayed(again, 400)
            }
            Key.Kind.SWITCH_KEYBOARD, Key.Kind.SPACE -> {
                val hold = Runnable {
                    if (pressed[pointer] === p) {
                        longPressed = true
                        listener?.onLongPress(p.key)
                    }
                }
                longPress = hold
                timers.postDelayed(hold, 500)
            }
            else -> Unit
        }
        invalidate()
    }

    private fun move(pointer: Int, x: Float, y: Float) {
        val held = pressed[pointer] ?: return
        if (held.key.kind == Key.Kind.DELETE) return
        val now = keyAt(x, y) ?: return
        if (now !== held && now.key.kind != Key.Kind.DELETE) {
            stopTimers()
            pressed[pointer] = now
            invalidate()
        }
    }

    private fun up(pointer: Int) {
        val held = pressed.remove(pointer) ?: return
        stopTimers()
        if (held.key.kind != Key.Kind.DELETE && !longPressed) listener?.onKey(held.key)
        longPressed = false
        invalidate()
    }

    @SuppressLint("ClickableViewAccessibility")
    override fun onTouchEvent(event: MotionEvent): Boolean {
        val index = event.actionIndex
        when (event.actionMasked) {
            MotionEvent.ACTION_DOWN, MotionEvent.ACTION_POINTER_DOWN -> down(event.getPointerId(index), event.getX(index), event.getY(index))
            MotionEvent.ACTION_MOVE -> for (i in 0 until event.pointerCount) move(event.getPointerId(i), event.getX(i), event.getY(i))
            MotionEvent.ACTION_UP, MotionEvent.ACTION_POINTER_UP -> up(event.getPointerId(index))
            MotionEvent.ACTION_CANCEL -> { pressed.clear(); stopTimers(); longPressed = false; invalidate() }
        }
        return true
    }

    override fun onDetachedFromWindow() {
        stopTimers()
        pressed.clear()
        super.onDetachedFromWindow()
    }
}
