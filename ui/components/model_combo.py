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

Rich rows (``fill_rows``, the speech pickers)
---------------------------------------------
A model list says what each row IS: the real model name, small chips for where it runs (LOCAL /
CLOUD) and, where a Pro plan is needed, PRO, and a muted second line ("Best accuracy · slower");
the rows sit under small headings for their group ("On this computer", "Gusan Cloud", "Your own
key"). The rows come from engine_choice (speech_row dicts) - the picker only draws them:

    box.fill_rows(engine_choice.live_speech_rows(cfg, pro))

Closed, the box shows the selected row's name and its chips (``closed_text()`` is the same as one
sentence). The headings are not rows: they cannot be picked and ``findData`` never finds them.

What a click does is up to the page (it reads ``currentIndexChanged``): a
marked row still selects like any other; the page puts the old choice back
and asks first (download / Pro window / Privacy mode). Because a pick can ask,
a marked picker only changes through its open list: the mouse wheel goes on
to the page (it scrolls) and Up / Down / PageUp / PageDown / Home / End open
the list instead of stepping through the rows.
"""

from PySide6.QtCore import QEvent, QPoint, QRect, QRectF, QSize, Qt
from PySide6.QtGui import QFont, QFontMetrics, QGuiApplication, QPainter, QPainterPath, QPixmap
from PySide6.QtWidgets import (QComboBox, QStyle, QStyledItemDelegate, QStyleOptionComboBox,
                               QStyleOptionViewItem, QStylePainter)

from ui import theme
from ui.components import icons

PLAIN_ROLE = Qt.UserRole + 1
MARK_ROLE = Qt.UserRole + 2          # "" | MARK_PRO | MARK_DOWNLOAD (row marks)
TITLE_ROLE = Qt.UserRole + 3         # a rich row's first line (the name; the box shows it closed)
NOTE_ROLE = Qt.UserRole + 4          # a rich row's muted second line
TAGS_ROLE = Qt.UserRole + 5          # a rich row's chips: ("local",) / ("cloud", "pro") ...
HEADING_ROLE = Qt.UserRole + 6       # True on a group heading (it is not a row)
TIP_ROLE = Qt.UserRole + 7           # a rich row's tooltip (the tags in words)
CLOSED_ROLE = Qt.UserRole + 8        # a rich row's short name: what the closed box says when the title does not fit
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
_CHIP_GAP = 4         # between two chips
_HEADING_H = 26       # a group heading's height
_ROW_H = 34           # a one-line row (the stylesheet's own item height)
_ROW_H_NOTE = 46      # a row with its muted second line
_NOTE_SCALE = 0.86    # the second line's size next to the name
_ARROW_ROOM = 34      # the closed box's drop-down arrow and its air, from the right edge
_MIN_NAME = 90        # a closed box keeps at least this much of the name (or all of it, if shorter)
_LIST_KEYS = (Qt.Key_Up, Qt.Key_Down, Qt.Key_PageUp, Qt.Key_PageDown, Qt.Key_Home, Qt.Key_End)


class _RowDelegate(QStyledItemDelegate):
    """The list's rows: the normal look, text never elided, the row's mark painted at the right
    edge - and, for a rich row (fill_rows), its name, its chips and its muted second line."""

    def __init__(self, box):
        super().__init__(box)
        self._box = box
        self._chip = {}                  # (tag, device pixel ratio) -> the chip's pixmap
        self._making = False             # a chip is being grabbed (see chip_pixmap)

    # ── the chips ──
    @staticmethod
    def _dpr():
        screen = QGuiApplication.primaryScreen()
        return screen.devicePixelRatio() if screen else 1.0

    def chip_pixmap(self, tag):
        """The pixmap of a speech tag's chip (local / cloud / pro)."""
        dpr = self._dpr()
        hit = self._chip.get((tag, dpr))
        if hit is not None and not hit.isNull():
            return hit
        # The real chip, grabbed: it is styled by the app's stylesheet through its place under the
        # box's window, so a chip can never drift from the PRO tag, LOCAL and CLOUD elsewhere in the
        # app. (Not a child of the box - a combo's children are not styled - and never of the list:
        # a new child of the list makes it ask the delegate for every row's size, which asks for this
        # chip again, without end.)
        if self._making:
            return QPixmap()
        self._making = True
        try:
            from ui.components.badges import tag_chip
            top = self._box.window()
            chip = tag_chip(tag, "s", parent=top if top is not self._box else self._box.view())
            chip.hide()
            chip.ensurePolished()
            chip.resize(chip.sizeHint())
            pm = self._rounded(chip.grab())
            chip.deleteLater()
        finally:
            self._making = False
        self._chip[(tag, dpr)] = pm
        return pm

    @staticmethod
    def _rounded(pm):
        """A grabbed chip with its corners cut to the pill's own radius: a widget's grab() fills
        the whole rectangle with the chip's colour (on screen the style clips it)."""
        ratio = max(1.0, pm.devicePixelRatio())
        out = QPixmap(pm.size())
        out.setDevicePixelRatio(ratio)
        out.fill(Qt.transparent)
        p = QPainter(out)
        p.setRenderHint(QPainter.Antialiasing, True)
        path = QPainterPath()
        w, h = pm.width() / ratio, pm.height() / ratio
        path.addRoundedRect(QRectF(0, 0, w, h), h / 2.0, h / 2.0)
        p.setClipPath(path)
        p.drawPixmap(0, 0, pm)
        p.end()
        return out

    def forget_chips(self):
        self._chip.clear()

    def _pill_pixmap(self):
        return self.chip_pixmap("pro")

    def chips_width(self, tags):
        """The width of a row of chips (with the gaps between them); 0 for none."""
        tags = [t for t in (tags or ()) if t]
        if not tags:
            return 0
        return sum(self._logical_width(self.chip_pixmap(t)) for t in tags) + _CHIP_GAP * (len(tags) - 1)

    def paint_chips(self, painter, tags, right, top, height, opacity=1.0):
        """Draw the chips of ``tags`` ending at x=``right``, centred in [top, top+height).
        Returns the x they begin at."""
        tags = [t for t in (tags or ()) if t]
        x = right
        painter.save()
        if opacity < 1.0:
            painter.setOpacity(opacity)
        for tag in reversed(tags):
            pm = self.chip_pixmap(tag)
            w, h = self._logical_width(pm), self._logical_height(pm)
            x -= w
            painter.drawPixmap(QRect(x, top + (height - h) // 2, w, h), pm)
            x -= _CHIP_GAP
        painter.restore()
        return x + _CHIP_GAP

    # ── the marks ──
    def mark_pixmap(self, mark, hot=False):
        """The pixmap painted for ``mark`` (None for no mark)."""
        if mark == MARK_PRO:
            return self.chip_pixmap("pro")
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

    # ── rich rows ──
    @staticmethod
    def is_heading(index):
        return bool(index.data(HEADING_ROLE))

    @staticmethod
    def is_rich(index):
        return bool(index.data(TITLE_ROLE))

    @staticmethod
    def note_font(font):
        f = QFont(font)
        if f.pointSizeF() > 0:
            f.setPointSizeF(max(6.0, f.pointSizeF() * _NOTE_SCALE))
        return f

    @staticmethod
    def heading_font(font):
        f = QFont(font)
        if f.pointSizeF() > 0:
            f.setPointSizeF(max(6.0, f.pointSizeF() * 0.78))
        f.setWeight(QFont.Weight.Bold)
        f.setLetterSpacing(QFont.AbsoluteSpacing, 0.6)
        return f

    def row_width(self, index, font=None):
        """The width a row needs to show everything whole (text, chips, mark, padding)."""
        font = font or self._box.font()
        fm = QFontMetrics(font)
        if self.is_heading(index):
            return QFontMetrics(self.heading_font(font)).horizontalAdvance(
                str(index.data(Qt.DisplayRole) or "").upper()) + _TEXT_PAD
        if not self.is_rich(index):
            text_w = fm.horizontalAdvance(str(index.data(Qt.DisplayRole) or "")) + _TEXT_PAD
            mw = self.mark_width(index.data(MARK_ROLE) or "")
            return text_w + (_MARK_GAP + mw if mw else 0)
        top_w = fm.horizontalAdvance(str(index.data(TITLE_ROLE) or ""))
        chips_w = self.chips_width(index.data(TAGS_ROLE))
        mw = self.mark_width(index.data(MARK_ROLE) or "")
        top_w += (_MARK_GAP + chips_w if chips_w else 0) + (_CHIP_GAP + mw if mw else 0)
        note_w = QFontMetrics(self.note_font(font)).horizontalAdvance(str(index.data(NOTE_ROLE) or ""))
        return max(top_w, note_w) + _TEXT_PAD

    # ── Qt ──
    def initStyleOption(self, option, index):
        super().initStyleOption(option, index)
        option.textElideMode = Qt.ElideNone

    def sizeHint(self, option, index):
        if self.is_heading(index):
            return QSize(self.row_width(index, option.font), _HEADING_H)
        if self.is_rich(index):
            return QSize(self.row_width(index, option.font),
                         _ROW_H_NOTE if index.data(NOTE_ROLE) else _ROW_H)
        sh = super().sizeHint(option, index)
        w = self.mark_width(index.data(MARK_ROLE) or "")
        if w:
            sh.setWidth(sh.width() + _MARK_GAP + w)
        return sh

    def paint(self, painter, option, index):
        if self.is_heading(index):
            self._paint_heading(painter, option, index)
            return
        if self.is_rich(index):
            self._paint_rich(painter, option, index)
            return
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

    def _paint_heading(self, painter, option, index):
        painter.save()
        painter.setRenderHint(QPainter.TextAntialiasing, True)
        painter.setFont(self.heading_font(option.font))
        painter.setPen(theme.qcolor("muted"))
        r = option.rect.adjusted(_TEXT_PAD // 2 - 1, 0, -6, -3)
        painter.drawText(r, Qt.AlignLeft | Qt.AlignBottom, str(index.data(Qt.DisplayRole) or "").upper())
        painter.restore()

    def _paint_rich(self, painter, option, index):
        """The row's background as the style draws it (hover, current), then its name, the chips
        and the muted second line - on the colours of the app's list rows."""
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        opt.text = ""
        style = opt.widget.style() if opt.widget is not None else self._box.style()
        style.drawControl(QStyle.CE_ItemViewItem, opt, painter, opt.widget)
        enabled = bool(index.flags() & Qt.ItemIsEnabled)
        selected = bool(option.state & QStyle.State_Selected)
        hover = bool(option.state & QStyle.State_MouseOver)
        title_c = theme.qcolor("blue-tx" if selected else ("ink" if hover else "text"))
        note_c = theme.qcolor("blue-tx" if selected else "muted")
        if selected:
            note_c.setAlpha(190)
        r = option.rect
        left = r.left() + _TEXT_PAD // 2
        right = r.right() - (_TEXT_PAD // 2 - 1)
        note = str(index.data(NOTE_ROLE) or "")
        top_h = _ROW_H if not note else _ROW_H - 4
        top_y = r.top() + (2 if note else 0)
        painter.save()
        painter.setRenderHint(QPainter.TextAntialiasing, True)
        if not enabled:
            painter.setOpacity(0.5)
        # the right edge: the mark (a download icon), then the chips
        mark = index.data(MARK_ROLE) or ""
        if mark:
            pm = self.mark_pixmap(mark, selected or hover)
            if pm is not None:
                w, h = self._logical_width(pm), self._logical_height(pm)
                blank = _ICON_BLANK if mark == MARK_DOWNLOAD else 0
                x = right - w + 1 + blank
                painter.drawPixmap(QRect(x, top_y + (top_h - h) // 2, w, h), pm)
                right = x - _CHIP_GAP - blank
        tags = index.data(TAGS_ROLE) or ()
        chips_left = self.paint_chips(painter, tags, right, top_y, top_h) if tags else right + _MARK_GAP
        painter.setFont(option.font)
        painter.setPen(title_c)
        text_r = QRect(left, top_y, max(10, chips_left - _MARK_GAP - left), top_h)
        painter.drawText(text_r, Qt.AlignLeft | Qt.AlignVCenter, str(index.data(TITLE_ROLE) or ""))
        if note:
            painter.setFont(self.note_font(option.font))
            painter.setPen(note_c)
            painter.drawText(QRect(left, r.top() + top_h - 2, r.right() - left - 4, r.height() - top_h),
                             Qt.AlignLeft | Qt.AlignTop, note)
        painter.restore()


class PlainNameCombo(QComboBox):
    """Closed it shows the plain name ('Balanced'); the list shows 'Balanced · Whisper Small'.
    With fill_rows it is a speech picker: real names, chips and groups (see the module notes)."""

    _marks_on = False
    _delegate = None

    def changeEvent(self, e):
        # A new stylesheet / style (the page was themed after it was built, a scale change): the
        # chips grabbed under the old one are drawn again.
        if e.type() in (QEvent.StyleChange, QEvent.PaletteChange, QEvent.ScreenChangeInternal)                 and self._delegate is not None:
            self._delegate.forget_chips()
        super().changeEvent(e)

    def paintEvent(self, _e):
        p = QStylePainter(self)
        opt = QStyleOptionComboBox()
        self.initStyleOption(opt)
        title = self.currentData(TITLE_ROLE)
        if title and self._delegate is not None:
            self._paint_closed_rich(p, opt, str(title), str(self.currentData(CLOSED_ROLE) or title))
            return
        plain = self.currentData(PLAIN_ROLE)
        if plain:
            opt.currentText = plain
        p.drawComplexControl(QStyle.CC_ComboBox, opt)
        p.drawControl(QStyle.CE_ComboBoxLabel, opt)

    def _paint_closed_rich(self, p, opt, title, short):
        """The frame, then the selected row's name and chips. The whole title when it fits next
        to the chips ("Gemini 2.5 Pro · your Google key"), else the short name ("Gemini 2.5 Pro"),
        elided only as a last resort - in the middle, so the end that tells two names apart ("NVIDIA
        FastConformer Armenian") stays."""
        opt.currentText = ""
        p.drawComplexControl(QStyle.CC_ComboBox, opt)
        edit = self.style().subControlRect(QStyle.CC_ComboBox, opt, QStyle.SC_ComboBoxEditField, self)
        tags = list(self.current_tags())
        enabled = self.isEnabled()
        # up to the drop-down arrow (the stylesheet's right padding is its room, the chips may use it)
        right = self.rect().right() + 1 - _ARROW_ROOM
        fm = QFontMetrics(self.font())
        # The name comes first: in a box too narrow for it and every chip the last chips (PRO first)
        # give way, so a name is never cut to nothing.
        need = min(fm.horizontalAdvance(short), _MIN_NAME)
        while tags and right - edit.left() - self._delegate.chips_width(tags) - _MARK_GAP < need:
            tags.pop()
        chips_w = self._delegate.chips_width(tags)
        room = max(10, right - edit.left() - (chips_w + _MARK_GAP if chips_w else 0))
        text = title if fm.horizontalAdvance(title) <= room else fm.elidedText(short, Qt.ElideMiddle, room)
        p.setFont(self.font())
        p.setPen(theme.qcolor("text" if enabled else "faint"))
        p.drawText(QRect(edit.left(), edit.top(), room, edit.height()), Qt.AlignLeft | Qt.AlignVCenter, text)
        if chips_w:
            self._delegate.paint_chips(p, tags, right, edit.top(), edit.height(), 1.0 if enabled else 0.5)

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

    # ── rich rows (the speech pickers) ──
    def fill_rows(self, rows):
        """Fill the list from engine_choice's rows (speech_row dicts): a small heading above each
        group, then each row - its name, chips and muted second line. The old items are cleared;
        the signals stay as they were (a page blocks them while it refills)."""
        self.enable_row_marks()
        was = self.blockSignals(True)
        try:
            self.clear()
            group = None
            for row in rows:
                heading = str(row.get("group") or "")
                if heading and heading != group:
                    self.add_heading(heading)
                group = heading or group
                self.add_row(row)
        finally:
            self.blockSignals(was)

    def add_heading(self, text):
        """A group heading: not a row - it cannot be picked and has no value."""
        self.addItem(str(text), None)
        i = self.count() - 1
        self.setItemData(i, True, HEADING_ROLE)
        model = self.model()
        model.setData(model.index(i, self.modelColumn()), 0, _FLAGS_ROLE)       # no flags: not enabled, not selectable
        return i

    def add_row(self, row):
        """One rich row from a speech_row dict (value, title, note, tags, line, tip)."""
        i = self.add_choice(str(row.get("line") or row.get("title") or ""), row.get("value"),
                            plain=str(row.get("closed") or row.get("title") or ""))
        self.setItemData(i, str(row.get("title") or ""), TITLE_ROLE)
        self.setItemData(i, str(row.get("closed") or row.get("title") or ""), CLOSED_ROLE)
        self.setItemData(i, str(row.get("note") or ""), NOTE_ROLE)
        self.setItemData(i, tuple(row.get("tags") or ()), TAGS_ROLE)
        tip = str(row.get("tip") or "")
        self.setItemData(i, tip, TIP_ROLE)
        self.setItemData(i, tip, Qt.ToolTipRole)
        spoken = str(row.get("line") or row.get("title") or "")
        if row.get("note"):
            spoken += ". " + str(row["note"])
        self.setItemData(i, spoken, Qt.AccessibleTextRole)
        return i

    def is_heading(self, index):
        return bool(self.itemData(index, HEADING_ROLE)) if 0 <= index < self.count() else False

    def headings(self):
        """The group headings, in order."""
        return [self.itemText(i) for i in range(self.count()) if self.is_heading(i)]

    def row_indexes(self):
        """The indexes of the real rows (everything but the headings)."""
        return [i for i in range(self.count()) if not self.is_heading(i)]

    def current_tags(self):
        """The chips of the selected row (rich rows), as tag ids: ("cloud", "pro")."""
        return tuple(self.currentData(TAGS_ROLE) or ())

    def row_tags(self, index):
        return tuple(self.itemData(index, TAGS_ROLE) or ()) if 0 <= index < self.count() else ()

    def row_note(self, index):
        return str(self.itemData(index, NOTE_ROLE) or "") if 0 <= index < self.count() else ""

    def closed_text(self):
        """What the closed box says, as one sentence: the name and its tags ("Gemini 2.5 Flash ·
        Cloud · Pro")."""
        title = self.currentData(TITLE_ROLE)
        if not title:
            return self.plain_text()
        words = " · ".join(t.capitalize() for t in self.current_tags())
        return f"{title} · {words}" if words else str(title)

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
        base = str(self.itemData(index, TIP_ROLE) or "")          # a rich row's own tooltip
        if base:
            tip = (base + ". " + tip) if tip else base
        if (self.itemData(index, MARK_ROLE) or "") == mark and (self.itemData(index, Qt.ToolTipRole) or "") == tip:
            return                                  # nothing new: no repaint (a download event asks for every row)
        self.setItemData(index, mark, MARK_ROLE)
        text = self.itemText(index)
        spoken = "%s, %s" % (text, _MARK_NOTE[mark]) if mark else text
        if self.itemData(index, TITLE_ROLE) and self.itemData(index, NOTE_ROLE):
            spoken += ". " + str(self.itemData(index, NOTE_ROLE))
        self.setItemData(index, spoken, Qt.AccessibleTextRole)
        self.setItemData(index, tip, Qt.ToolTipRole)

    def choice_mark(self, index):
        return str(self.itemData(index, MARK_ROLE) or "") if 0 <= index < self.count() else ""

    def popup_width_hint(self):
        """The list's width that shows every row whole: the widest row's text
        + its chips and mark + padding, and the list's own border (never less than the
        closed box)."""
        font = self.font()
        delegate = self._delegate
        widest = 0
        for i in range(self.count()):
            if delegate is not None:
                w = delegate.row_width(self.model().index(i, self.modelColumn()), font)
            else:
                w = QFontMetrics(font).horizontalAdvance(self.itemText(i)) + _TEXT_PAD
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


__all__ = ["HEADING_ROLE", "MARK_DOWNLOAD", "MARK_PRO", "MARK_ROLE", "NOTE_ROLE", "PLAIN_ROLE",
           "PlainNameCombo", "TAGS_ROLE", "TITLE_ROLE", "combo"]
