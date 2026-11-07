"""A Smart Action's answer about the selected text ("what does this mean?"): shown in a small card near the pill,
never pasted - it is on the clipboard too (docs/redesign/selected-text-spec.md). Also shows a changed text that had
nowhere to go (the cursor is on a web page): "Copied".

The card never takes the keyboard (the person keeps typing where they were); it closes with its ×, on the next take,
or after a while untouched.
"""
from PySide6.QtCore import QPoint, Qt, QTimer
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QTextBrowser, QVBoxLayout, QWidget

import rich_text
from ui import theme

WIDTH = 440
MAX_TEXT_H = 320
HIDE_AFTER_MS = 120_000          # untouched for two minutes: it goes


class AnswerCard(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool | Qt.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setAccessibleName("Smart Action answer")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 10, 10, 10)
        self.card = QFrame(self)
        self.card.setObjectName("answerCard")
        self.card.setStyleSheet(
            "QFrame#answerCard { background: %s; border: 1px solid %s; border-radius: 14px; }"
            % (theme.qss_color("white"), theme.qss_color("line")))
        outer.addWidget(self.card)
        lay = QVBoxLayout(self.card)
        lay.setContentsMargins(16, 12, 10, 14)
        lay.setSpacing(6)
        head = QHBoxLayout()
        head.setSpacing(8)
        self.title = QLabel("", self.card)
        self.title.setFont(theme.font(12.5, 700))
        self.title.setStyleSheet("color: %s;" % theme.qss_color("muted"))
        head.addWidget(self.title, 1)
        self.copy_button = self._small_button("Copy", "Copy the answer")
        self.copy_button.mousePressEvent = lambda _e: self.copy()
        head.addWidget(self.copy_button)
        self.close_button = self._small_button("✕", "Close")
        self.close_button.mousePressEvent = lambda _e: self.hide()
        head.addWidget(self.close_button)
        lay.addLayout(head)
        self.body = QTextBrowser(self.card)
        self.body.setOpenExternalLinks(True)
        self.body.setFrameShape(QFrame.NoFrame)
        self.body.setFocusPolicy(Qt.NoFocus)
        self.body.setAccessibleName("Answer")
        self.body.setFont(theme.font(14))
        self.body.setStyleSheet("QTextBrowser { background: transparent; color: %s; }" % theme.qss_color("text"))
        self.body.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.body.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        lay.addWidget(self.body)
        self._text = ""
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.hide)
        self.setFixedWidth(WIDTH)

    def _small_button(self, text, name):
        b = QLabel(text, self.card)
        b.setFont(theme.font(12.5, 600))
        b.setCursor(Qt.PointingHandCursor)
        b.setAccessibleName(name)
        b.setToolTip(name)
        b.setStyleSheet("QLabel { color: %s; padding: 2px 8px; border-radius: 8px; }"
                        "QLabel:hover { background: %s; }" % (theme.qss_color("text2"), theme.qss_color("line")))
        return b

    def text(self):
        return self._text

    def show_text(self, text, title="Answer", near=None):
        """Show ``text`` (Markdown, rendered) under ``title``; ``near``: the pill's window, to sit above it."""
        self._text = str(text or "")
        self.title.setText(title)
        self.body.setHtml(rich_text.qt_html(self._text, {"text": theme.qss_color("text"),
                                                        "muted": theme.qss_color("muted"), "item_gap": "3px"}))
        self.body.document().setTextWidth(WIDTH - 2 * 10 - 16 - 10 - 6)
        doc_h = int(self.body.document().size().height()) + 8
        self.body.setFixedHeight(max(40, min(MAX_TEXT_H, doc_h)))
        self.adjustSize()
        self._place(near)
        self.show()
        self.raise_()
        self._timer.start(HIDE_AFTER_MS)

    def _place(self, near):
        screen = None
        if near is not None and near.isVisible():
            screen = near.screen()
            g = near.frameGeometry()
            x = g.center().x() - self.width() // 2
            y = g.top() - self.height() - 6
        else:
            screen = QGuiApplication.primaryScreen()
        area = screen.availableGeometry() if screen is not None else None
        if near is None or not near.isVisible():
            if area is None:
                return
            x = area.right() - self.width() - 16
            y = area.bottom() - self.height() - 16
        if area is not None:
            x = max(area.left() + 8, min(x, area.right() - self.width() - 8))
            y = max(area.top() + 8, min(y, area.bottom() - self.height() - 8))
        self.move(QPoint(x, y))

    def copy(self):
        QGuiApplication.clipboard().setText(self._text)
        self.title.setText("Copied")
