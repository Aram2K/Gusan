"""A small modal question: a rounded white card, a title, one line, two buttons.

    if mini_prompt.ask(parent, "Download Balanced · Whisper Small?",
                       "460 MB - Smart Actions will use it once it is ready.",
                       "Download", "Not now", icon="download"):
        ...

Enter presses the first (yes) button, Esc the second ("Not now"). The card is
opaque white with a soft shadow (the dialog around it is see-through), centred
over the Gusan window. ``ask`` is the one call a page makes, so a test replaces
it (monkeypatch ``mini_prompt.ask``) instead of opening a window.

``confirm_download`` is the model download question (Smart Actions' picker);
the Privacy mode question lives in ui/privacy_prompt.py.
"""

import logging

from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QDialog, QFrame, QHBoxLayout, QLabel, QVBoxLayout

from ui import theme
from ui.components import icons
from ui.components.buttons import Button
from ui.components.surfaces import label

logger = logging.getLogger("transcribe")

_WIDTH = 392          # the card; the dialog adds the shadow's room around it
_SHADOW = 22


class MiniPrompt(QDialog):
    """The card. ``yes_button`` is the default (Enter), ``no_button`` is Esc."""

    def __init__(self, parent, title, body, yes_text, no_text, icon="info"):
        super().__init__(parent.window() if parent is not None else None,
                         Qt.Dialog | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setModal(True)
        self.setStyleSheet(theme.stylesheet())
        self.setFont(theme.font(14))
        self.setWindowTitle(title)
        self.setAccessibleName(title)
        self.setAccessibleDescription(body)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(_SHADOW, _SHADOW - 6, _SHADOW, _SHADOW + 6)
        self.card = QFrame(self)
        self.card.setObjectName("miniCard")
        self.card.setFixedWidth(_WIDTH)
        self.card.setStyleSheet(
            "QFrame#miniCard { background: %s; border: 1px solid %s; border-radius: 16px; }"
            % (theme.qss_color("white"), theme.qss_color("line2")))
        theme.elevate(self.card, "lg")
        outer.addWidget(self.card)

        lay = QVBoxLayout(self.card)
        lay.setContentsMargins(22, 20, 22, 18)
        lay.setSpacing(18)

        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.setSpacing(14)
        tile = QLabel(self.card)
        tile.setFixedSize(40, 40)
        tile.setAlignment(Qt.AlignCenter)
        tile.setPixmap(icons.pixmap(icon, 20, "blue-tx", sw=2))
        tile.setStyleSheet("QLabel { background: %s; border: none; border-radius: 12px; }"
                           % theme.qss_color("blue-sub"))
        tile.setAccessibleName("")
        top.addWidget(tile, 0, Qt.AlignTop)
        text = QVBoxLayout()
        text.setContentsMargins(0, 0, 0, 0)
        text.setSpacing(4)
        self.title_label = label(title, "h2", self.card, wrap=True)
        self.title_label.setFont(theme.font(15.5, 700))
        self.title_label.setAccessibleName(title)
        text.addWidget(self.title_label)
        self.body_label = label(body, "muted", self.card, wrap=True)
        self.body_label.setFont(theme.font(13, 400))
        self.body_label.setAccessibleName(body)
        text.addWidget(self.body_label)
        top.addLayout(text, 1)
        lay.addLayout(top)

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        row.addStretch(1)
        self.no_button = Button(no_text, "secondary", "m", parent=self.card)
        self.yes_button = Button(yes_text, "primary", "m", parent=self.card)
        for b in (self.no_button, self.yes_button):
            b.setAutoDefault(True)
            row.addWidget(b)
        self.yes_button.setDefault(True)
        self.no_button.clicked.connect(self.reject)
        self.yes_button.clicked.connect(self.accept)
        lay.addLayout(row)
        self.setTabOrder(self.yes_button, self.no_button)
        self.adjustSize()

    def showEvent(self, event):
        super().showEvent(event)
        self._centre()
        self.yes_button.setFocus(Qt.OtherFocusReason)

    def _centre(self):
        """Over the Gusan window (or the screen), and inside the screen."""
        try:
            host = self.parentWidget()
            if host is not None:
                c = host.mapToGlobal(host.rect().center())
            else:
                scr = self.screen() or QGuiApplication.primaryScreen()
                c = scr.availableGeometry().center()
            x, y = c.x() - self.width() // 2, c.y() - self.height() // 2
            scr = QGuiApplication.screenAt(QPoint(c.x(), c.y())) or self.screen() or QGuiApplication.primaryScreen()
            if scr is not None:
                a = scr.availableGeometry()
                x = max(a.left(), min(x, a.right() - self.width() + 1))
                y = max(a.top(), min(y, a.bottom() - self.height() + 1))
            self.move(x, y)
        except Exception:
            logger.debug("MiniPrompt placement failed", exc_info=True)


def ask(parent, title, body, yes_text, no_text, icon="info"):
    """Show the card; True when the first button (or Enter) was pressed. Tests replace this."""
    dlg = MiniPrompt(parent, title, body, yes_text, no_text, icon=icon)
    try:
        return dlg.exec() == QDialog.Accepted
    finally:
        dlg.deleteLater()


def download_title(plain, real=""):
    """'Download Balanced · Whisper Small?' (the plain name first, as everywhere)."""
    name = " · ".join(x for x in (plain, real) if x)
    return "Download %s?" % name


def download_body(size=""):
    """'460 MB - Smart Actions will use it once it is ready.'"""
    tail = "Smart Actions will use it once it is ready."
    return "%s - %s" % (size, tail) if size else tail


def confirm_download(parent, plain, real="", size=""):
    """Ask before a model starts downloading. True = download it."""
    return ask(parent, download_title(plain, real), download_body(size), "Download", "Not now", icon="download")


__all__ = ["MiniPrompt", "ask", "confirm_download", "download_body", "download_title"]
