"""A small searchable language picker: the language's name and a chevron; a click opens a little
rounded panel with a search box and a short list (the current language ticked).

    p = LangPicker(current="en", accessible="They speak", style="app")   # "glass": the live card
    p.picked.connect(fn)          # the code the person picked (never for set_code)
    p.set_code("hy"); p.code()
    p.set_codes([...])            # limit the list (default: ui.interpreter_host.LANG_CODES)
    p.set_theme(t, dark=False)    # glass: the card's colours (its _THEMES entry)
    p.open_popup()                # what a click does
    p.popup.search, p.popup.list  # the panel's search box and list

Keys: typing filters by name or code (best match first), Up / Down move, Enter picks the highlighted
row (the first match after typing), Esc closes; a click on a row picks it.

Never a QComboBox: changing a combo popup's window flags or making it translucent stopped its list
selecting (and crashed once). The panel is a Qt.Popup of its own, the picker's child - so the live
card's popup guard (ui/live_assist.py _PopupPrivacy) keeps it out of screen shares whenever the card
is kept out, and over a window that is kept out of shares (MainWindow.hidden_from_share) it is too.
"""

import logging
import unicodedata

from PySide6.QtCore import QEvent, QPoint, QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QCursor, QFont, QFontMetrics, QGuiApplication, QIcon, QPainter, QPalette, QPen
from PySide6.QtWidgets import (
    QAbstractButton, QAbstractItemView, QFrame, QLabel, QLineEdit, QListWidget, QListWidgetItem, QSizePolicy,
    QStyle, QStyledItemDelegate, QVBoxLayout, QWidget,
)

from ui import theme
from ui.components import icons

logger = logging.getLogger("transcribe")

STYLES = ("app", "glass")
SEARCH_PLACEHOLDER = "Search languages"
NO_MATCH = "No language found"
VISIBLE_ROWS = 8                 # the list shows about this many rows, then scrolls
SHADOW = 10                      # the panel's window: a transparent margin for its shadow
RADIUS = 10
GAP = 3                          # between the picker and the panel
CODE_ROLE = Qt.UserRole
CURRENT_ROLE = Qt.UserRole + 1
# Per look: the button's height and font, its side paddings, the panel's width, a row's height and font.
METRICS = {
    "app": {"h": 32, "font": 13.5, "pad_l": 10, "pad_r": 8, "panel_w": 236, "row": 30, "list_font": 13.5},
    "glass": {"h": 24, "font": 12, "pad_l": 8, "pad_r": 6, "panel_w": 214, "row": 26, "list_font": 12.5},
}
CHEVRON = 10                     # the chevron's box (px) and its gap after the name
CHEVRON_GAP = 4


def _default_codes():
    from ui import interpreter_host as ih
    return list(ih.LANG_CODES)


def lang_name(code):
    from ui import interpreter_host as ih
    return ih.lang_name(code)


def _fold(text):
    """Lower case without accents ("Español" -> "espanol")."""
    text = unicodedata.normalize("NFKD", str(text or "").strip().lower())
    return "".join(ch for ch in text if not unicodedata.combining(ch))


def filter_languages(items, query):
    """``items`` [(code, name)] that match ``query`` (name or code), best first: the exact code, a name
    that starts with it, a word that starts with it, a name that holds it, a code that starts with it.
    An empty query keeps every item in its order."""
    q = _fold(query)
    if not q:
        return list(items)
    ranked = []
    for i, (code, name) in enumerate(items):
        n, c = _fold(name), str(code or "").lower()
        if c == q:
            rank = 0
        elif n.startswith(q):
            rank = 1
        elif any(w.startswith(q) for w in n.replace("(", " ").replace("-", " ").split()):
            rank = 2
        elif q in n:
            rank = 3
        elif c.startswith(q):
            rank = 4
        else:
            continue
        ranked.append((rank, i, code, name))
    ranked.sort()
    return [(code, name) for _r, _i, code, name in ranked]


def _app_colors():
    tk = theme.TOKENS
    return {"text": tk["text"], "muted": tk["muted"], "faint": tk["faint"], "accent": tk["blue"],
            "rest": "rgba(15,23,42,0)", "hover": tk["bg2"], "panel": tk["white"], "line": tk["line2"],
            "field": tk["bg"], "field_line": tk["line"], "sel": "rgba(37,99,235,0.10)",
            "scroll": "rgba(15,23,42,0.20)", "shadow": "rgba(15,23,42,0.16)"}


def _glass_colors(t=None, dark=False):
    """The live card's look: ``t`` is its colour dict (text, muted, faint, accent)."""
    t = t or {}
    text = t.get("text") or ("#f8fafc" if dark else "#0f172a")
    muted = t.get("muted") or ("#cbd5e1" if dark else "#475569")
    faint = t.get("faint") or ("#94a3b8" if dark else "#64748b")
    accent = t.get("accent") or ("#60a5fa" if dark else "#2563eb")
    if dark:
        return {"text": text, "muted": muted, "faint": faint, "accent": accent,
                "rest": "rgba(255,255,255,0.07)", "hover": "rgba(255,255,255,0.15)", "panel": "#182031",
                "line": "rgba(255,255,255,0.16)", "field": "rgba(255,255,255,0.07)",
                "field_line": "rgba(255,255,255,0.14)", "sel": "rgba(96,165,250,0.24)",
                "scroll": "rgba(255,255,255,0.24)", "shadow": "rgba(0,0,0,0.42)"}
    return {"text": text, "muted": muted, "faint": faint, "accent": accent,
            "rest": "rgba(15,23,42,0.05)", "hover": "rgba(15,23,42,0.10)", "panel": "#f8fafc",
            "line": "rgba(15,23,42,0.14)", "field": "#ffffff", "field_line": "rgba(15,23,42,0.13)",
            "sel": "rgba(37,99,235,0.12)", "scroll": "rgba(15,23,42,0.22)", "shadow": "rgba(15,23,42,0.22)"}


def _qc(value):
    return theme.qcolor(value)


class _RowDelegate(QStyledItemDelegate):
    """One row: the name (the current one in the accent colour, ticked); the highlighted row tinted."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.colors = _glass_colors()
        self.row_h = 26
        self.font = QFont()

    def configure(self, colors, row_h, font):
        self.colors, self.row_h, self.font = dict(colors), int(row_h), QFont(font)

    def sizeHint(self, option, index):
        return QSize(max(1, option.rect.width()), self.row_h)

    def paint(self, p, option, index):
        c = self.colors
        p.save()
        p.setRenderHint(QPainter.Antialiasing, True)
        r = QRectF(option.rect).adjusted(1, 1, -1, -1)
        if option.state & QStyle.State_Selected:
            p.setPen(Qt.NoPen)
            p.setBrush(_qc(c["sel"]))
            p.drawRoundedRect(r, 6, 6)
        current = bool(index.data(CURRENT_ROLE))
        f = QFont(self.font)
        if current:
            f.setWeight(QFont.DemiBold)
        p.setFont(f)
        p.setPen(_qc(c["accent"] if current else c["text"]))
        text_r = r.adjusted(9, 0, -26, 0)
        name = str(index.data(Qt.DisplayRole) or "")
        p.drawText(text_r, Qt.AlignVCenter | Qt.AlignLeft,
                   QFontMetrics(f).elidedText(name, Qt.ElideRight, max(1, int(text_r.width()))))
        if current:
            pm = icons.pixmap("check", 14, _qc(c["accent"]), 2.2)
            p.drawPixmap(QPointF(r.right() - 21, r.center().y() - 7), pm)
        p.restore()


class LangPopup(QWidget):
    """The picker's panel: a search box over a short list. A Qt.Popup window of its own (a click
    outside closes it), the picker's child."""

    chosen = Signal(str)

    def __init__(self, picker):
        super().__init__(picker, Qt.Popup | Qt.FramelessWindowHint | Qt.NoDropShadowWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setObjectName("langPopup")
        self.setAccessibleName("Languages")
        self._picker = picker
        self._items = []
        self._current = ""
        self._open_at = QPoint()
        self._c = _glass_colors()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(SHADOW + 6, SHADOW + 6, SHADOW + 6, SHADOW + 6)
        lay.setSpacing(6)
        self.search = QLineEdit(self)
        self.search.setObjectName("lpSearch")
        self.search.setPlaceholderText(SEARCH_PLACEHOLDER)
        self.search.setAccessibleName(SEARCH_PLACEHOLDER)
        self._search_icon = self.search.addAction(QIcon(), QLineEdit.LeadingPosition)
        self._search_icon.setText("Search")
        # The line edit's icon button gets a name too (screen readers list it), like home.search_field.
        for b in self.search.findChildren(QAbstractButton):
            if not (b.text() or "").strip() and not (b.accessibleName() or "").strip():
                b.setAccessibleName("Search")
                b.setFocusPolicy(Qt.NoFocus)
        self.search.textChanged.connect(self._filter)
        self.search.installEventFilter(self)
        lay.addWidget(self.search)
        self.list = QListWidget(self)
        self.list.setObjectName("lpList")
        self.list.setAccessibleName("Languages")
        self.list.setFrameShape(QFrame.NoFrame)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.list.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.list.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.list.setSelectionMode(QAbstractItemView.SingleSelection)
        self.list.setUniformItemSizes(True)
        self.list.setMouseTracking(True)                 # the row under the pointer is the highlighted one
        self.list.viewport().setAutoFillBackground(False)
        self.list.setCursor(Qt.PointingHandCursor)
        self._delegate = _RowDelegate(self.list)
        self.list.setItemDelegate(self._delegate)
        self.list.entered.connect(self._hover)
        self.list.clicked.connect(self._pick_index)
        self.list.activated.connect(self._pick_index)      # Enter on the list itself
        self.list.installEventFilter(self)
        lay.addWidget(self.list)
        self.empty = QLabel(NO_MATCH, self)
        self.empty.setObjectName("lpEmpty")
        self.empty.setAlignment(Qt.AlignCenter)
        self.empty.hide()
        lay.addWidget(self.empty)

    # ── the look ──
    def set_look(self, colors, metrics):
        self._c = c = dict(colors)
        body = theme.font(metrics["list_font"], 400)
        self.search.setFont(body)
        self.list.setFont(body)
        self.empty.setFont(theme.font(12, 400))
        self._delegate.configure(c, metrics["row"], body)
        self._search_icon.setIcon(icons.icon("search", 14, _qc(c["faint"]), 1.9))
        pal = self.search.palette()
        pal.setColor(QPalette.PlaceholderText, _qc(c["faint"]))
        pal.setColor(QPalette.Text, _qc(c["text"]))
        self.search.setPalette(pal)
        search_h = metrics["row"] + 2
        self.search.setFixedHeight(search_h)
        list_h = VISIBLE_ROWS * metrics["row"] + 4
        self.list.setFixedHeight(list_h)
        self.empty.setFixedHeight(list_h)
        q = theme.qss_color
        self.setStyleSheet(
            "QLineEdit#lpSearch { background: %s; color: %s; border: 1px solid %s; border-radius: 7px;"
            " padding: 0px 6px 0px 0px; selection-background-color: %s; selection-color: #ffffff; }"
            "QLineEdit#lpSearch:focus { border-color: %s; }"
            "QListWidget#lpList { background: transparent; border: none; outline: 0; color: %s; }"
            "QLabel#lpEmpty { color: %s; background: transparent; }"
            "QScrollBar:vertical { background: transparent; width: 6px; margin: 2px 0px 2px 0px; }"
            "QScrollBar::handle:vertical { background: %s; border-radius: 3px; min-height: 24px; }"
            "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0px; }"
            "QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }"
            % (q(c["field"]), q(c["text"]), q(c["field_line"]), q(c["accent"]), q(c["accent"]),
               q(c["text"]), q(c["faint"]), q(c["scroll"])))
        self.setFixedSize(metrics["panel_w"] + 2 * SHADOW, 2 * SHADOW + 12 + search_h + 6 + list_h)
        self.update()

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        panel = QRectF(self.rect()).adjusted(SHADOW, SHADOW, -SHADOW, -SHADOW)
        shadow = _qc(self._c["shadow"])
        p.setPen(Qt.NoPen)
        steps = 6
        for i in range(steps, 0, -1):            # a soft shadow, a little lower than the panel
            col = QColor(shadow)
            col.setAlphaF(shadow.alphaF() * (steps - i + 1) / (steps * 3.5))
            p.setBrush(col)
            grow = float(i) * SHADOW / (steps + 1)
            p.drawRoundedRect(panel.adjusted(-grow, -grow + 3, grow, grow + 3), RADIUS + grow, RADIUS + grow)
        p.setBrush(_qc(self._c["panel"]))
        p.setPen(QPen(_qc(self._c["line"]), 1))
        p.drawRoundedRect(panel.adjusted(0.5, 0.5, -0.5, -0.5), RADIUS, RADIUS)
        p.end()

    # ── the rows ──
    def fill(self, items, current):
        """Show every language (``items`` [(code, name)]) with ``current`` ticked and in view; the
        search box empty."""
        self._items = list(items)
        self._current = str(current or "")
        self._open_at = QCursor.pos()
        self.search.blockSignals(True)
        self.search.clear()
        self.search.blockSignals(False)
        self._show_rows(self._items, first=False)

    def _filter(self, text):
        self._show_rows(filter_languages(self._items, text), first=bool(str(text).strip()))

    def _show_rows(self, rows, first):
        self.list.clear()
        mine = -1
        for i, (code, name) in enumerate(rows):
            it = QListWidgetItem(name)
            it.setData(CODE_ROLE, code)
            cur = code == self._current
            it.setData(CURRENT_ROLE, cur)
            it.setData(Qt.AccessibleTextRole, ("%s, selected" % name) if cur else name)
            self.list.addItem(it)
            if cur:
                mine = i
        has = bool(rows)
        self.list.setVisible(has)
        self.empty.setVisible(not has)
        if has:
            row = 0 if first or mine < 0 else mine
            self.list.setCurrentRow(row)
            self.list.scrollToItem(self.list.item(row), QAbstractItemView.PositionAtCenter
                                   if row == mine and not first else QAbstractItemView.EnsureVisible)
            sb = self.list.verticalScrollBar()
            step = max(1, self._delegate.row_h)
            sb.setValue(min(sb.maximum(), int(round(sb.value() / float(step))) * step))   # whole rows at the top

    def _hover(self, index):
        """The row under the pointer is the highlighted one - once the pointer moves (a list that
        opens under a still pointer keeps the current language highlighted)."""
        if QCursor.pos() != self._open_at:
            self._open_at = QPoint(-99999, -99999)
            self.list.setCurrentIndex(index)

    def codes(self):
        """The codes the list shows now, top to bottom."""
        return [self.list.item(i).data(CODE_ROLE) for i in range(self.list.count())]

    def _move(self, step):
        n = self.list.count()
        if not n:
            return
        row = self.list.currentRow()
        row = max(0, min(n - 1, (row if row >= 0 else -1 if step > 0 else n) + step))
        self.list.setCurrentRow(row)
        self.list.scrollToItem(self.list.item(row), QAbstractItemView.EnsureVisible)

    def pick_current(self):
        """Enter: the highlighted row (the first match after typing)."""
        it = self.list.currentItem() or (self.list.item(0) if self.list.count() else None)
        if it is not None:
            self._choose(it.data(CODE_ROLE))

    def _pick_index(self, index):
        if index.isValid():
            self._choose(index.data(CODE_ROLE))

    def _choose(self, code):
        if not self.isVisible():
            return                                 # (a click that also activates: picked once)
        self.hide()
        self.chosen.emit(str(code or ""))

    # ── keys and clicks ──
    def eventFilter(self, obj, e):
        if e.type() == QEvent.KeyPress:
            k = e.key()
            if k == Qt.Key_Escape:
                self.hide()
                return True
            if obj is self.search:
                steps = {Qt.Key_Down: 1, Qt.Key_Up: -1, Qt.Key_PageDown: VISIBLE_ROWS, Qt.Key_PageUp: -VISIBLE_ROWS}
                if k in steps:
                    self._move(steps[k])
                    return True
                if k in (Qt.Key_Return, Qt.Key_Enter):
                    self.pick_current()
                    return True
            elif obj is self.list:
                text = e.text()
                if text and text.isprintable() and not (e.modifiers() & (Qt.ControlModifier | Qt.AltModifier)):
                    self.search.setFocus(Qt.OtherFocusReason)       # typing in the list types in the search
                    self.search.insert(text)
                    return True
        return super().eventFilter(obj, e)

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Escape:
            self.hide()
            return
        super().keyPressEvent(e)

    def mousePressEvent(self, e):
        # A click outside closes it (Qt.Popup). One on the picker itself must not open it again.
        if not self.rect().contains(e.position().toPoint()):
            pk = self._picker
            on_picker = pk is not None and pk.rect().contains(pk.mapFromGlobal(e.globalPosition().toPoint()))
            self.setAttribute(Qt.WA_NoMouseReplay, bool(on_picker))
        super().mousePressEvent(e)

    def hideEvent(self, e):
        super().hideEvent(e)
        pk = self._picker
        if pk is not None:
            pk.update()
            if pk.isVisible():
                pk.setFocus(Qt.PopupFocusReason)

    def place(self, anchor):
        """Under ``anchor`` (left edges in line), or over it when the screen has no room below."""
        below = anchor.mapToGlobal(QPoint(0, anchor.height()))
        x = below.x() - SHADOW
        y = below.y() + GAP - SHADOW
        scr = anchor.screen() or QGuiApplication.screenAt(below) or QGuiApplication.primaryScreen()
        if scr is not None:
            a = scr.availableGeometry()
            if y + self.height() - SHADOW > a.bottom():
                above = anchor.mapToGlobal(QPoint(0, 0)).y() - GAP - self.height() + SHADOW
                if above + SHADOW >= a.top():
                    y = above
            x = max(a.left() - SHADOW, min(x, a.right() + 1 + SHADOW - self.width()))
        self.move(x, y)


class LangPicker(QAbstractButton):
    """See the module docstring. The button: the name and a small chevron, no border (a faint fill on
    the glass card, a tint on hover); its accessible name is "<accessible>: <language>"."""

    picked = Signal(str)

    def __init__(self, current="en", accessible="Language", style="app", parent=None):
        super().__init__(parent)
        self._style = style if style in STYLES else "app"
        self._m = METRICS[self._style]
        self._label = str(accessible or "Language")
        self._c = _app_colors() if self._style == "app" else _glass_colors()
        self._codes = None                          # None: every language the interpreter offers
        self._code = ""
        self._kb_focus = False
        self._compact = False                       # the short code ("EN") instead of the name
        self.setObjectName("langPicker")
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setAttribute(Qt.WA_Hover, True)
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        self.setFont(theme.font(self._m["font"], 600))
        self.setAccessibleDescription("Opens a list of languages to search")
        self.popup = LangPopup(self)
        self.popup.set_look(self._c, self._m)
        self.popup.chosen.connect(self._chosen)
        self.clicked.connect(lambda _on=False: self.open_popup())
        self.set_code(current)

    # ── the value ──
    def code(self):
        return self._code

    def set_code(self, code):
        """Show ``code`` (no signal)."""
        self._code = str(code or "")
        name = lang_name(self._code) if self._code else ""
        self.setText(name)
        said = ("%s: %s" % (self._label, name)) if name else self._label
        self.setAccessibleName(said)
        self.setToolTip(said)
        self.updateGeometry()
        self.update()

    def set_codes(self, codes):
        """Only these languages in the list (None or empty: all of them)."""
        self._codes = [str(c) for c in codes if c] if codes else None

    def items(self):
        """[(code, name)] the list offers, by name (the current one always among them)."""
        codes = list(self._codes) if self._codes is not None else _default_codes()
        if self._code and self._code not in codes:
            codes.append(self._code)
        return sorted(((c, lang_name(c)) for c in codes), key=lambda cn: _fold(cn[1]))

    def _chosen(self, code):
        if code and code != self._code:
            self.set_code(code)
            self.picked.emit(code)

    # ── the look ──
    def set_theme(self, t=None, dark=False):
        """Glass: the card's colours (``t``) in its light or dark look. The app look has one look."""
        self._c = _glass_colors(t, dark) if self._style == "glass" else _app_colors()
        self.popup.set_look(self._c, self._m)
        self.update()

    def _width_for(self, text):
        fm = QFontMetrics(self.font())
        return self._m["pad_l"] + fm.horizontalAdvance(text) + CHEVRON_GAP + CHEVRON + self._m["pad_r"]

    def short_text(self):
        """The short code ("EN", "HY")."""
        return self._code.split("-")[0].upper() if self._code else self.text()

    def full_width(self):
        """Its width with the whole name showing."""
        return self._width_for(self.text())

    def set_compact(self, on):
        """Show the short code instead of the name (a row too narrow for both names: LangPairRow)."""
        on = bool(on)
        if on != self._compact:
            self._compact = on
            self.updateGeometry()
            self.update()

    def is_compact(self):
        return self._compact

    def sizeHint(self):
        return QSize(self._width_for(self.short_text() if self._compact else self.text()), self._m["h"])

    def minimumSizeHint(self):
        # Room for the short code ("EN"): what it shows when the name doesn't fit (shown_text).
        return QSize(self._width_for("WW"), self._m["h"])

    def shown_text(self, room=None):
        """What the button shows in ``room`` px (default: its own): the name, or the short code
        ("EN") when it is compact or the name doesn't fit - never a cut name like "Eng…"."""
        if self._compact:
            return self.short_text()
        fm = QFontMetrics(self.font())
        if room is None:
            room = self.width() - self._m["pad_l"] - self._m["pad_r"] - CHEVRON - CHEVRON_GAP
        name = self.text()
        if not name or fm.horizontalAdvance(name) <= room or not self._code:
            return name
        return self.short_text()

    def paintEvent(self, _e):
        c = self._c
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        radius = min(8.0, r.height() / 2.0)
        lit = self.underMouse() or self.isDown() or self.popup.isVisible()
        fill = _qc(c["hover"] if lit else c["rest"])
        if fill.alpha():
            p.setPen(Qt.NoPen)
            p.setBrush(fill)
            p.drawRoundedRect(r, radius, radius)
        if self.hasFocus() and self._kb_focus:
            p.setBrush(Qt.NoBrush)
            p.setPen(QPen(_qc(c["accent"]), 1.4))
            p.drawRoundedRect(r.adjusted(0.5, 0.5, -0.5, -0.5), radius, radius)
        fm = QFontMetrics(self.font())
        left, right = self._m["pad_l"], self._m["pad_r"] + CHEVRON + CHEVRON_GAP
        room = max(1, self.width() - left - right)
        p.setFont(self.font())
        p.setPen(_qc(c["text"] if self.isEnabled() else c["faint"]))
        p.drawText(QRectF(left, 0, room, self.height()), Qt.AlignVCenter | Qt.AlignLeft,
                   fm.elidedText(self.shown_text(room), Qt.ElideRight, room))
        pm = icons.pixmap("chev_d", CHEVRON + 2, _qc(c["muted"]), 2.4)
        p.drawPixmap(QPointF(self.width() - self._m["pad_r"] - CHEVRON - 1, (self.height() - CHEVRON - 2) / 2.0), pm)
        p.end()

    # ── the panel ──
    def open_popup(self):
        """Open the panel under the picker: every language, the current one ticked, the search box
        focused."""
        if not self.isEnabled():
            return
        pop = self.popup
        pop.set_look(self._c, self._m)
        pop.fill(self.items(), self._code)
        pop.place(self)
        self._keep_out_of_shares()
        pop.show()
        pop.raise_()
        pop.search.setFocus(Qt.PopupFocusReason)
        self.update()

    def close_popup(self):
        if self.popup.isVisible():
            self.popup.hide()

    def _keep_out_of_shares(self):
        """Over a window that is kept out of screen shares (MainWindow.hidden_from_share) the panel is
        kept out too. (On the live card the card's own popup guard does this as the panel shows.)"""
        pop = self.popup
        want = bool(getattr(self.window(), "hidden_from_share", False))
        if want or pop.property("lp_private"):
            try:
                from ui import glass
                pop.winId()                           # the native window, before it maps
                glass.exclude_from_capture(pop, want)
                pop.setProperty("lp_private", want)
            except Exception:
                logger.debug("LangPicker: the list could not be kept out of shares", exc_info=True)

    # ── keys and focus ──
    def keyPressEvent(self, e):
        if e.key() in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_Space, Qt.Key_F4, Qt.Key_Down, Qt.Key_Up):
            self.open_popup()
            e.accept()
            return
        super().keyPressEvent(e)

    def focusInEvent(self, e):
        reason = e.reason()
        self._kb_focus = reason in (Qt.TabFocusReason, Qt.BacktabFocusReason, Qt.ShortcutFocusReason) or (
            reason == Qt.PopupFocusReason and self._kb_focus)
        super().focusInEvent(e)
        self.update()

    def focusOutEvent(self, e):
        super().focusOutEvent(e)
        self.update()

    def hideEvent(self, e):
        self.close_popup()
        super().hideEvent(e)

    def changeEvent(self, e):
        if e.type() == QEvent.FontChange:
            self.updateGeometry()
        super().changeEvent(e)


__all__ = ["LangPicker", "LangPopup", "NO_MATCH", "SEARCH_PLACEHOLDER", "filter_languages"]
