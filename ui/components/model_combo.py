"""PlainNameCombo: a model picker for a settings rail.

Closed it shows the plain name ("Balanced"); its list shows the whole line
("Balanced · Whisper Small"). Used by Audio to text's "Accuracy" and Smart
Actions' "Transcribes with".

    box = combo(PlainNameCombo(parent), "Accuracy", width=150)
    box.add_choice("Balanced · Whisper Small", "small", plain="Balanced")
    box.set_choice_enabled(box.findData("cloud"), False)     # e.g. Privacy mode

Row marks (opt-in, ``enable_row_marks()``)
------------------------------------------
Smart Actions' picker lists every model with what you can do with it: a real
PRO pill (the ui.components.ProTag look) on a Pro row for someone without Pro,
a download icon on a local model that is not on this computer yet, nothing
on a model that is. The marks are PAINTED by the list's delegate at the right
edge, so the text is never cut: the list is always as wide as its widest row
(text + mark + padding), even wider than the closed box, and is kept on
screen. A page that does not call ``enable_row_marks()`` (Audio to text)
keeps the plain Qt list.

    box.enable_row_marks()
    box.set_choice_mark(box.findData("cloud"), MARK_PRO)
    box.set_choice_mark(box.findData("small"), MARK_DOWNLOAD, note="460 MB")
    box.set_choice_mark(box.findData("base"), "")             # nothing

What a click does is up to the page (it reads ``currentIndexChanged``): a
marked row still selects like any other; the page puts the old choice back
and asks first (download / Pro window / Privacy mode). Because a pick can ask,
a marked picker only changes through its open list: the mouse wheel goes on
to the page (it scrolls) and Up / Down / PageUp / PageDown / Home / End open
the list instead of stepping through the rows.
"""

from PySide6.QtCore import QPoint, QRect, Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (QComboBox, QStyle, QStyledItemDelegate, QStyleOptionComboBox,
                               QStylePainter)

from ui import theme
from ui.components import icons

PLAIN_ROLE = Qt.UserRole + 1
MARK_ROLE = Qt.UserRole + 2          # "" | MARK_PRO | MARK_DOWNLOAD (row marks)
MARK_PRO = "pro"
MARK_DOWNLOAD = "download"
# Where QStandardItem keeps an item's flags. Rows are greyed out through the
# model index, never model.item(): PySide keeps that QStandardItem wrapper
# alive inside the model, so once clear() frees the item (a refill) the stale
# wrapper is handed out for whatever Qt allocates at its address next - an
# event in the Live Assistant's app-wide filter, and the app crashes.
_FLAGS_ROLE = Qt.UserRole - 1
_PICKABLE = Qt.ItemIsEnabled | Qt.ItemIsSelectable

_TEXT_PAD = 28        # a row's text inset: the item padding (10) + the focus margin, both sides
_MARK_GAP = 16        # at least this much between the text and a mark
_CHROME = 14          # the list's border + padding, both sides, and a little air
_ICON_PX = 16
_ICON_BLANK = 3       # blank margin inside the 16 px download icon (its arrow spans 5..19 of 24)
_SCROLLBAR = 18

_MARK_NOTE = {MARK_PRO: "Pro", MARK_DOWNLOAD: "not downloaded"}
_LIST_KEYS = (Qt.Key_Up, Qt.Key_Down, Qt.Key_PageUp, Qt.Key_PageDown, Qt.Key_Home, Qt.Key_End)


class _RowDelegate(QStyledItemDelegate):
    """The list's rows: the normal look, text never elided, and the row's mark
    painted at the right edge."""

    def __init__(self, box):
        super().__init__(box)
        self._box = box
        self._pill = {}                  # device pixel ratio -> the PRO pill's pixmap

    # ── the marks ──
    def _pill_pixmap(self):
        from ui.components.badges import ProTag
        dpr = QGuiApplication.primaryScreen().devicePixelRatio() if QGuiApplication.primaryScreen() else 1.0
        hit = self._pill.get(dpr)
        if hit is not None and not hit.isNull():
            return hit
        # The real ProTag, grabbed: it is styled by the app's stylesheet through
        # its place under the list, so the pill can never drift from the PRO tag.
        tag = ProTag("s", parent=self._box.view())
        tag.hide()
        tag.ensurePolished()
        tag.resize(tag.sizeHint())
        pm = tag.grab()
        tag.deleteLater()
        self._pill[dpr] = pm
        return pm

    def mark_pixmap(self, mark, hot=False):
        """The pixmap painted for ``mark`` (None for no mark)."""
        if mark == MARK_PRO:
            return self._pill_pixmap()
        if mark == MARK_DOWNLOAD:
            return icons.pixmap("download", _ICON_PX, "blue-tx" if hot else "muted", sw=2)
        return None

    @staticmethod
    def _logical_width(pm):
        return int(round(pm.width() / max(1.0, pm.devicePixelRatio())))

    @staticmethod
    def _logical_height(pm):
        return int(round(pm.height() / max(1.0, pm.devicePixelRatio())))

    def mark_width(self, mark):
        pm = self.mark_pixmap(mark)
        return self._logical_width(pm) if pm is not None else 0

    # ── Qt ──
    def initStyleOption(self, option, index):
        super().initStyleOption(option, index)
        option.textElideMode = Qt.ElideNone

    def sizeHint(self, option, index):
        sh = super().sizeHint(option, index)
        w = self.mark_width(index.data(MARK_ROLE) or "")
        if w:
            sh.setWidth(sh.width() + _MARK_GAP + w)
        return sh

    def paint(self, painter, option, index):
        super().paint(painter, option, index)
        mark = index.data(MARK_ROLE) or ""
        if not mark:
            return
        hot = bool(option.state & (QStyle.State_MouseOver | QStyle.State_Selected))
        pm = self.mark_pixmap(mark, hot)
        if pm is None:
            return
        w, h = self._logical_width(pm), self._logical_height(pm)
        r = option.rect
        x = r.right() - (_TEXT_PAD // 2 - 1) - w + 1
        if mark == MARK_DOWNLOAD:
            x += _ICON_BLANK            # the icon's own margin: its arrow lines up with the pill's edge
        y = r.top() + (r.height() - h) // 2
        painter.save()
        if not (index.flags() & Qt.ItemIsEnabled):
            painter.setOpacity(0.5)
        painter.drawPixmap(QRect(x, y, w, h), pm)
        painter.restore()


class PlainNameCombo(QComboBox):
    """Closed it shows the plain name ('Balanced'); the list shows 'Balanced · Whisper Small'."""

    _marks_on = False
    _delegate = None

    def paintEvent(self, _e):
        p = QStylePainter(self)
        opt = QStyleOptionComboBox()
        self.initStyleOption(opt)
        plain = self.currentData(PLAIN_ROLE)
        if plain:
            opt.currentText = plain
        p.drawComplexControl(QStyle.CC_ComboBox, opt)
        p.drawControl(QStyle.CE_ComboBoxLabel, opt)

    def add_choice(self, text, data, plain=None):
        """One item: ``text`` in the list, ``plain`` (or ``text``) when closed."""
        self.addItem(text, data)
        i = self.count() - 1
        if plain:
            self.setItemData(i, plain, PLAIN_ROLE)
        return i

    def plain_text(self):
        """What the closed box shows."""
        return self.currentData(PLAIN_ROLE) or self.currentText()

    def set_choice_enabled(self, index, enabled):
        """Grey an item out in the list (it can't be picked)."""
        model = self.model()
        idx = model.index(index, self.modelColumn())
        if not idx.isValid():
            return
        flags = model.flags(idx)
        flags = (flags | _PICKABLE) if enabled else (flags & ~_PICKABLE)
        model.setData(idx, int(getattr(flags, "value", flags)), _FLAGS_ROLE)

    def choice_enabled(self, index):
        model = self.model()
        idx = model.index(index, self.modelColumn())
        return bool(model.flags(idx) & Qt.ItemIsEnabled) if idx.isValid() else True

    # ── row marks (opt-in) ──
    def enable_row_marks(self):
        """Paint each row's mark (PRO pill / download icon) at its right edge,
        never elide a row, and size the list to its widest row."""
        if self._marks_on:
            return self
        self._marks_on = True
        self._delegate = _RowDelegate(self)
        self.setItemDelegate(self._delegate)
        view = self.view()
        view.setTextElideMode(Qt.ElideNone)
        view.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        return self

    def set_choice_mark(self, index, mark, note=""):
        """The mark on row ``index``: MARK_PRO, MARK_DOWNLOAD or "" (none). ``note``
        is the row's tooltip (e.g. "460 MB"); screen readers hear the mark too."""
        if not (0 <= index < self.count()):
            return
        mark = mark if mark in _MARK_NOTE else ""
        tip = ""
        if mark == MARK_DOWNLOAD:
            tip = "Not downloaded yet" + (" (%s)" % note if note else "")
        elif mark == MARK_PRO:
            tip = "Pro feature"
        if (self.itemData(index, MARK_ROLE) or "") == mark and (self.itemData(index, Qt.ToolTipRole) or "") == tip:
            return                                  # nothing new: no repaint (a download event asks for every row)
        self.setItemData(index, mark, MARK_ROLE)
        text = self.itemText(index)
        spoken = "%s, %s" % (text, _MARK_NOTE[mark]) if mark else text
        self.setItemData(index, spoken, Qt.AccessibleTextRole)
        self.setItemData(index, tip, Qt.ToolTipRole)

    def choice_mark(self, index):
        return str(self.itemData(index, MARK_ROLE) or "") if 0 <= index < self.count() else ""

    def popup_width_hint(self):
        """The list's width that shows every row whole: the widest row's text
        + its mark + padding, and the list's own border (never less than the
        closed box)."""
        fm = self.fontMetrics()
        mark_w = self._delegate.mark_width if self._delegate is not None else (lambda _m: 0)
        widest = 0
        for i in range(self.count()):
            w = fm.horizontalAdvance(self.itemText(i)) + _TEXT_PAD
            mw = mark_w(self.choice_mark(i))
            if mw:
                w += _MARK_GAP + mw
            widest = max(widest, w)
        extra = _SCROLLBAR if self.count() > self.maxVisibleItems() else 0
        return max(self.width(), widest + _CHROME + extra)

    # A pick may ask a question (download this model? turn Privacy mode off?),
    # so a marked picker is only changed through its open list: the mouse wheel
    # goes on to the page (it scrolls) and Up / Down open the list instead of
    # moving to the next model, which would ask about every one it passes.
    def wheelEvent(self, e):
        if self._marks_on:
            e.ignore()
            return
        super().wheelEvent(e)

    def keyPressEvent(self, e):
        if (self._marks_on and e.key() in _LIST_KEYS and not self.view().isVisible()
                and not (e.modifiers() & (Qt.ControlModifier | Qt.AltModifier | Qt.MetaModifier))):
            e.accept()
            self.showPopup()
            return
        super().keyPressEvent(e)

    def showPopup(self):
        if self._marks_on:
            self.view().setMinimumWidth(self.popup_width_hint())
        super().showPopup()
        if self._marks_on:
            self._place_popup()

    def _place_popup(self):
        """At least as wide as the widest row, and on screen: a list wider
        than the closed box lines up with the box's right edge when it would
        stick out of the window, then is kept inside the screen."""
        pop = self.view().window()
        if pop is None or pop is self.window():
            return
        need = self.popup_width_hint()
        geo = pop.frameGeometry()
        if geo.width() < need:
            pop.resize(need, pop.height())
            geo = pop.frameGeometry()
        x = geo.x()
        left = self.mapToGlobal(QPoint(0, 0)).x()
        right = left + self.width()
        top = self.window()
        if geo.width() > self.width() and top is not None and top is not pop:
            if x + geo.width() - 1 > top.frameGeometry().right():
                x = right - geo.width()
        screen = QGuiApplication.screenAt(self.mapToGlobal(QPoint(self.width() // 2, self.height() // 2))) \
            or self.screen() or QGuiApplication.primaryScreen()
        if screen is not None:
            avail = screen.availableGeometry()
            x = min(x, avail.right() - geo.width() + 1)
            x = max(x, avail.left())
        if x != geo.x():
            pop.move(x, geo.y())


def combo(box, accessible, width):
    """The rail's look for a combo: name, font, cursor and a fixed width."""
    box.setAccessibleName(accessible)
    box.setFont(theme.font(13.5))         # height: the stylesheet's (min-height 34)
    box.setCursor(Qt.PointingHandCursor)
    box.setFixedWidth(width)
    return box


__all__ = ["MARK_DOWNLOAD", "MARK_PRO", "MARK_ROLE", "PLAIN_ROLE", "PlainNameCombo", "combo"]
