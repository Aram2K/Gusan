"""A Smart Action's result that is not pasted, shown in a small card near the pill (docs/redesign/selected-text-spec.md):
an answer about the selected text ("what does this mean?"), or a changed text that had nowhere to go (the cursor is on
a web page) - both are on the clipboard already.

    [icon]  Copied · press Ctrl+V to paste             [x]
    the text, as formatted text (never raw Markdown)
                                               [ Copy ]   <- turns into "✓ Copied" for a moment

The card never takes the keyboard (the person keeps typing where they were); it closes with its ×, on the next Smart
Action, or after two minutes untouched.
"""
from PySide6.QtCore import QPoint, QSize, Qt, QTimer
from PySide6.QtGui import QColor, QGuiApplication
from PySide6.QtWidgets import (QFrame, QGraphicsDropShadowEffect, QHBoxLayout, QLabel, QScrollArea, QVBoxLayout,
                               QWidget)

import pill_copy
import rich_text
from ui import theme
from ui.components import Button, IconButton, icons

WIDTH = 420
MAX_TEXT_H = 300
HIDE_AFTER_MS = 120_000          # untouched for two minutes: it goes
COPIED_MS = 1600                 # how long the button says "Copied"
SHADOW = 18                      # room around the card for its shadow
BODY_W = WIDTH - 18 - 10 - 8      # the text's width inside the card (its margins, the gap kept for a scroll bar)

KIND_ANSWER, KIND_COPIED = "answer", "copied"


def sources_line(sources):
    """ "Sources · 1 Reuters · 2 ecb.europa.eu" as rich text, each name a link (only http(s) addresses); "" for none."""
    import html
    from urllib.parse import urlsplit
    try:
        from action_api import sanitize_web_sources
        rows = sanitize_web_sources(list(sources or ()))
    except Exception:
        rows = []
    if not rows:
        return ""
    links = []
    for n, row in enumerate(rows, 1):
        url = row["url"]
        name = (row.get("title") or urlsplit(url).hostname or url).strip()
        if len(name) > 40:
            name = name[:39].rstrip() + "\u2026"
        links.append('%d <a href="%s" style="color:%s; text-decoration:none">%s</a>'
                     % (n, html.escape(url, quote=True), theme.qss_color("blue-tx"), html.escape(name)))
    return "Sources \u00b7 " + " \u00b7 ".join(links)


def heading(kind):
    """The card's one line: what happened, in plain words."""
    if kind == KIND_COPIED:
        return "Copied · press %s to paste" % pill_copy.paste_keys()
    return "Answer"


class AnswerCard(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool | Qt.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setAccessibleName("Smart Action result")
        theme.apply(self)                                   # the app's buttons, text and thin scroll bar
        outer = QVBoxLayout(self)
        outer.setContentsMargins(SHADOW, SHADOW - 6, SHADOW, SHADOW + 6)
        self.card = QFrame(self)
        self.card.setObjectName("answerCard")
        self.card.setStyleSheet("QFrame#answerCard { background: %s; border: 1px solid %s; border-radius: 16px; }"
                                % (theme.qss_color("white"), theme.qss_color("line")))
        shadow = QGraphicsDropShadowEffect(self.card)
        shadow.setBlurRadius(28)
        shadow.setOffset(0, 6)
        shadow.setColor(QColor(15, 23, 42, 46))
        self.card.setGraphicsEffect(shadow)
        outer.addWidget(self.card)

        lay = QVBoxLayout(self.card)
        lay.setContentsMargins(18, 12, 10, 14)
        lay.setSpacing(8)
        head = QHBoxLayout()
        head.setSpacing(10)
        self.mark = QLabel(self.card)
        self.mark.setFixedSize(18, 18)
        head.addWidget(self.mark, 0, Qt.AlignVCenter)
        self.title = QLabel("", self.card)
        self.title.setFont(theme.font(13.5, 600))
        self.title.setStyleSheet("color: %s; background: transparent;" % theme.qss_color("ink"))
        head.addWidget(self.title, 1, Qt.AlignVCenter)
        self.close_button = IconButton("x", "Close", size=28, icon_size=15, color="muted", parent=self.card)
        self.close_button.setFocusPolicy(Qt.NoFocus)
        self.close_button.clicked.connect(self.hide)
        head.addWidget(self.close_button, 0, Qt.AlignVCenter)
        lay.addLayout(head)

        # The text: a wrapping label (it knows its own height for a width, so the card fits it exactly), in a scroll
        # area that only scrolls past MAX_TEXT_H. Selectable, links open.
        self.body = QLabel(self.card)
        self.body.setWordWrap(True)
        self.body.setTextFormat(Qt.RichText)
        self.body.setTextInteractionFlags(Qt.TextBrowserInteraction)
        self.body.setOpenExternalLinks(True)
        self.body.setFocusPolicy(Qt.NoFocus)
        self.body.setAccessibleName("Result")
        self.body.setFont(theme.font(13.5))
        self.body.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        self.body.setStyleSheet("QLabel { background: transparent; color: %s; padding: 0px; }" % theme.qss_color("text"))
        self.scroll = QScrollArea(self.card)
        self.scroll.setWidget(self.body)
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.scroll.setFocusPolicy(Qt.NoFocus)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }")
        self.scroll.viewport().setAutoFillBackground(False)
        self.body.setAutoFillBackground(False)
        body_row = QHBoxLayout()
        body_row.setContentsMargins(0, 0, 8, 0)
        body_row.addWidget(self.scroll)
        lay.addLayout(body_row)
        # The web pages a Smart Action's answer used (Linkup): "Sources · 1 Reuters · 2 ECB", each a link.
        self.sources = QLabel(self.card)
        self.sources.setWordWrap(True)
        self.sources.setTextFormat(Qt.RichText)
        self.sources.setOpenExternalLinks(True)
        self.sources.setTextInteractionFlags(Qt.TextBrowserInteraction)
        self.sources.setFocusPolicy(Qt.NoFocus)
        self.sources.setFont(theme.font(12))
        self.sources.setAccessibleName("Web sources")
        self.sources.setStyleSheet("QLabel { color: %s; background: transparent; }" % theme.qss_color("muted"))
        self.sources.hide()
        lay.addWidget(self.sources)

        foot = QHBoxLayout()
        foot.setContentsMargins(0, 2, 8, 0)
        foot.addStretch(1)
        self.copy_button = Button("Copy", "secondary", "s", icon="copy", parent=self.card)
        self.copy_button.setFocusPolicy(Qt.NoFocus)
        self.copy_button.setAccessibleName("Copy")
        self.copy_button.clicked.connect(self.copy)
        foot.addWidget(self.copy_button)
        lay.addLayout(foot)

        self._text = ""
        self._kind = KIND_ANSWER
        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.timeout.connect(self.hide)
        self._copied_timer = QTimer(self)
        self._copied_timer.setSingleShot(True)
        self._copied_timer.timeout.connect(self._copy_again)
        self.setFixedWidth(WIDTH + 2 * SHADOW)

    # ── what the app calls ──
    def text(self):
        return self._text

    def show_text(self, text, kind=KIND_ANSWER, near=None, sources=()):
        """Show ``text`` (Markdown, rendered); ``kind``: KIND_ANSWER or KIND_COPIED; ``near``: the pill, to sit above."""
        self._text = str(text or "")
        self._kind = kind if kind in (KIND_ANSWER, KIND_COPIED) else KIND_ANSWER
        self.title.setText(heading(self._kind))
        icon_name, color = ("check", "ok") if self._kind == KIND_COPIED else ("sparkle", "blue")
        self.mark.setPixmap(icons.pixmap(icon_name, 18, theme.qss_color(color)))
        self._copy_again()
        # The same type as every AI answer in the app (Ask about this meeting): 13.5 px, 21 px lines, its colours.
        html = rich_text.qt_html(rich_text.answer_text(self._text), {
            "text": theme.qss_color("text"), "muted": theme.qss_color("muted"), "head": theme.qss_color("ink"),
            "code_bg": theme.qss_color("bg2"), "item_gap": "3px"})
        self.body.setText("<div style='line-height:21px'>%s</div>" % html)
        text_h = self.body.heightForWidth(BODY_W)
        if text_h > MAX_TEXT_H:                                 # it scrolls: the bar takes some of the width
            text_h = MAX_TEXT_H
        self.scroll.setFixedHeight(max(20, text_h))
        self.scroll.verticalScrollBar().setValue(0)
        links = sources_line(sources)
        self.sources.setText(links)
        self.sources.setVisible(bool(links))
        self.setMinimumHeight(0)                                # forget the last text's height before measuring
        self.setMaximumHeight(16777215)
        for lay in (self.card.layout(), self.layout()):
            lay.invalidate()
            lay.activate()
        self.setFixedHeight(self.sizeHint().height())          # exactly as tall as its text: no empty space
        self._place(near)
        self.show()
        self.raise_()
        self._hide_timer.start(HIDE_AFTER_MS)

    def copy(self):
        QGuiApplication.clipboard().setText(self._text)
        self.copy_button.setText("Copied")
        self.copy_button.setIcon(icons.icon("check", 15, theme.qss_color("ok")))
        self.copy_button.setAccessibleName("Copied")
        self._copied_timer.start(COPIED_MS)
        self._hide_timer.start(HIDE_AFTER_MS)

    # ── inside ──
    def _copy_again(self):
        self.copy_button.setText("Copy")
        self.copy_button.setIcon(icons.icon("copy", 15, theme.qss_color("text2")))
        self.copy_button.setIconSize(QSize(15, 15))
        self.copy_button.setAccessibleName("Copy")

    def _place(self, near):
        """Above the pill when it is on screen, else at the bottom right of the screen."""
        screen = near.screen() if (near is not None and near.isVisible()) else QGuiApplication.primaryScreen()
        area = screen.availableGeometry() if screen is not None else None
        if near is not None and near.isVisible():
            g = near.frameGeometry()
            x = g.center().x() - self.width() // 2
            y = g.top() - self.height() + SHADOW - 8
        elif area is not None:
            x = area.right() - self.width() - 8
            y = area.bottom() - self.height() - 8
        else:
            return
        if area is not None:
            x = max(area.left(), min(x, area.right() - self.width()))
            y = max(area.top(), min(y, area.bottom() - self.height()))
        self.move(QPoint(x, y))
