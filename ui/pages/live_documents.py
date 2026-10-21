"""The Documents section of the Live Assistant page: what the assistant may read.

    section = DocumentsSection(app, parent)       # a Card; the page puts it under "About my calls"
    section.reload()                              # the list, read on a worker thread
    section.add_paths(paths)                      # a pick or a drop: read off the GUI thread
    section.drag_enter(mime) / drop(mime)         # the page forwards its drag and drop here

One row per document: a file-type tile, the name, "12 pages · 340 KB", a switch "Use in
answers" (new ones start on: that is the person's choice of what the assistant may use) and
a remove. A file that cannot be read becomes a calm note row with its plain message (and an
x) instead of an error. Reading shows a strip with the file's name and a Cancel.

Safety, on this side of the glass:
* a file is only ever handed to live_docs / doc_reader (size-capped read, hard budgets,
  never raises) on a worker thread (ui.live_docs_ui.AddJob); nothing is opened or run;
* names and messages are shown as PLAIN text (Qt.PlainText, tooltips escaped), already
  stripped of control and bidi characters by doc_reader.safe_name;
* the small operations (a switch, a remove, Clear all) touch only the index and the stored files.

Keyboard: Tab reaches each row's switch and its x; Space (or Enter) flips "Use in answers",
Delete removes the document the focus is on. Every control has an accessible name.
"""
import html
import logging
import os

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (QFileDialog, QFrame, QHBoxLayout, QLabel, QSizePolicy, QVBoxLayout,
                               QWidget)

import live_docs
from ui import live_docs_ui, theme
from ui.components import Button, Card, IconButton, Toggle, icons, label, mini_prompt

logger = logging.getLogger("transcribe")

TITLE = "Documents"
ADD = "Add documents"
CLEAR = "Clear all"
USE_IN_ANSWERS = "Use in answers"
EMPTY = live_docs.EMPTY_LINE
EMPTY_TIP = "Click to choose files, or drop them here."
DROP = "Drop files to add them"
READING = "Reading %s"
STOPPING = "Stopping…"
FIRST_PART = "first part only"
MAX_NOTES = 12                      # notes about files that weren't added, newest first
STRIP_DELAY_MS = 350                # the reading strip shows only for a read that takes this long
MORE_FILES = "Gusan took the first %d files."
CLEAR_TITLE = "Remove all documents?"
CLEAR_BODY = "Gusan forgets them. Your files aren't touched."
CLEAR_YES, CLEAR_NO = "Remove all", "Keep them"

# What the footer says about where the words go (see refresh_privacy).
LINE_LOCAL = "Your documents stay on this computer."
LINE_UNDECIDED = ("Only the parts that match a question go to the AI that writes the answer, "
                  "and Gusan asks you first.")
LINE_YES = "Only the parts that match a question go to %s."
LINE_NO = "Documents stay on this computer, so cloud answers don't use them."
LINK_NO, LINK_YES = "Don't send", "Allow"

# file-type tile: kind -> (letters, ink, background)
_TILES = {
    "pdf": ("PDF", "#b91c1c", "#fee2e2"),
    "doc": ("DOC", "#1d4ed8", "#dbeafe"),
    "slides": ("PPT", "#92400e", "#fef3c7"),
    "sheet": ("XLS", "#15803d", "#dcfce7"),
    "book": ("EPUB", "#475569", "#f1f5f9"),
    "text": ("TXT", "#475569", "#f1f5f9"),
    "image": ("", "#0369a1", "#e0f2fe"),
    "alert": ("", "#b45309", "#fef3c7"),
    "info": ("", "#64748b", "#f1f5f9"),
}


def _alive(obj):
    try:
        import shiboken6
        return shiboken6.isValid(obj)
    except Exception:
        return True


def _h2(text, parent, px=16):
    lb = label(text, "h2", parent)
    lb.setFont(theme.font(px, 600, display=True))
    return lb


def _plain(text, role, parent=None, wrap=False):
    """A label that shows ``text`` exactly as it is: never as rich text or a link."""
    lb = label("", role, parent, wrap=wrap)
    lb.setTextFormat(Qt.PlainText)
    lb.setText(text or "")
    return lb


def _tip(text):
    """A tooltip that wraps, with the text escaped (never taken for HTML)."""
    return "<p style='white-space:normal'>%s</p>" % html.escape(text or "", quote=False) if text else ""


class _Elided(QLabel):
    """One plain line that ends in "…" when it doesn't fit; its full text is the tooltip."""

    def __init__(self, text="", role="label", parent=None):
        super().__init__(parent)
        self.setTextFormat(Qt.PlainText)
        theme.set_role(self, role)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self._full = ""
        self.set_full_text(text)

    def set_full_text(self, text):
        self._full = text or ""
        self.setToolTip(_tip(self._full))
        self.setAccessibleName(self._full)
        self._elide()

    def full_text(self):
        return self._full

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._elide()

    def _elide(self):
        super().setText(self.fontMetrics().elidedText(self._full, Qt.ElideRight, max(10, self.width())))

    def minimumSizeHint(self):
        return QSize(20, super().minimumSizeHint().height())

    def sizeHint(self):
        return QSize(self.fontMetrics().horizontalAdvance(self._full) + 4, super().sizeHint().height())


class FileTile(QWidget):
    """The file-type icon: a rounded square with the format's letters (PDF, DOC ...), a
    little landscape for a picture, or an alert / info mark for a note."""
    SIZE = 40

    def __init__(self, kind="text", parent=None):
        super().__init__(parent)
        self._kind = kind if kind in _TILES else "text"
        self.setFixedSize(self.SIZE, self.SIZE)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setAccessibleName("")

    def kind(self):
        return self._kind

    def set_kind(self, kind):
        self._kind = kind if kind in _TILES else "text"
        self.update()

    def paintEvent(self, _e):
        word, ink, bg = _TILES[self._kind]
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        r = QRectF(0.5, 0.5, self.width() - 1, self.height() - 1)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(bg))
        p.drawRoundedRect(r, 11, 11)
        if self._kind == "image":
            c = QColor(ink)
            p.setBrush(c)
            p.drawEllipse(QRectF(24.5, 11.5, 5, 5))                 # the sun
            hills = QPainterPath()
            hills.moveTo(9, 29)
            hills.lineTo(17, 18.5)
            hills.lineTo(22.5, 25)
            hills.lineTo(25.5, 21.5)
            hills.lineTo(31, 29)
            hills.closeSubpath()
            p.drawPath(hills)
        elif self._kind in ("alert", "info"):
            pm = icons.pixmap("alert" if self._kind == "alert" else "info", 20, ink, 1.9)
            p.drawPixmap(int((self.width() - pm.width() / pm.devicePixelRatio()) / 2),
                         int((self.height() - pm.height() / pm.devicePixelRatio()) / 2), pm)
        else:
            f = theme.font(10.5 if len(word) < 4 else 9, 800)
            p.setFont(f)
            p.setPen(QColor(ink))
            p.drawText(r, Qt.AlignCenter, word)
        p.end()


_ROW_QSS = """
QFrame#laDocRow { background: transparent; border: none; border-top: 1px solid %(line)s; }
QFrame#laDocRow QLabel[off="true"] { color: %(faint)s; }
QFrame#laNote { background: transparent; border: none; border-top: 1px solid %(line)s; }
"""


def _row_qss():
    return _ROW_QSS % {"line": theme.qss_color("line"), "faint": theme.qss_color("faint")}


def _meta(info):
    text = live_docs.describe(info)
    return text + " · " + FIRST_PART if getattr(info, "truncated", False) else text


def _row_name(info):
    state = "used in answers" if info.active else "not used in answers"
    return "%s, %s, %s" % (info.name, _meta(info), state)


class DocRow(QFrame):
    """One document. ``toggled(id, on)`` is "Use in answers", ``removed(id)`` the x."""
    toggled = Signal(str, bool)
    removed = Signal(str)
    HEIGHT = 60

    def __init__(self, info, parent=None):
        super().__init__(parent)
        self.info = info
        self.setObjectName("laDocRow")
        self.setStyleSheet(_row_qss())
        self.setFixedHeight(self.HEIGHT)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(8, 0, 4, 0)
        lay.setSpacing(12)
        self.tile = FileTile(live_docs.icon_for(info), self)
        lay.addWidget(self.tile, 0, Qt.AlignVCenter)
        col = QVBoxLayout()
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(1)
        self.name_label = _Elided(info.name, "label", self)
        self.meta_label = _Elided(_meta(info), "muted", self)
        col.addStretch(1)
        col.addWidget(self.name_label)
        col.addWidget(self.meta_label)
        col.addStretch(1)
        lay.addLayout(col, 1)
        self.toggle = Toggle("Use %s in answers" % info.name, bool(info.active), self)
        self.toggle.setToolTip(_tip(USE_IN_ANSWERS))
        self.toggle.toggled.connect(self._on_toggled)
        lay.addWidget(self.toggle, 0, Qt.AlignVCenter)
        self.remove_button = IconButton("x", "Remove %s" % info.name, size=30, icon_size=14,
                                        color="muted", parent=self)
        self.remove_button.setToolTip(_tip("Remove"))
        self.remove_button.clicked.connect(lambda: self.removed.emit(self.info.id))
        lay.addWidget(self.remove_button, 0, Qt.AlignVCenter)
        self._look()

    def set_info(self, info):
        """Show ``info`` (the same document, maybe a new switch state)."""
        self.info = info
        self.tile.set_kind(live_docs.icon_for(info))
        self.name_label.set_full_text(info.name)
        self.meta_label.set_full_text(_meta(info))
        self.toggle.setAccessibleName("Use %s in answers" % info.name)
        self.remove_button.setAccessibleName("Remove %s" % info.name)
        if self.toggle.isChecked() != bool(info.active):
            self.toggle.blockSignals(True)
            self.toggle.setChecked(bool(info.active))
            self.toggle.blockSignals(False)
            self.toggle.update()
        self._look()

    def has_focus(self):
        """True when the keyboard focus is on this row's switch or x."""
        return self.toggle.hasFocus() or self.remove_button.hasFocus()

    def _look(self):
        off = not self.info.active
        for lb in (self.name_label, self.meta_label):
            theme.set_prop(lb, "off", off, children=False)
        self.setAccessibleName(_row_name(self.info))
        self.setAccessibleDescription("Delete removes it.")

    def _on_toggled(self, on):
        self.info = self.info._replace(active=bool(on))
        self._look()
        self.toggled.emit(self.info.id, bool(on))

    def keyPressEvent(self, e):
        """The switch and the x are the Tab stops; a key they don't use reaches the row:
        Delete removes the document the focus is on."""
        if e.key() == Qt.Key_Delete:
            self.removed.emit(self.info.id)
            e.accept()
            return
        super().keyPressEvent(e)


class NoteRow(QFrame):
    """A file that was not added: its name, why in plain words, and an x."""
    dismissed = Signal(object)

    def __init__(self, name, message, tone="warn", parent=None):
        super().__init__(parent)
        self.name, self.message, self.tone = name, message, tone
        self.setObjectName("laNote")
        self.setStyleSheet(_row_qss())
        self.setMinimumHeight(DocRow.HEIGHT)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(8, 8, 4, 8)
        lay.setSpacing(12)
        self.tile = FileTile("alert" if tone == "warn" else "info", self)
        lay.addWidget(self.tile, 0, Qt.AlignTop)
        col = QVBoxLayout()
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(1)
        self.name_label = _Elided(name, "label", self)
        self.message_label = _plain(message, "muted", self, wrap=True)
        self.message_label.setFont(theme.font(12.5))
        col.addWidget(self.name_label)
        col.addWidget(self.message_label)
        lay.addLayout(col, 1)
        self.dismiss_button = IconButton("x", "Dismiss: %s" % name, size=30, icon_size=14,
                                         color="muted", parent=self)
        self.dismiss_button.setToolTip(_tip("Dismiss"))
        self.dismiss_button.clicked.connect(lambda: self.dismissed.emit(self))
        lay.addWidget(self.dismiss_button, 0, Qt.AlignTop)
        self.setAccessibleName("%s: %s" % (name, message))


class _BusyBar(QWidget):
    """A thin bar with a soft segment sliding along it while a file is read."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(4)
        self._phase = 0.25
        self._timer = QTimer(self)
        self._timer.setInterval(33)
        self._timer.timeout.connect(self._step)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)

    def showEvent(self, e):
        super().showEvent(e)
        self._timer.start()

    def hideEvent(self, e):
        self._timer.stop()
        super().hideEvent(e)

    def _step(self):
        self._phase = (self._phase + 0.018) % 1.0
        self.update()

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        r = QRectF(0, 0, self.width(), self.height())
        p.setPen(Qt.NoPen)
        p.setBrush(theme.qcolor("line"))
        p.drawRoundedRect(r, 2, 2)
        seg = max(40.0, r.width() * 0.28)
        x = -seg + (r.width() + seg) * self._phase
        p.setClipRect(r)
        p.setBrush(theme.qcolor("blue-l"))
        p.drawRoundedRect(QRectF(x, 0, seg, r.height()), 2, 2)
        p.end()


class ReadingStrip(QFrame):
    """"Reading Annual report.pdf   2 of 5   Cancel" with a bar."""
    cancelled = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("laReading")
        self.setStyleSheet("QFrame#laReading { background: %s; border: 1px solid %s; border-radius: 12px; }"
                           % (theme.qss_color("blue-sub"), theme.qss_color("blue-line")))
        v = QVBoxLayout(self)
        v.setContentsMargins(14, 10, 8, 12)
        v.setSpacing(8)
        row = QHBoxLayout()
        row.setSpacing(10)
        self.text_label = _Elided("", "label", self)
        self.count_label = _plain("", "muted", self)
        self.cancel_button = Button("Cancel", "link", "s", parent=self)
        self.cancel_button.setAccessibleName("Stop reading")
        self.cancel_button.clicked.connect(self.cancelled.emit)
        row.addWidget(self.text_label, 1)
        row.addWidget(self.count_label, 0)
        row.addWidget(self.cancel_button, 0)
        v.addLayout(row)
        self.bar = _BusyBar(self)
        v.addWidget(self.bar)
        self.hide()

    def show_reading(self, name, index=1, total=1):
        self.text_label.set_full_text(READING % name)
        self.count_label.setText("%d of %d" % (index, total) if total > 1 else "")
        self.cancel_button.setEnabled(True)
        self.setAccessibleName(READING % name + (", %d of %d" % (index, total) if total > 1 else ""))
        self.show()

    def show_stopping(self):
        self.text_label.set_full_text(STOPPING)
        self.count_label.setText("")
        self.cancel_button.setEnabled(False)
        self.setAccessibleName(STOPPING)


class _EmptyBox(QFrame):
    """The empty state: one plain line, what is readable, and a dashed edge that says
    "drop here". A click opens the picker too."""
    clicked = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("laDocEmpty")
        self.setCursor(Qt.PointingHandCursor)
        self.setMinimumHeight(76)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(18, 14, 18, 14)
        lay.setSpacing(14)
        self.icon = QLabel(self)
        self.icon.setFixedSize(40, 40)
        self.icon.setAlignment(Qt.AlignCenter)
        self.icon.setPixmap(icons.pixmap("file", 20, "muted", 1.8))
        self.icon.setStyleSheet("background: %s; border-radius: 12px;" % theme.qss_color("bg2"))
        self.icon.setAccessibleName("")
        lay.addWidget(self.icon, 0, Qt.AlignVCenter)
        col = QVBoxLayout()
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(2)
        self.line = _plain(EMPTY, "label", self, wrap=True)
        col.addWidget(self.line)
        lay.addLayout(col, 1)
        self.setAccessibleName(EMPTY)
        self.setToolTip(_tip(EMPTY_TIP))

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.LeftButton and self.rect().contains(e.position().toPoint()):
            self.clicked.emit()
            e.accept()
            return
        super().mouseReleaseEvent(e)

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        pen = QPen(theme.qcolor("line2"), 1.5)
        pen.setStyle(Qt.CustomDashLine)
        pen.setDashPattern([4.0, 3.0])
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(QRectF(1, 1, self.width() - 2, self.height() - 2), 14, 14)
        p.end()
        super().paintEvent(e)


class _DropOverlay(QWidget):
    """Over the whole section while files are dragged onto the page."""

    def __init__(self, parent):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.hide()

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        r = QRectF(1.5, 1.5, self.width() - 3, self.height() - 3)
        fill = theme.qcolor("blue-l")
        fill.setAlpha(34)
        p.setBrush(fill)
        pen = QPen(theme.qcolor("blue-l"), 2)
        pen.setStyle(Qt.CustomDashLine)
        pen.setDashPattern([4.0, 3.0])
        p.setPen(pen)
        p.drawRoundedRect(r, 16, 16)
        font = theme.font(14.5, 600)
        p.setFont(font)
        w = p.fontMetrics().horizontalAdvance(DROP) + 40
        pill = QRectF(0, 0, w, 40)
        pill.moveCenter(QPointF(r.center().x(), min(r.center().y(), r.top() + 110)))   # in view, however long the list
        p.setBrush(QColor("#ffffff"))
        p.setPen(QPen(theme.qcolor("blue-line"), 1))
        p.drawRoundedRect(pill, 20, 20)
        p.setPen(theme.qcolor("blue-tx"))
        p.drawText(pill, Qt.AlignCenter, DROP)
        p.end()


class DocumentsSection(Card):
    """See the module docstring."""

    changed = Signal()                  # this section changed the documents (the page may care)

    def __init__(self, app, parent=None):
        super().__init__(parent, pad=(20, 16), gap=10)
        self.app = app
        self.setAccessibleName(TITLE)
        self._docs = []                 # DocInfo, newest first
        self._notes = []                # NoteRow, newest first
        self._rows = {}                 # id -> DocRow
        self._loaded = False
        self._gen = 0                   # bumped by every local change (a late reload must not undo it)
        self._queue = live_docs_ui.AddQueue(self)
        self._reading = None            # (name, index, total) of the file being read
        self._strip_timer = QTimer(self)    # a quick read never flashes the strip
        self._strip_timer.setSingleShot(True)
        self._strip_timer.setInterval(STRIP_DELAY_MS)
        self._strip_timer.timeout.connect(self._show_strip)
        self._queue.started.connect(self._on_started)
        self._queue.added.connect(self._on_added)
        self._queue.batch_done.connect(self._on_finished)
        self._route = ""                # "cloud" | "own_key" | "local" | "none" | "" (the page tells)
        self._announcing = False
        self._last_dir = os.path.expanduser("~")

        head = QHBoxLayout()
        head.setContentsMargins(0, 0, 0, 0)
        head.setSpacing(8)
        self.title_label = _h2(TITLE, self)
        head.addWidget(self.title_label, 0, Qt.AlignVCenter)
        head.addStretch(1)
        self.add_button = Button(ADD, "secondary", "s", icon="plus", parent=self)
        self.add_button.setAccessibleName(ADD)
        self.add_button.clicked.connect(self.choose_files)
        head.addWidget(self.add_button, 0, Qt.AlignVCenter)
        self.add(head)

        self.strip = ReadingStrip(self)
        self.strip.cancelled.connect(self.cancel_reading)
        self.add(self.strip)

        self.col_head = QWidget(self)
        ch = QHBoxLayout(self.col_head)
        ch.setContentsMargins(0, 0, 4 + 30 + 12 - 2, 0)  # right edge over the switches, past the x
        # "Clear all" sits at the left of the column head, not beside "Add documents": in the
        # narrowest window the card is only ~320 px wide and the header's two buttons crowded
        # the second one off its edge.
        self.clear_button = Button(CLEAR, "link", "s", parent=self.col_head)
        self.clear_button.setAccessibleName("Clear all documents")
        self.clear_button.clicked.connect(self.clear_all)
        ch.addWidget(self.clear_button, 0, Qt.AlignVCenter)
        ch.addStretch(1)
        self.col_label = _plain(USE_IN_ANSWERS, "muted", self.col_head)
        self.col_label.setFont(theme.font(11.5, 500))
        ch.addWidget(self.col_label)
        self.add(self.col_head)

        self.list_box = QWidget(self)
        self.list_lay = QVBoxLayout(self.list_box)
        self.list_lay.setContentsMargins(0, 0, 0, 0)
        self.list_lay.setSpacing(0)
        self.add(self.list_box)

        self.empty_box = _EmptyBox(self)
        self.empty_box.clicked.connect(self.choose_files)
        self.add(self.empty_box)

        self.foot = QWidget(self)
        fl = QHBoxLayout(self.foot)
        fl.setContentsMargins(0, 2, 0, 0)
        fl.setSpacing(10)
        self.privacy_label = _plain("", "hint", self.foot, wrap=True)
        fl.addWidget(self.privacy_label, 1, Qt.AlignVCenter)
        self.privacy_link = Button(LINK_NO, "link", "s", parent=self.foot)
        self.privacy_link.clicked.connect(self._on_privacy_link)
        fl.addWidget(self.privacy_link, 0, Qt.AlignVCenter)
        self.add(self.foot)

        self._overlay = _DropOverlay(self)
        self.setTabOrder(self.add_button, self.clear_button)
        live_docs_ui.bridge().changed.connect(self._on_bridge)
        self._refresh_state()

    # ── what the page tells it ──
    def set_route(self, kind):
        """Who writes the answers now: "cloud" (Gusan Cloud), "own_key", "local", "none"."""
        self._route = str(kind or "")
        self.refresh_privacy()

    def focus_add(self):
        self.add_button.setFocus(Qt.TabFocusReason)

    # ── the list ──
    def documents(self):
        return list(self._docs)

    def reload(self):
        """Read the list on a worker; it lands on the GUI thread."""
        gen = self._gen
        try:
            import api_keys
            api_keys.run_then(lambda: live_docs.get_store().list(),
                              then=lambda docs, g=gen: self._reloaded(g, docs),
                              error=lambda exc, g=gen: self._reloaded(g, None, exc),
                              name="gusan-docs-list")
        except Exception:
            logger.warning("The documents couldn't be listed", exc_info=True)
            self._reloaded(gen, None)

    def _reloaded(self, gen, docs, exc=None):
        if not _alive(self):
            return
        if exc is not None:
            logger.warning("The documents couldn't be listed: %s", exc)
        if gen != self._gen:
            self.reload()               # something changed while it was reading: ask again
            return
        if docs is None:                # it couldn't be read: keep what is shown
            self._loaded = True
            self._refresh_state()
            return
        self.set_documents(docs)

    def set_documents(self, infos):
        """Show these documents (live_docs.DocInfo), newest first."""
        infos = sorted(list(infos or ()), key=lambda d: d.added, reverse=True)
        same = [d.id for d in infos] == [d.id for d in self._docs] and len(self._rows) == len(infos)
        self._docs = infos
        self._loaded = True
        if same:
            for d in infos:
                row = self._rows.get(d.id)
                if row is not None and row.info != d:
                    row.set_info(d)
            self._refresh_state()
        else:
            self._rebuild()

    def _rebuild(self, focus_id=None, focus_index=None):
        """Lay the notes and the documents out again. ``focus_id`` / ``focus_index``: put the
        keyboard focus back on that row (or the nearest one) - only for a keyboard user."""
        if focus_id is None and focus_index is None:
            for did, row in self._rows.items():
                if row.has_focus():
                    focus_id = did
        while self.list_lay.count():
            item = self.list_lay.takeAt(0)
            w = item.widget()
            if w is None:
                continue
            w.hide()
            if w not in self._notes:            # a note row is kept and laid out again
                w.deleteLater()
        self._rows = {}
        order = []
        for note in self._notes:
            self.list_lay.addWidget(note)
            note.show()
            order.append(note.dismiss_button)
        for info in self._docs:
            row = DocRow(info, self.list_box)
            row.toggled.connect(self._on_toggled)
            row.removed.connect(self._on_removed)
            self.list_lay.addWidget(row)
            self._rows[info.id] = row
            order.extend((row.toggle, row.remove_button))
        QWidget.setTabOrder(self.add_button, self.clear_button)
        prev = self.strip.cancel_button
        QWidget.setTabOrder(self.clear_button, prev)
        for w in order:
            QWidget.setTabOrder(prev, w)
            prev = w
        QWidget.setTabOrder(prev, self.privacy_link)
        self._refresh_state()
        target = None
        if focus_id in self._rows:
            target = self._rows[focus_id]
        elif focus_index is not None:
            rows = [self._rows[d.id] for d in self._docs]
            target = rows[min(focus_index, len(rows) - 1)] if rows else self.add_button
        if target is not None:
            (target.toggle if isinstance(target, DocRow) else target).setFocus(Qt.TabFocusReason)

    def _refresh_state(self):
        has_docs = bool(self._docs)
        has_notes = bool(self._notes)
        reading = self._queue.is_busy()
        self.col_head.setVisible(has_docs)
        self.list_box.setVisible(has_docs or has_notes)
        self.clear_button.setVisible(len(self._docs) >= 2)
        self.empty_box.setVisible(self._loaded and not has_docs and not has_notes and not reading)
        self.refresh_privacy()

    # ── the switches and the x ──
    def _on_toggled(self, doc_id, on):
        self._gen += 1
        try:
            found = live_docs.get_store().set_active(doc_id, bool(on))
        except Exception:
            logger.warning("A document switch couldn't be saved", exc_info=True)
            found = False
        self._docs = [d._replace(active=bool(on)) if d.id == doc_id else d for d in self._docs]
        self.refresh_privacy()          # the footer only speaks while something is switched on
        if not found:
            self.reload()               # it was gone: show what is really there
        self._emit_changed()

    def _on_removed(self, doc_id):
        self._gen += 1
        index = next((i for i, d in enumerate(self._docs) if d.id == doc_id), 0)
        row = self._rows.get(doc_id)
        keyboard = row is not None and row.has_focus()
        try:
            live_docs.get_store().remove(doc_id)
        except Exception:
            logger.warning("A document couldn't be removed", exc_info=True)
        self._docs = [d for d in self._docs if d.id != doc_id]
        self._rebuild(focus_index=index if keyboard else None)
        self._emit_changed()

    def clear_all(self):
        """Forget every document (after a plain question). True when it did."""
        if not self._docs:
            return False
        if not mini_prompt.ask(self, CLEAR_TITLE, CLEAR_BODY, CLEAR_YES, CLEAR_NO, icon="trash"):
            return False
        self._gen += 1
        try:
            live_docs.get_store().clear()
        except Exception:
            logger.warning("The documents couldn't be cleared", exc_info=True)
        self._docs = []
        self._rebuild()
        self._emit_changed()
        return True

    def _emit_changed(self):
        self._announcing = True
        try:
            live_docs_ui.bridge().changed.emit()
            self.changed.emit()
        finally:
            self._announcing = False

    def _on_bridge(self):
        if self._announcing or not _alive(self):
            return
        self.reload()                   # the card's menu (or another window) changed something

    # ── adding ──
    def ask_open_paths(self):
        """The file picker (every kind of file). Tests replace this."""
        paths, _ = QFileDialog.getOpenFileNames(self, "Add documents", self._last_dir, "All files (*)")
        if paths:
            self._last_dir = os.path.dirname(paths[0]) or self._last_dir
        return list(paths or [])

    def choose_files(self):
        paths = self.ask_open_paths()
        if paths:
            self.add_paths(paths)

    def add_paths(self, paths):
        """Read these files (a pick, a drop) on a worker thread, one after the other.
        Returns how many were taken."""
        paths = [os.fspath(p) for p in (paths or ()) if p]
        if not paths:
            return 0
        if len(paths) > live_docs_ui.MAX_BATCH:
            paths = paths[:live_docs_ui.MAX_BATCH]
            self._note("Documents", MORE_FILES % live_docs_ui.MAX_BATCH, "info")
        taken = self._queue.add(paths)
        self._refresh_state()
        return taken

    def is_reading(self):
        return self._queue.is_busy()

    def cancel_reading(self):
        """Stop: before the next file, and ask the one being read to stop."""
        if self._queue.is_busy():
            self._queue.cancel()
            self.strip.show_stopping()

    def _on_started(self, name, index, total):
        if self._queue.is_cancelled():
            return
        self._reading = (name, index, total)
        if self.strip.isVisibleTo(self):
            self.strip.show_reading(name, index, total)         # already showing: follow along
        elif not self._strip_timer.isActive():
            self._strip_timer.start()

    def _show_strip(self):
        if self._queue.is_busy() and self._reading and not self._queue.is_cancelled():
            self.strip.show_reading(*self._reading)

    def _on_added(self, result):
        if not _alive(self):
            return
        if getattr(result, "reason", "") != "cancelled":
            self._track(*live_docs.event_for(result))
        if result.ok:
            self._gen += 1
            self._docs = [d for d in self._docs if d.id != result.info.id]
            self._docs.insert(0, result.info)
            self._loaded = True
            self._add_row(result.info)              # just the new row: a big drop isn't laid out again each time
        elif result.reason != "cancelled":
            tone = "info" if result.reason == "duplicate" else "warn"
            self._note(result.name, result.message, tone)

    def _on_finished(self, results, cancelled):
        if not _alive(self):
            return
        if not self._queue.is_busy():
            self._strip_timer.stop()
            self._reading = None
            self.strip.hide()
        self._loaded = True
        if results:
            self._rebuild()                         # once per batch: the Tab order, tidy
        self._refresh_state()
        if any(r.ok for r in results or ()):
            self._emit_changed()

    def _add_row(self, info):
        """The row of a document that was just added, right under the notes."""
        old = self._rows.pop(info.id, None)
        if old is not None:
            old.hide()
            old.deleteLater()
        row = DocRow(info, self.list_box)
        row.toggled.connect(self._on_toggled)
        row.removed.connect(self._on_removed)
        self.list_lay.insertWidget(len(self._notes), row)
        self._rows[info.id] = row
        self._refresh_state()

    def _note(self, name, message, tone):
        for old in list(self._notes):
            if old.name == name and old.message == message:
                self._notes.remove(old)
                old.hide()
                old.deleteLater()
        note = NoteRow(name, message, tone, self.list_box)
        note.dismissed.connect(self._dismiss_note)
        self._notes.insert(0, note)
        self.list_lay.insertWidget(0, note)
        note.show()
        while len(self._notes) > MAX_NOTES:         # a folder of junk never floods the card
            gone = self._notes.pop()
            gone.hide()
            gone.deleteLater()
        self._refresh_state()

    def _dismiss_note(self, note):
        keyboard = note.dismiss_button.hasFocus()
        if note in self._notes:
            self._notes.remove(note)
        note.hide()
        note.deleteLater()
        self._rebuild(focus_index=0 if keyboard else None)

    def _track(self, event, props):
        fn = getattr(self.app, "track", None)
        if callable(fn):
            try:
                fn(event, props)
            except Exception:
                logger.debug("track(%s) failed", event, exc_info=True)

    # ── drag and drop (the page forwards its own) ──
    def drag_enter(self, mime):
        """A drag carrying files came over the page: True when they can be added."""
        ok = live_docs_ui.wants_files(mime)
        self._overlay.setGeometry(self.rect())
        self._overlay.setVisible(ok)
        if ok:
            self._overlay.raise_()
        return ok

    def drag_leave(self):
        self._overlay.hide()

    def drop(self, mime):
        """Files were dropped on the page. Returns how many were taken."""
        self._overlay.hide()
        paths, more = live_docs_ui.dropped_paths(mime)
        if more:
            self._note("Documents", MORE_FILES % live_docs_ui.MAX_BATCH, "info")
        return self.add_paths(paths)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._overlay.setGeometry(self.rect())

    # ── where the words go ──
    def privacy_line(self):
        """(text, link text) for the footer; ("", "") when there is nothing to say."""
        if not any(d.active for d in self._docs):       # none, or all switched off: nothing goes anywhere
            return "", ""
        cfg = getattr(self.app, "cfg", None)
        cfg = cfg if isinstance(cfg, dict) else {}
        if cfg.get("privacy_mode") or self._route in ("local", "none"):
            return LINE_LOCAL, ""
        choice = cfg.get(live_docs.CLOUD_CHOICE_KEY)
        agreed = str(cfg.get(live_docs.CLOUD_ROUTE_KEY) or "")
        here = {"cloud": "managed", "own_key": "cloud"}.get(self._route, "")
        if choice is True and agreed and here and agreed != here:
            choice = None               # the yes was for the other kind of cloud route: asked again
        if choice is True:
            where = "Gusan Cloud" if self._route == "cloud" else "your AI service"
            return LINE_YES % where, LINK_NO
        if choice is False:
            return LINE_NO, LINK_YES
        return LINE_UNDECIDED, ""

    def refresh_privacy(self):
        text, link = self.privacy_line()
        self.privacy_label.setText(text)
        if link:                        # (hidden without one, but it keeps a name)
            self.privacy_link.setText(link)
            self.privacy_link.setAccessibleName(link)
        self.privacy_link.setVisible(bool(link))
        self.foot.setVisible(bool(text))

    def _on_privacy_link(self):
        if self.privacy_link.text() == LINK_NO:
            live_docs_ui.set_choice(self.app, False)
        else:
            live_docs_ui.ask_cloud(self, self.app, managed=(self._route == "cloud"))
        self.refresh_privacy()
        self._emit_changed()


__all__ = ["DocRow", "DocumentsSection", "FileTile", "NoteRow", "ReadingStrip"]
