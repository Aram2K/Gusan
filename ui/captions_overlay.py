"""Live captions on the screen (founder, 2026-11-11): the words captions.py keeps, at the bottom middle of the screen
the call is on, with no background - white for the call, a soft yellow for you - each side in its own lines, never
overlapping.

Motion (his note on the first preview: "a bit violent"): nothing jumps. A word fades in (FADE_IN_S, easing out); words
that arrive together come one after another (STAGGER_S apart, never more than MAX_LAG_S behind); lines glide to their
place (a smooth follow, no bounce) - the newest line at the bottom, the oldest leaving at the top while it fades; a
side fades out after its last words (captions.HOLD_S). With the system's animations turned off, words simply appear.
Writing, not pushing (founder 2026-11-12: "every word it adds pushes the words before to the left"): a line starts at
the left edge of a box about captions.MAX_CHARS wide, centred on the screen, and grows to the right - the words already
there never move.

The window: frameless, see-through, always on top, never takes the focus or a click (click-through); hidden from screen
sharing and recordings unless cfg captions_in_share (ui.glass.exclude_from_capture, like the Live Assistant). Text:
the system's UI font, semi-bold, sized to the screen (about 2.8 % of its height), with a dark outline and a soft
shadow so it reads on any picture without a background.
"""
import logging
import re
import time

from PySide6.QtCore import QPointF, QRect, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QGuiApplication, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QWidget

import captions

logger = logging.getLogger("transcribe")

COLORS = {"them": (255, 255, 255), "me": (255, 226, 132)}     # the call white, you a soft yellow (captions.COLORS)
OUTLINE = (6, 6, 10)
FADE_IN_S = 0.28            # a word fades in this long
STAGGER_S = 0.05            # words that arrive together appear this far apart ...
MAX_LAG_S = 0.25            # ... never more than this behind the moment they came
BOX_SAMPLE = "so we will look at the new version of the app today"   # ordinary text, for the box's width
LEAVE_S = 0.45              # a line rolling out at the top fades this long
LANE_IN_S, LANE_OUT_S = 0.3, 0.8
FOLLOW = 7.0                # how quickly lines glide to their place (1/s; no overshoot)
FRAME_MS = 16
POLL_MS = 300               # how often the words are looked at again (a side's captions expire by themselves)
FONT_SHARE = 0.028          # of the screen's height
FONT_MIN, FONT_MAX = 20, 46
LINE_SPACING = 1.32
SIDE_GAP = 0.45             # between the two sides, in lines
BOTTOM_SHARE = 0.065        # of the screen's height, above its bottom (the taskbar excluded)
WIDTH_SHARE = 0.86


_WORDLIKE = re.compile(r"[^\w]+", re.UNICODE)


def _norm(word):
    return _WORDLIKE.sub("", str(word)).lower()


def _ease_out(t):
    t = max(0.0, min(1.0, t))
    return 1.0 - (1.0 - t) ** 3


class _Word:
    __slots__ = ("text", "born")

    def __init__(self, text, born):
        self.text, self.born = text, born


class _Line:
    __slots__ = ("id", "words", "y", "x", "leaving_at")

    def __init__(self, lid, y):
        self.id, self.words, self.y, self.x, self.leaving_at = lid, [], y, None, None


class _Side:
    __slots__ = ("lines", "alpha", "target", "height", "last_birth")

    def __init__(self):
        self.lines, self.alpha, self.target, self.height, self.last_birth = [], 0.0, 0.0, 0.0, 0.0


class CaptionsOverlay(QWidget):
    """See the module notes. ``feed`` is a captions.CaptionFeed; call nudge() (any thread) when its words changed."""

    changed = Signal()

    def __init__(self, feed=None, clock=time.monotonic, parent=None):
        flags = (Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.WindowTransparentForInput
                 | Qt.WindowDoesNotAcceptFocus | Qt.NoDropShadowWindowHint)
        super().__init__(parent, flags)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WA_NoSystemBackground, True)
        self.setAccessibleName("Live captions")
        self.feed = feed
        self._clock = clock
        self._sides = {s: _Side() for s in captions.SIDES}
        self._paths = {}
        self._in_share = False
        self._colors = dict(COLORS)
        self._font_key = "clean"
        self._px = 30
        self._animate = self._animations_on()
        self._last = clock()
        self._frame = QTimer(self)
        self._frame.setInterval(FRAME_MS)
        self._frame.timeout.connect(self._tick)
        self._poll = QTimer(self)
        self._poll.setInterval(POLL_MS)
        self._poll.timeout.connect(self.pull)
        self.changed.connect(self.pull, Qt.QueuedConnection)
        self._set_font(30)

    # ── setup ──
    @staticmethod
    def _animations_on():
        from ui import glass
        return glass.animations_enabled()           # (safe: True when the setting can't be read)

    def _set_font(self, px):
        self._px = px
        font = caption_font(self._font_key, px)
        self._font = font
        self._fm = QFontMetricsF(font)
        self._line_h = px * LINE_SPACING
        self._stroke = max(3.0, px * 0.17)
        # the box lines are written into (left-aligned, centred on the screen): about MAX_CHARS of ordinary text
        self._box_w = self._fm.horizontalAdvance(BOX_SAMPLE[:captions.MAX_CHARS])
        self._paths.clear()

    def set_style(self, font_key=None, colors_key=None):
        """The look picked on the Meetings card (captions.FONTS / captions.COLORS)."""
        self._font_key = font_key if font_key in captions.FONT_FAMILIES else "clean"
        them, me = color_pair(colors_key)
        self._colors = {"them": them, "me": me}
        self._set_font(self._px)
        self.update()

    def _box_left(self):
        return max(0.0, (self.width() - min(self._box_w, float(self.width()))) / 2.0)

    def _screen(self):
        """The screen the call is on (the window in front), else the primary one."""
        screens = QGuiApplication.screens()
        try:
            from ui import glass
            name = glass.foreground_monitor_name()
        except Exception:
            name = ""
        if name:
            for sc in screens:
                if sc.name() == name:
                    return sc
        return QGuiApplication.primaryScreen()

    def place(self, screen=None):
        """Size the text to the screen and sit at its bottom middle."""
        screen = screen or self._screen()
        if screen is None:
            return
        full, avail = screen.geometry(), screen.availableGeometry()
        px = max(FONT_MIN, min(FONT_MAX, round(full.height() * FONT_SHARE)))
        self._set_font(px)
        width = int(avail.width() * WIDTH_SHARE)
        lines = 2 * captions.MAX_LINES + 1 + SIDE_GAP          # both sides, and a line rolling out at the top
        height = int(self._line_h * lines + self._stroke * 4)
        bottom = avail.y() + avail.height() - int(full.height() * BOTTOM_SHARE)
        self.setGeometry(QRect(avail.x() + (avail.width() - width) // 2, bottom - height, width, height))

    def start(self):
        """Show (without taking the focus) and follow the feed."""
        self.place()
        self.show()
        try:
            from ui import glass
            glass.set_no_activate(self, True)
            glass.set_click_through(self, True)
        except Exception:
            logger.debug("captions window flags", exc_info=True)
        self.set_in_share(self._in_share)
        self.pull()

    def stop(self):
        self._poll.stop()
        self._frame.stop()
        self.hide()

    def set_in_share(self, on):
        """Seen in screen sharing and recordings (on) or hidden from them (off, the default)."""
        self._in_share = bool(on)
        if not self.isVisible():
            return
        try:
            from ui import glass
            glass.exclude_from_capture(self, not self._in_share)
        except Exception:
            logger.debug("captions capture exclusion", exc_info=True)

    # ── words ──
    def nudge(self):
        """The feed's words changed (any thread)."""
        try:
            self.changed.emit()
        except RuntimeError:
            pass                                         # the window is gone

    def pull(self):
        """Look at the words again. The slow look (POLL_MS: words let out at reading pace, a side fading after its
        last words) runs only while something is on screen or waiting - an idle meeting wakes nothing."""
        if self.feed is None:
            return
        try:
            self.set_view(self.feed.view())
            busy = self.feed.busy() if hasattr(self.feed, "busy") else True
        except Exception:
            logger.debug("captions view failed", exc_info=True)
            busy = True
        if busy and self.isVisible():
            if not self._poll.isActive():
                self._poll.start()
        elif self._poll.isActive():
            self._poll.stop()

    def set_view(self, view):
        """{"them": [(id, [words])], "me": [...]} from captions.CaptionFeed.view()."""
        now = self._clock()
        for name in captions.SIDES:
            side = self._sides[name]
            want = [(lid, list(ws)) for lid, ws in (view.get(name) or []) if ws]
            ids = {lid for lid, _ in want}
            for ln in side.lines:
                if ln.leaving_at is None and ln.id not in ids:
                    ln.leaving_at = now                  # rolled out at the top (or the side was cleared)
            kept = {ln.id: ln for ln in side.lines if ln.leaving_at is None}
            fresh = []
            for idx, (lid, words) in enumerate(want):
                ln = kept.get(lid)
                if ln is None:
                    ln = _Line(lid, self._slot_y(len(want) - 1 - idx))
                # A word that only changed its punctuation or case (the finished sentence of what was being
                # said: "so" -> "So,") keeps its place in the fade; a revised word fades in at once; new words
                # come one after another.
                same = 0
                while same < min(len(ln.words), len(words)) and _norm(ln.words[same].text) == _norm(words[same]):
                    ln.words[same].text = words[same]
                    same += 1
                if same < len(ln.words) or same < len(words):
                    added = []
                    for k, w in enumerate(words[same:], start=same):
                        if not self._animate:
                            born = now - FADE_IN_S
                        elif k < len(ln.words):
                            born = now                                     # revised
                        else:
                            born = min(max(now, side.last_birth + STAGGER_S), now + MAX_LAG_S)   # new
                            side.last_birth = born
                        added.append(_Word(w, born))
                    ln.words = ln.words[:same] + added
                fresh.append(ln)
            side.lines = fresh + [ln for ln in side.lines if ln.leaving_at is not None]
            side.target = 1.0 if want else 0.0
        self._wake()

    def _slot_y(self, from_bottom):
        return from_bottom * self._line_h

    # ── motion ──
    def _wake(self):
        self._last = self._clock()
        if not self._frame.isActive():
            self._frame.start()
        self.update()

    def _tick(self):
        now = self._clock()
        dt = max(0.0, min(0.1, now - self._last))
        self._last = now
        busy = False
        k = 1.0 - pow(2.718281828, -FOLLOW * dt) if self._animate else 1.0
        for side in self._sides.values():
            live = [ln for ln in side.lines if ln.leaving_at is None]
            n = len(live)
            for idx, ln in enumerate(live):
                target = self._slot_y(n - 1 - idx)
                ln.y += (target - ln.y) * k
                busy = busy or abs(target - ln.y) > 0.3
                busy = busy or any(now - w.born < FADE_IN_S for w in ln.words)
                ln.x = self._box_left()                # written from the left: the words there never move
            gone = []
            for ln in side.lines:
                if ln.leaving_at is not None:
                    ln.y += (self._slot_y(n) - ln.y) * k
                    if now - ln.leaving_at >= LEAVE_S or not self._animate:
                        gone.append(ln)
                    else:
                        busy = True
            if gone:
                side.lines = [ln for ln in side.lines if ln not in gone]
            target_h = n * self._line_h
            side.height += (target_h - side.height) * k
            busy = busy or abs(target_h - side.height) > 0.3
            step = (dt / (LANE_IN_S if side.target > side.alpha else LANE_OUT_S)) if self._animate else 1.0
            if side.alpha < side.target:
                side.alpha = min(side.target, side.alpha + step)
            elif side.alpha > side.target:
                side.alpha = max(side.target, side.alpha - step)
            busy = busy or abs(side.alpha - side.target) > 0.001
            if side.alpha <= 0.0 and side.target <= 0.0 and not any(ln.leaving_at is None for ln in side.lines):
                side.lines = [ln for ln in side.lines if ln.leaving_at is not None]
        self.update()
        if not busy:
            self._frame.stop()

    # ── drawing ──
    def _path(self, text):
        path = self._paths.get(text)
        if path is None:
            path = QPainterPath()
            path.addText(QPointF(0, 0), self._font, text)
            if len(self._paths) > 400:
                self._paths.clear()
            self._paths[text] = path
        return path

    def _word_alpha(self, word, now):
        if not self._animate:
            return 1.0
        return _ease_out((now - word.born) / FADE_IN_S)

    def paintEvent(self, _e):
        if not any(s.lines for s in self._sides.values()):
            return
        now = self._clock()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setRenderHint(QPainter.TextAntialiasing, True)
        bottom = self.height() - self._stroke * 2
        descent = self._fm.descent()
        space = self._fm.horizontalAdvance(" ")
        for name in ("me", "them"):                       # you at the bottom, the call above - never overlapping
            side = self._sides[name]
            rgb = self._colors.get(name, COLORS[name])
            for ln in side.lines:
                line_alpha = side.alpha
                if ln.leaving_at is not None:
                    line_alpha *= 1.0 - _ease_out((now - ln.leaving_at) / LEAVE_S)
                if line_alpha <= 0.003 or not ln.words:
                    continue
                baseline = bottom - ln.y - descent
                x = self._box_left()
                for word in ln.words:
                    a = line_alpha * self._word_alpha(word, now)
                    if a > 0.003:
                        self._draw_word(p, word.text, x, baseline, rgb, a)
                    x += self._fm.horizontalAdvance(word.text) + space
            if side.height > 0.5:
                bottom -= side.height + self._line_h * SIDE_GAP
        p.end()

    def _draw_word(self, p, text, x, baseline, rgb, alpha):
        path = self._path(text)
        p.save()
        p.translate(x, baseline)
        soft = QPen(QColor(*OUTLINE, int(70 * alpha)), self._stroke * 2.2, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
        p.strokePath(path, soft)
        edge = QPen(QColor(*OUTLINE, int(225 * alpha)), self._stroke, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
        p.strokePath(path, edge)
        p.fillPath(path, QColor(*rgb, int(255 * alpha)))
        p.restore()


def caption_font(key, px):
    """The QFont of a captions.FONTS look at ``px`` pixels (the first of its families on this computer; Qt falls back
    for scripts the font lacks, e.g. Armenian in Georgia)."""
    families, weight = captions.FONT_FAMILIES.get(key, captions.FONT_FAMILIES["clean"])
    font = QFont(QGuiApplication.font())
    if families:
        try:
            font.setFamilies(list(families) + [QGuiApplication.font().family()])
        except Exception:
            font.setFamily(families[0])
    font.setPixelSize(int(px))
    font.setWeight(QFont.Weight(int(weight)))
    font.setHintingPreference(QFont.PreferNoHinting)
    return font


def color_pair(key):
    """((r, g, b) of the call, (r, g, b) of you) for a captions.COLORS key (the first one when unknown)."""
    for k, _name, them, me in captions.COLORS:
        if k == key:
            return them, me
    return captions.COLORS[0][2], captions.COLORS[0][3]


__all__ = ["COLORS", "CaptionsOverlay", "caption_font", "color_pair"]
