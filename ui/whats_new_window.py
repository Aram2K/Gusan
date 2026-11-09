"""The "What's new" window: shown once after Gusan was updated, and from Settings > Updates & about. Each change
is one row - a coloured tile with its icon, a NEW / BETTER / FIXED chip, a short title and one line - and the
rows rise into place one after another when it opens. Content: whats_new.py.

    win = WhatsNewWindow(sections, current="2.1.4", previous="2.1.3", release_url="https://...")
    win.show()

A themed QDialog subclass (a bare QDialog is banned), non-modal and parentless; "Got it", Enter or Esc closes
it; "See all changes" opens the release page. Never in the way of a quit (closeEvent always accepts).
"""

import logging

from PySide6.QtCore import (Property, QEasingCurve, QParallelAnimationGroup, QPropertyAnimation, QSequentialAnimationGroup,
                            QSize, Qt, QUrl)
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QDialog, QFrame, QGraphicsOpacityEffect, QHBoxLayout, QLabel, QLayout, QScrollArea,
                               QSizePolicy, QVBoxLayout, QWidget)

import whats_new
from app_info import APP_NAME
from ui import theme, winfit
from ui.components import icons
from ui.components.buttons import Button
from ui.components.surfaces import WhiteCard

logger = logging.getLogger("transcribe")

WIDTH = 460                 # the card's width (logical pixels)
MAX_LIST = 470              # taller than this, the list scrolls
STAGGER_MS = 70             # one row after another
RISE_MS = 340
RISE_PX = 14


def _text(text, px, weight=400, role="body", parent=None, wrap=False):
    lb = QLabel(text, parent)
    lb.setTextFormat(Qt.PlainText)
    theme.set_role(lb, role)                # its colour (the role also sets a font: ours comes after)
    lb.setFont(theme.font(px, weight))
    lb.setWordWrap(wrap)
    return lb


class _Chip(QLabel):
    def __init__(self, word, family, parent=None):
        super().__init__(word.upper(), parent)
        self.setFont(theme.font(10, 700, letter_spacing=0.4))
        self.setAlignment(Qt.AlignCenter)
        self.setFixedHeight(18)
        self.setContentsMargins(7, 0, 7, 0)
        self.setStyleSheet("QLabel { color: %s; background: %s; border-radius: 9px; }"
                           % (theme.qss_color(family + "-tx"), theme.qss_color(family + "-sub")))


class _Item(QWidget):
    """One change: tile, title + chip, one line."""

    def __init__(self, kind, title, line, parent=None):
        super().__init__(parent)
        word, icon, family = whats_new.KINDS.get(kind, whats_new.KINDS["better"])
        self.kind, self.title = kind, title
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(12)
        tile = QLabel(self)
        tile.setFixedSize(34, 34)
        tile.setAlignment(Qt.AlignCenter)
        tile.setPixmap(icons.pixmap(icon, 18, family + "-tx", sw=2.0))
        tile.setStyleSheet("QLabel { background: %s; border-radius: 10px; }" % theme.qss_color(family + "-sub"))
        lay.addWidget(tile, 0, Qt.AlignTop)
        col = QVBoxLayout()
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(2)
        head = QHBoxLayout()
        head.setContentsMargins(0, 0, 0, 0)
        head.setSpacing(8)
        self.title_label = _text(title, 13.5, 600, "body", self)          # short: one line, the chip after it
        head.addWidget(self.title_label, 0, Qt.AlignVCenter)
        self.chip = _Chip(word, family, self)
        head.addWidget(self.chip, 0, Qt.AlignVCenter)
        head.addStretch(1)
        col.addLayout(head)
        self.line_label = None
        if line:
            self.line_label = _text(line, 12.5, 400, "muted", self, wrap=True)
            col.addWidget(self.line_label)
        lay.addLayout(col, 1)
        self.setAccessibleName("%s: %s. %s" % (word, title, line or ""))


class _Reveal(QWidget):
    """Holds one row; its content rises ``lift`` pixels into place while it fades in."""

    def __init__(self, inner, parent=None):
        super().__init__(parent)
        self.inner = inner
        inner.setParent(self)
        self._lift = 0.0
        sp = QSizePolicy(QSizePolicy.Preferred, QSizePolicy.Preferred)
        sp.setHeightForWidth(True)
        self.setSizePolicy(sp)
        self.fx = QGraphicsOpacityEffect(self)
        self.fx.setOpacity(1.0)
        self.setGraphicsEffect(self.fx)

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, w):
        lay = self.inner.layout()
        if lay is not None and lay.hasHeightForWidth():
            return lay.totalHeightForWidth(w)
        return self.inner.sizeHint().height()

    def sizeHint(self):
        # (at the list's own width: a size asked before the layout places the row would be far too tall)
        return QSize(self.inner.sizeHint().width(), self.heightForWidth(WIDTH - 48))

    def minimumSizeHint(self):
        return QSize(0, 0)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._place()

    def _place(self):
        self.inner.setGeometry(0, int(round(self._lift)), self.width(), self.height())

    def get_lift(self):
        return self._lift

    def set_lift(self, value):
        self._lift = float(value)
        self._place()

    lift = Property(float, get_lift, set_lift)


class WhatsNewWindow(QDialog):
    def __init__(self, sections, current="", previous="", release_url="", parent=None, animate=True):
        super().__init__(parent)
        self.sections = list(sections or [])
        self.current, self.previous, self.release_url = str(current or ""), str(previous or ""), release_url
        theme.apply(self)
        self.setModal(False)
        self.setWindowFlag(Qt.WindowContextHelpButtonHint, False)
        self.setWindowTitle("What's new in %s" % APP_NAME)
        self.setAccessibleName("What's new in %s %s" % (APP_NAME, self.current))

        outer = QVBoxLayout(self)
        outer.setContentsMargins(18, 18, 18, 18)
        outer.setSizeConstraint(QLayout.SetFixedSize)
        self.card = WhiteCard(self, pad=(24, 22), gap=16)
        self.card.setFixedWidth(WIDTH)
        outer.addWidget(self.card)

        # Header: the logo, "What's new", the version (and the one it came from).
        head = QHBoxLayout()
        head.setSpacing(14)
        logo = QLabel(self.card)
        logo.setPixmap(icons.logo_pixmap(40))
        logo.setFixedSize(40, 40)
        logo.setAccessibleName(APP_NAME)
        head.addWidget(logo, 0, Qt.AlignVCenter)
        names = QVBoxLayout()
        names.setSpacing(1)
        self.title_label = _text("What's new", 20, 700, "h2", self.card)
        names.addWidget(self.title_label)
        sub = "%s %s" % (APP_NAME, self.current) if self.current else APP_NAME
        if self.previous:
            sub += " · updated from %s" % self.previous
        self.sub_label = _text(sub, 12.5, 400, "muted", self.card)
        names.addWidget(self.sub_label)
        head.addLayout(names, 1)
        self.card.add(head)

        # The changes, newest release first; a release label once there is more than one.
        listing = QWidget()
        lv = QVBoxLayout(listing)
        lv.setContentsMargins(0, 0, 0, 0)
        lv.setSpacing(12)
        self.rows = []
        self.items = []
        several = len(self.sections) > 1
        for version, items in self.sections:
            if several:
                lab = _text("Gusan %s" % version, 11.5, 700, "faint", listing)
                lab.setContentsMargins(0, 4, 0, 0)
                wrap = _Reveal(lab, listing)
                lv.addWidget(wrap)
                self.rows.append(wrap)
            for kind, title, line in items:
                item = _Item(kind, title, line)
                self.items.append(item)
                wrap = _Reveal(item, listing)
                lv.addWidget(wrap)
                self.rows.append(wrap)
        lv.addStretch(1)
        self.scroll = QScrollArea(self.card)
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll.setStyleSheet("QScrollArea, QScrollArea > QWidget > QWidget { background: transparent; }")
        self.scroll.setWidget(listing)
        self.listing = listing
        self._fit_list()
        self.card.add(self.scroll)

        foot = QHBoxLayout()
        foot.setContentsMargins(0, 4, 0, 0)
        foot.setSpacing(8)
        self.all_button = Button("See all changes", "link", "s", parent=self.card)
        self.all_button.clicked.connect(self._open_release)
        self.all_button.setVisible(bool(self.release_url))
        foot.addWidget(self.all_button, 0, Qt.AlignVCenter)
        foot.addStretch(1)
        self.ok_button = Button("Got it", "primary", "m", parent=self.card)
        self.ok_button.setAutoDefault(True)
        self.ok_button.setDefault(True)
        self.ok_button.clicked.connect(self.accept)
        foot.addWidget(self.ok_button, 0, Qt.AlignVCenter)
        self.card.add(foot)

        self._anim = None
        self._animate = bool(animate)
        if self._animate:
            for r in self.rows:
                r.fx.setOpacity(0.0)
                r.set_lift(RISE_PX)

    def _fit_list(self):
        """The list as tall as its rows (up to MAX_LIST; then it scrolls and the rows get the width left beside
        the scroll bar). Measured again once shown: the app's styles change the text's size."""
        lv = self.listing.layout()
        inner = WIDTH - 48                      # the card's width inside its padding
        lv.setContentsMargins(0, 0, 0, 0)
        # Measured as if the scroll bar were there: the rows are never taller than this at the full width, so a
        # list that fits never gets a scroll bar that would make it not fit.
        need = lv.totalHeightForWidth(inner - 14)
        if need > MAX_LIST:
            lv.setContentsMargins(0, 0, 12, 0)
        self.scroll.setFixedHeight(min(MAX_LIST, max(60, need)))

    # ── the rows rise into place, one after another ──
    def _start_animation(self):
        if not self._animate or self._anim is not None:
            return
        group = QParallelAnimationGroup(self)
        for i, r in enumerate(self.rows):
            seq = QSequentialAnimationGroup(group)
            seq.addPause(120 + i * STAGGER_MS)
            both = QParallelAnimationGroup(seq)
            fade = QPropertyAnimation(r.fx, b"opacity", both)
            fade.setStartValue(0.0)
            fade.setEndValue(1.0)
            fade.setDuration(RISE_MS)
            fade.setEasingCurve(QEasingCurve.OutCubic)
            rise = QPropertyAnimation(r, b"lift", both)
            rise.setStartValue(float(RISE_PX))
            rise.setEndValue(0.0)
            rise.setDuration(RISE_MS)
            rise.setEasingCurve(QEasingCurve.OutCubic)
            both.addAnimation(fade)
            both.addAnimation(rise)
            seq.addAnimation(both)
            group.addAnimation(seq)
        group.finished.connect(self.finish_animation)
        self._anim = group
        group.start()

    def finish_animation(self):
        """Every row in place (the end of the animation, or at once)."""
        if self._anim is not None:
            self._anim.stop()
        for r in self.rows:
            r.fx.setOpacity(1.0)
            r.set_lift(0.0)
        self._animate = False

    def showEvent(self, e):
        self.ensurePolished()
        for w in self.listing.findChildren(QWidget):
            w.ensurePolished()
        self._fit_list()
        super().showEvent(e)
        try:
            winfit.settle_on_screen(self)
        except Exception:
            logger.debug("fit on screen failed", exc_info=True)
        self.ok_button.setFocus()
        self._start_animation()

    def _open_release(self):
        if self.release_url:
            QDesktopServices.openUrl(QUrl(self.release_url))

    def closeEvent(self, e):
        self.finish_animation()
        e.accept()                      # never in the way of a quit
