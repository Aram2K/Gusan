"""The Interpreter on the live card (ui/live_assist.py): the header chip, the thin row with the two
languages, the two directions and the delay, and the captions that take the live transcript's place.

    chip = InterpChip(bar)                      # off: "文A Interpreter"; on: "文A EN ⇄ JA" (gradient)
    row = DirectionRow(body)                    # You [English ▾] ⇄ [Japanese ▾] Anna   (mic) (headphones)  ● 1.8 s
    row.languages_picked.connect(...)           # (my code, their code) - a pick or the swap
    log = CaptionLog()                          # engine events in, HTML out
    log.event({"type": "phrase", "direction": "hear", "id": 3, "text": "..."})
    box.setHtml(log.html(colors, them="Anna"))

Mockup: docs/redesign/interpreter-live-card.png. The card's own window flags and capture exclusion
are untouched; everything here is ordinary child widgets painted in the card's glass theme. The one
window of its own - a picker's language list, a Qt.Popup - is the card's (grand)child, so the card's
popup guard (live_assist._PopupPrivacy) keeps it out of screen shares whenever the card is kept out.
"""

import html

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFontMetrics, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QAbstractButton, QFrame, QHBoxLayout, QLabel, QPushButton, QSizePolicy

from ui.components import icons
from ui.components.lang_picker import LangPicker

KEEP = 12                    # captions kept on the card (oldest go first)
SPEAKER_URL = "gusan-interp://speaker"
GOOD_DELAY_S = 2.5           # the delay shows green under this, amber above
CAPTIONS_PLACEHOLDER = "Captions show here as people talk."
ROW_H = 34                   # the direction row's height on the card
NAME_W = 64                  # the other person's name is cut ("…") past this many px
SPEAK_TEXT = "They hear me in %s"
HEAR_TEXT = "I hear them in %s"
SWAP_TEXT = "Swap the languages"
MY_VOICE_NOTE = "In my own voice"


def _esc(text):
    return html.escape(str(text or ""), quote=False)


def _units_prefix(live, consumed):
    """``live`` with the start ``consumed`` already shown as phrases taken off ("" when nothing
    is left)."""
    if consumed and live.startswith(consumed):
        return live[len(consumed):].strip()
    return live.strip()


class CaptionLog:
    """What the captions show, built from the engine's events (interpreter/pipeline.py):
    "live" (words being said), "phrase" (a piece cut for translation), "translation" (its
    translation, streamed then final). Each direction keeps its own live line."""

    def __init__(self, keep=KEEP):
        self.keep = keep
        self.clear()

    def clear(self):
        self.items = []                          # {"id", "dir", "src", "dst", "final"}
        self.live = {"hear": "", "speak": ""}
        self._consumed = {"hear": "", "speak": ""}

    def _item(self, pid):
        for it in reversed(self.items):
            if it["id"] == pid:
                return it
        return None

    def event(self, e):
        """Take one engine event; True when the captions changed."""
        t = e.get("type")
        d = e.get("direction") if e.get("direction") in ("hear", "speak") else "hear"
        if t == "live":
            text = str(e.get("text") or "")
            consumed = self._consumed[d]
            if consumed and not text.startswith(consumed):
                self._consumed[d] = ""             # a new utterance: nothing of it was cut yet
            self.live[d] = text
            return True
        if t == "phrase":
            text = str(e.get("text") or "").strip()
            live = self.live[d]
            rest = _units_prefix(live, self._consumed[d])
            if rest.startswith(text):
                # cut from the live words: they keep going, minus this piece
                at = live.find(text, len(self._consumed[d]))
                self._consumed[d] = live[:at + len(text)] if at >= 0 else ""
            else:
                self.live[d], self._consumed[d] = "", ""     # the utterance ended with this piece
            self.items.append({"id": e.get("id"), "dir": d, "src": text, "dst": "", "final": False})
            del self.items[:-self.keep]
            return True
        if t == "translation":
            it = self._item(e.get("id"))
            if it is None:
                return False
            it["dst"] = str(e.get("text") or "")
            it["final"] = bool(e.get("final"))
            return True
        if t == "session" and e.get("state") == "stopped":
            self.live = {"hear": "", "speak": ""}
            self._consumed = {"hear": "", "speak": ""}
            return True
        return False

    def live_text(self, d):
        return _units_prefix(self.live.get(d, ""), self._consumed.get(d, ""))

    def html(self, c, them="Them", speaker_url=SPEAKER_URL):
        """The captions as HTML for a QTextEdit. ``c``: colours {"text", "muted", "faint", "mine",
        "accent"}. Each phrase: who (small capitals), the original (small, grey), the translation
        (larger; yours in indigo with a small speaker)."""
        blocks = []

        def who(d):
            if d == "speak":
                return ("<span style='color:%s; font-size:10px; font-weight:700; letter-spacing:1px'>YOU</span>"
                        "&nbsp;<img src='%s' width='11' height='11'>" % (c["faint"], speaker_url))
            return ("<span style='color:%s; font-size:10px; font-weight:700; letter-spacing:1px'>%s</span>"
                    % (c["faint"], _esc(them.upper())))

        def block(d, src, dst, caret):
            ink = c["mine"] if d == "speak" else c["text"]
            out = "<p style='margin:0 0 1px 0'>%s</p>" % who(d)
            if src:
                out += "<p style='margin:0; color:%s; font-size:12px'>%s</p>" % (c["muted"], _esc(src))
            if dst or caret:
                tail = ("<span style='color:%s'>&#9614;</span>" % c["accent"]) if caret else ""
                out += ("<p style='margin:1px 0 0 0; color:%s; font-size:15px; font-weight:600'>%s%s</p>"
                        % (ink, _esc(dst), tail))
            return "<div>%s</div>" % out

        for it in self.items:
            blocks.append(block(it["dir"], it["src"], it["dst"], not it["final"] and bool(it["dst"])))
        for d in ("hear", "speak"):
            rest = self.live_text(d)
            if rest:
                blocks.append(block(d, rest, "", False))
        sep = "<hr style='border:none; height:1px; background-color:%s'>" % c.get("line", "#e2e8f0")
        return sep.join(blocks)


class InterpChip(QPushButton):
    """The header chip, round like the card's other buttons: off, the outlined "文A"; on, the same
    mark filled (blue to violet). The languages are not repeated here - the row under the header
    shows them (and lets you change them) - so the title keeps its room; the pair stays in the
    tooltip and the accessible name. A click turns it on or off at any time (the card asks the
    host; Free -> the Pro window)."""

    def __init__(self, parent=None):
        super().__init__("", parent)
        self.setObjectName("laInterp")
        self.setCursor(Qt.PointingHandCursor)
        self.setProperty("on", False)
        self.setAccessibleName("Turn the interpreter on")
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self._on = None
        self._ink = "#4f46e5"
        self.set_state(False)

    def set_state(self, on, pair="", ink="#4f46e5"):
        on = bool(on)
        changed = on != self._on
        self._on = on
        self._ink = ink
        self.pair = pair if on else ""
        self.setText("")
        self.setIcon(icons.icon("translate", 14, "#ffffff" if on else ink, 1.9))
        self.setIconSize(QSize(14, 14))
        self.setAccessibleName(("Interpreter on, %s. Turn it off" % pair) if on else "Turn the interpreter on")
        self.setFixedSize(26, 26)
        if changed or self.property("on") != on:
            self.setProperty("on", on)
            self.style().unpolish(self)
            self.style().polish(self)

    def is_on(self):
        return self._on


class IconToggle(QAbstractButton):
    """A small round icon button for the direction row. Checkable (the mic and the headphones): on,
    the accent fill and a white icon; off, a faint icon with a slash through it. Plain (the swap): a
    muted icon. A soft tint on hover; a ring when it has the keyboard focus."""

    SIZE = 24
    ICON = 14

    def __init__(self, icon_name, accessible="", checkable=True, parent=None):
        super().__init__(parent)
        self._icon = icon_name
        self.setCheckable(checkable)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setAttribute(Qt.WA_Hover, True)
        self.setFixedSize(self.SIZE, self.SIZE)
        self.setAccessibleName(accessible)
        self._accent, self._off, self._ink, self._hover = "#2563eb", "#64748b", "#475569", QColor(15, 23, 42, 22)
        self._kb_focus = False

    def set_colors(self, accent, off, ink=None, hover=None):
        self._accent, self._off = accent, off
        self._ink = ink or off
        if hover is not None:
            self._hover = QColor(hover)
        self.update()

    def sizeHint(self):
        return QSize(self.SIZE, self.SIZE)

    def _icon_pixmap(self, color, slash):
        """The icon, with a slash cut through it when ``slash`` (a gap either side, so it reads)."""
        pm = icons.pixmap(self._icon, self.ICON, QColor(color), 1.9)
        if not slash:
            return pm
        dpr = pm.devicePixelRatio()
        out = QPixmap(pm.size())
        out.setDevicePixelRatio(dpr)
        out.fill(Qt.transparent)
        p = QPainter(out)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.drawPixmap(0, 0, pm)
        a, b = QPointF(1.5, 1.5), QPointF(self.ICON - 1.5, self.ICON - 1.5)
        p.setCompositionMode(QPainter.CompositionMode_Clear)
        p.setPen(QPen(Qt.black, 4.2, Qt.SolidLine, Qt.RoundCap))
        p.drawLine(a, b)
        p.setCompositionMode(QPainter.CompositionMode_SourceOver)
        p.setPen(QPen(QColor(color), 1.6, Qt.SolidLine, Qt.RoundCap))
        p.drawLine(a, b)
        p.end()
        return out

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        r = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        on = self.isCheckable() and self.isChecked()
        lit = self.underMouse() or self.isDown()
        p.setPen(Qt.NoPen)
        if on:
            fill = QColor(self._accent)
            if lit:
                fill = fill.darker(112)
            p.setBrush(fill)
            p.drawEllipse(r)
            color = "#ffffff"
        else:
            if lit:
                p.setBrush(self._hover)
                p.drawEllipse(r)
            color = self._off if self.isCheckable() else self._ink
        pm = self._icon_pixmap(color, self.isCheckable() and not on)
        p.drawPixmap(QPointF((self.width() - self.ICON) / 2.0, (self.height() - self.ICON) / 2.0), pm)
        if self.hasFocus() and self._kb_focus:
            p.setBrush(Qt.NoBrush)
            p.setPen(QPen(QColor(self._ink if on else self._accent), 1.3))
            p.drawEllipse(QRectF(self.rect()).adjusted(0.7, 0.7, -0.7, -0.7))
        p.end()

    def focusInEvent(self, e):
        self._kb_focus = e.reason() in (Qt.TabFocusReason, Qt.BacktabFocusReason, Qt.ShortcutFocusReason)
        super().focusInEvent(e)
        self.update()

    def focusOutEvent(self, e):
        super().focusOutEvent(e)
        self.update()


class DirectionRow(QFrame):
    """Under the header while the interpreter is on, in one glance: "You [English ▾] ⇄ [Japanese ▾]
    Anna", then the two directions as small icon toggles - the mic: they hear me in Japanese (Speak),
    the headphones: I hear them in English (Hear) - and the delay ("● 1.8 s": green under
    GOOD_DELAY_S, amber above).

    The pickers (ui/components/lang_picker.py) search all the interpreter's languages. Picking the
    other side's language swaps the two, like the Interpreter page; the swap button swaps them too.
    Either way the row shows the new pair at once and emits ``languages_picked(my, their)`` - the
    card writes the settings (a running interpreter starts again with them)."""

    toggled = Signal(str, bool)                  # "speak" | "hear", on
    languages_picked = Signal(str, str)          # (my code, their code)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("laInterpRow")
        self.setFixedHeight(ROW_H)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 0, 7, 0)
        lay.setSpacing(2)
        self.me_label = QLabel("You", self)
        self.me_label.setObjectName("laInterpWho")
        self.me_label.setTextFormat(Qt.PlainText)
        lay.addWidget(self.me_label)
        lay.addSpacing(1)
        self.my_picker = LangPicker("en", "I speak", "glass", self)
        self.my_picker.picked.connect(lambda code: self._picked("mine", code))
        lay.addWidget(self.my_picker)
        self.swap_btn = IconToggle("swap", SWAP_TEXT, checkable=False, parent=self)
        self.swap_btn.setToolTip(SWAP_TEXT)
        self.swap_btn.clicked.connect(self._swap)
        lay.addWidget(self.swap_btn)
        self.their_picker = LangPicker("hy", "They speak", "glass", self)
        self.their_picker.picked.connect(lambda code: self._picked("theirs", code))
        lay.addWidget(self.their_picker)
        lay.addSpacing(1)
        self.them_label = QLabel("Them", self)
        self.them_label.setObjectName("laInterpWho")
        self.them_label.setTextFormat(Qt.PlainText)
        lay.addWidget(self.them_label)
        lay.addStretch(1)
        self.speak_switch = IconToggle("mic", SPEAK_TEXT % "their language", parent=self)
        self.speak_switch.clicked.connect(lambda on: self._toggle("speak", on))
        lay.addWidget(self.speak_switch)
        lay.addSpacing(2)
        self.hear_switch = IconToggle("headphones", HEAR_TEXT % "my language", parent=self)
        self.hear_switch.clicked.connect(lambda on: self._toggle("hear", on))
        lay.addWidget(self.hear_switch)
        lay.addSpacing(6)
        self.delay = QLabel("", self)
        self.delay.setObjectName("laInterpDelay")
        self.delay.setAccessibleName("Delay")
        self.delay.setAlignment(Qt.AlignVCenter | Qt.AlignRight)
        lay.addWidget(self.delay)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._my, self._their = "en", "hy"
        self._them = "Them"
        self._my_voice = True
        self._hear_voice = ""
        self._ink = "#0f172a"
        self._fit_delay()
        self._refresh_names()

    # ── what the card tells it ──
    def set_theme(self, t, dark=False):
        self._ink = t["text"]
        hover = QColor(255, 255, 255, 34) if dark else QColor(15, 23, 42, 22)
        for b in (self.speak_switch, self.hear_switch, self.swap_btn):
            b.set_colors(t["accent"], t["faint"], t["muted"], hover)
        for p in (self.my_picker, self.their_picker):
            p.set_theme(t, dark=dark)
        self._fit_delay()

    def set_languages(self, my, their):
        """The pair on the pickers (codes)."""
        self._my, self._their = str(my or ""), str(their or "")
        self.my_picker.set_code(self._my)
        self.their_picker.set_code(self._their)
        self._refresh_names()

    def languages(self):
        return self._my, self._their

    def set_people(self, them="Them"):
        """The other person's first name ("Them" when nobody is known), cut to fit."""
        self._them = str(them or "").strip() or "Them"
        fm = QFontMetrics(self.them_label.font())
        self.them_label.setText(fm.elidedText(self._them, Qt.ElideRight, NAME_W))
        self.them_label.setToolTip(("%s speaks %s" % (self._them, self.their_picker.text()))
                                   if self.their_picker.text() else self._them)
        self.them_label.setAccessibleName(self._them)

    def set_voice(self, my_voice=True, hear_voice=""):
        """For the tooltips: they hear me in my own voice; the voice I hear them in ("Voice: ...")."""
        self._my_voice = bool(my_voice)
        self._hear_voice = str(hear_voice or "")
        self._refresh_names()

    def set_directions(self, speak_on, hear_on):
        for sw, on in ((self.speak_switch, speak_on), (self.hear_switch, hear_on)):
            if sw.isChecked() != bool(on):
                sw.setChecked(bool(on))
        self._refresh_names()

    def set_latency(self, seconds, good="#16a34a", slow="#d97706"):
        if seconds is None:
            self.delay.setText("")
            self.delay.setToolTip("")
            return
        color = good if seconds < GOOD_DELAY_S else slow
        self.delay.setText("<span style='color:%s'>&#9679;</span> %.1f s" % (color, float(seconds)))
        self.delay.setToolTip("About %.1f s behind the speaker" % float(seconds))

    def close_popups(self):
        for p in (self.my_picker, self.their_picker):
            p.close_popup()

    # ── inside ──
    def _fit_delay(self):
        """Room for "● 9.9 s" kept, so the toggles do not move when the delay shows up."""
        fm = QFontMetrics(self.delay.font())
        self.delay.setMinimumWidth(fm.horizontalAdvance("● 9.9 s") + 6)

    def _refresh_names(self):
        my, their = self.my_picker.text() or self._my, self.their_picker.text() or self._their
        for sw, text, note in ((self.speak_switch, SPEAK_TEXT % their, MY_VOICE_NOTE if self._my_voice else ""),
                               (self.hear_switch, HEAR_TEXT % my, self._hear_voice)):
            said = "%s: %s" % (text, "on" if sw.isChecked() else "off")
            sw.setAccessibleName(said)
            sw.setToolTip(said + ("\n" + note if note else ""))
        if self._them:
            self.them_label.setToolTip("%s speaks %s" % (self._them, their))

    def _toggle(self, name, on):
        self._refresh_names()
        self.toggled.emit(name, bool(on))

    def _picked(self, side, code):
        my, their = self._my, self._their
        if side == "mine":
            pair = (their, my) if code == their else (code, their)
        else:
            pair = (their, my) if code == my else (my, code)
        self.set_languages(*pair)
        self.languages_picked.emit(*pair)

    def _swap(self):
        pair = (self._their, self._my)
        self.set_languages(*pair)
        self.languages_picked.emit(*pair)

    def hideEvent(self, e):
        self.close_popups()
        super().hideEvent(e)


__all__ = ["CAPTIONS_PLACEHOLDER", "CaptionLog", "DirectionRow", "GOOD_DELAY_S", "IconToggle", "InterpChip",
           "SPEAKER_URL"]
