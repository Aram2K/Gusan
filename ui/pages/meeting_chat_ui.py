"""Ask about this meeting: the card in the notes view's right column.

One card above the People card and the recording, for every meeting that has a transcript: the questions and
answers, a box to ask in (Enter sends, Shift+Enter starts a new line, Esc stops an answer that is coming), three
starter questions while the chat is empty, Clear chat, and a Copy under each answer. The brain is
meeting_chat.py (the prompt, which part of a long meeting to send, which AI answers, the calm sentences); the
conversation is the meeting's chat.json (meeting_store.load_chat; written here with views.write_latest: atomic, the
newest change wins, and a folder that is gone is never created again).

    card = ChatCard(notes_view)
    card.bind(folder, data, busy, final)    # NotesView.render: idempotent - the same meeting only updates the gate
    card.unbind()                           # another meeting is loading: a running answer ends ("stopped") in
                                            # its own folder and the card empties
    card.release()                          # the meeting is about to be trashed or deleted: nothing more is saved

A question runs on its own worker thread (SYNC_ASK = True runs it inline, for tests). The words stream in: the
worker hands the whole text so far to the window at most every 50 ms and the card draws it at most every
STREAM_RENDER_MS, with the app's real formatting (rich_text.answer_text -> qt_html: bold and lists, never raw
symbols, never HTML from the model, never a link or a picture). A question's text is shown as plain text, cut at
QUESTION_SHOWN characters (all of it is kept and sent). An answer that was stopped keeps its words, marked
"Stopped", and is never sent back to the model; one that failed is a calm sentence and Try again, and only the
question is kept. Nothing about a question or an answer goes anywhere but the AI engine: the one analytics event
is a count (meeting_chat_asked, the kind of engine).
"""

import logging
import os
import threading
import time

from PySide6.QtCore import QObject, QPoint, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QAccessible, QAccessibleEvent
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QPlainTextEdit, QScrollArea, QSizePolicy,
                               QVBoxLayout, QWidget)

import actions
import meeting_chat
import meeting_store
import rich_text
from ui import theme
from ui.components import Button, Card, label, mini_prompt
from ui.components import clipboard as rich_clipboard
from ui.pages import meeting_views as views

logger = logging.getLogger("transcribe")

SYNC_ASK = False               # tests: run a question inline
STREAM_RENDER_MS = 120         # a streaming answer is drawn at most this often
PARTIAL_MS = 50                # the worker hands the text over at most this often
QUESTION_SHOWN = 600           # a question's bubble shows this many characters (all of it is sent)
COUNTER_FROM = 3500            # the characters counter shows from here
INPUT_MIN_H, INPUT_MAX_H = 44, 120
PREFERRED_W = 400              # the card's (and so the right column's) width while the window has room
COPIED_MS = 1800

TITLE = "Ask about this meeting"
PLACEHOLDER = "Ask about this meeting…"
THINKING = "Thinking…"
WAITING_LOCAL = "Waiting for the AI on this computer…"
WRITING = "Writing…"
STOPPED = "Stopped"
NO_ANSWER_YET = "No answer yet"
FOOTER_NOTE = "AI can be wrong."
LOCAL_NOTE = "Stays on this computer."
CLEAR_TITLE = "Clear this chat?"
CLEAR_TEXT = "The questions and answers about this meeting are removed. The meeting itself stays."
HIDDEN_NOTE = "Hidden on screen share"


def _qss():
    c = theme.qss_color
    return f"""
QFrame[chat="bubble"] {{
    background: {c('blue-sub')}; border: 1px solid {c('blue-line')}; border-radius: 14px;
}}
"""


class _Bridge(QObject):
    """Worker thread -> GUI thread: the answer so far, and how a question ended."""
    partial = Signal(int, str)
    done = Signal(int, object)


class _Input(QPlainTextEdit):
    """The question box: Enter sends, Shift+Enter is a new line, Esc stops an answer that is coming. (While an input
    method is composing - Japanese, Chinese, Armenian layouts - Qt hands Enter to the method, not to this.)"""
    submit = Signal()
    stop_asked = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.running = False

    def keyPressEvent(self, e):                                   # noqa: N802 (Qt name)
        if e.key() in (Qt.Key_Return, Qt.Key_Enter) and not (e.modifiers() & Qt.ShiftModifier):
            e.accept()
            self.submit.emit()
            return
        if e.key() == Qt.Key_Escape and self.running:
            e.accept()
            self.stop_asked.emit()
            return
        super().keyPressEvent(e)


def _announce(widget):
    """Tell a screen reader something new is here (the finished answer, a failure) - never every streamed word."""
    try:
        if widget is not None and QAccessible.isActive():
            QAccessible.updateAccessibility(QAccessibleEvent(widget, QAccessible.Event.Alert))
    except Exception:
        logger.debug("accessibility alert failed", exc_info=True)


def _colors():
    return {"text": theme.qss_color("text"), "muted": theme.qss_color("muted"), "head": theme.qss_color("ink")}


class _AnswerRow(QWidget):
    """One answer: its words, a line of state under them, and the actions (Copy, Try again)."""

    def __init__(self, card, parent=None):
        super().__init__(parent)
        self.card = card
        self.msg = None
        lay = QVBoxLayout(self)
        lay.setContentsMargins(2, 0, 8, 0)
        lay.setSpacing(4)
        self.body = QLabel(self)
        self.body.setTextFormat(Qt.RichText)
        self.body.setWordWrap(True)
        self.body.setFont(theme.font(13.5))
        self.body.setOpenExternalLinks(False)
        self.body.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.body.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.body.hide()
        lay.addWidget(self.body)
        self.status = views.tlabel("", 12.5, 400, "muted", parent=self, wrap=True)
        self.status.setTextFormat(Qt.PlainText)                  # a sentence, never markup
        self.status.hide()
        lay.addWidget(self.status)
        self.note = views.tlabel("", 12, 400, "muted", parent=self, wrap=True)
        self.note.setTextFormat(Qt.PlainText)
        self.note.hide()
        lay.addWidget(self.note)
        self.actions = QHBoxLayout()
        self.actions.setContentsMargins(0, 0, 0, 0)
        self.actions.setSpacing(6)
        self.copy_btn = Button("Copy", "ghost", "s", icon="copy", parent=self)
        self.copy_btn.setAccessibleName("Copy this answer")
        self.copy_btn.clicked.connect(self._copy)
        self.retry_btn = Button("Try again", "secondary", "s", parent=self)
        self.retry_btn.clicked.connect(lambda: card.retry())
        self.actions.addWidget(self.copy_btn)
        self.actions.addWidget(self.retry_btn)
        self.actions.addStretch(1)
        lay.addLayout(self.actions)
        self.copy_btn.hide()
        self.retry_btn.hide()
        self._copied = QTimer(self)
        self._copied.setSingleShot(True)
        self._copied.setInterval(COPIED_MS)
        self._copied.timeout.connect(lambda: self.copy_btn.setText("Copy"))
        self.setAccessibleName("Answer")

    # the words
    def set_text(self, md, streaming=False):
        self.body.setText(views.with_line_height(
            rich_text.qt_html(rich_text.answer_text(md, streaming=streaming), _colors()), 21))
        self.body.setVisible(bool(str(md or "").strip()))
        self.setAccessibleDescription(rich_text.to_plain(str(md or ""))[:2000])

    def set_status(self, text, tone="muted"):
        self.status.setText(text)
        views.set_tone(self.status, tone)
        self.status.setVisible(bool(text))

    def set_note(self, text):
        self.note.setText(text)
        self.note.setVisible(bool(text))

    # the states
    def show_thinking(self, text=THINKING):
        self.set_text("")
        self.set_status(text)
        self.set_note("")
        self.copy_btn.hide()
        self.retry_btn.hide()

    def show_streaming(self, md):
        self.set_text(md, streaming=True)
        self.set_status(WRITING)
        self.copy_btn.hide()
        self.retry_btn.hide()

    def show_message(self, msg):
        """A kept answer (done, or stopped by the user)."""
        self.msg = msg
        self.set_text(msg.get("text") or "")
        self.set_status(STOPPED if msg.get("status") == "stopped" else "")
        self.set_note(meeting_chat.coverage_note(msg.get("coverage")))
        self.copy_btn.show()
        self.retry_btn.hide()

    def show_error(self, sentence, retry=True):
        self.set_text("")
        self.set_status(sentence, "text2")
        self.set_note("")
        self.copy_btn.hide()
        self.retry_btn.setVisible(retry)
        self.setAccessibleName("Answer, not available")

    def show_unanswered(self):
        self.set_text("")
        self.set_status(NO_ANSWER_YET)
        self.set_note("")
        self.copy_btn.hide()
        self.retry_btn.show()

    def _copy(self):
        text = (self.msg or {}).get("text") or ""
        if text and rich_clipboard.copy_rich(text):
            self.copy_btn.setText("Copied")
            self._copied.start()


class ChatCard(Card):
    """See the module notes."""

    def __init__(self, view, parent=None):
        super().__init__(parent or view, pad=(16, 14), gap=10, shadow=False)   # no shadow: a streaming repaint would
        self.view = view                                                       # go through its off-screen pixmap
        self.page = view.page
        self.setAccessibleName(TITLE)
        self.setStyleSheet(_qss())
        self._bg = views.BgRunner(self)
        self._bridge = _Bridge(self)
        self._bridge.partial.connect(self._on_partial)
        self._bridge.done.connect(self._on_done)
        self._folder = None
        self._data = {}
        self._busy = False
        self._final = True
        self._msgs = []                # the conversation (meeting_store.clean_chat rows)
        self._loaded = False
        self._load_token = 0
        self._req = 0                  # the question being answered; a stale request's results are dropped
        self._running = False
        self._partial = ""
        self._pending = None           # (req, text) waiting for the next draw
        self._drawn_at = 0.0
        self._live = None              # the answer row being written
        self._error_row = None         # an answer that failed (not part of the conversation)
        self._choice = meeting_chat.EngineChoice()
        self._gate = ("ready", "")
        self._stick = True
        self._rows = []

        head = QHBoxLayout()
        head.setSpacing(8)
        head.addWidget(label(TITLE, "h3", self))
        head.addStretch(1)
        self.clear_btn = Button("Clear chat", "link", "s", parent=self)
        self.clear_btn.setAccessibleName("Clear the chat")
        self.clear_btn.clicked.connect(self.clear)
        self.clear_btn.hide()
        head.addWidget(self.clear_btn)
        self.add(head)

        self.scroll = QScrollArea(self)
        self.scroll.setProperty("gusan", "scroll")
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll.viewport().setAutoFillBackground(False)
        self.scroll.setFocusPolicy(Qt.StrongFocus)
        self.scroll.setAccessibleName("Conversation")
        content = QWidget()
        content.setProperty("gusan", "scrollContent")
        self._content = content
        self._col = QVBoxLayout(content)
        self._col.setContentsMargins(0, 0, 4, 0)
        self._col.setSpacing(12)
        self._empty = self._build_empty(content)
        self._col.addWidget(self._empty)
        self._col.addStretch(1)
        self.scroll.setWidget(content)
        self.add(self.scroll, 1)
        bar = self.scroll.verticalScrollBar()
        bar.rangeChanged.connect(self._on_range)
        bar.valueChanged.connect(self._on_value)
        self._programmatic = False

        self.gate_bar = self._gate_box(self)          # the same words above the box once there is a chat
        self.add(self.gate_bar)
        self.counter = views.tlabel("", 11.5, 400, "faint", parent=self)
        self.counter.setAlignment(Qt.AlignRight)
        self.counter.hide()
        self.add(self.counter)

        row = QHBoxLayout()
        row.setSpacing(8)
        self.input = _Input(self)
        self.input.setProperty("chat", "input")
        self.input.setPlaceholderText(PLACEHOLDER)
        self.input.setFont(theme.font(13.5))
        self.input.setTabChangesFocus(True)
        self.input.setAccessibleName("Your question")
        self.input.setFixedHeight(INPUT_MIN_H)
        self.input.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.input.textChanged.connect(self._on_text)
        self.input.submit.connect(self.send)
        self.input.stop_asked.connect(self.stop)
        row.addWidget(self.input, 1)
        self.send_btn = Button("Send", "primary", "m", parent=self)
        self.send_btn.setAccessibleName("Send question")
        self.send_btn.clicked.connect(lambda: self.send())
        self.stop_btn = Button("Stop", "secondary", "m", icon="stop", parent=self)
        self.stop_btn.setAccessibleName("Stop the answer")
        self.stop_btn.clicked.connect(lambda: self.stop())
        self.stop_btn.hide()
        row.addWidget(self.send_btn, 0, Qt.AlignBottom)
        row.addWidget(self.stop_btn, 0, Qt.AlignBottom)
        self.add(row)

        foot = QHBoxLayout()
        foot.setSpacing(6)
        self._foot_icon = QLabel(self)
        self._foot_icon.setFixedSize(14, 14)
        self._foot_icon.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        foot.addWidget(self._foot_icon)
        self.foot = views.ElidedLabel("", self)
        self.foot.setFont(theme.font(11.5))
        views.set_tone(self.foot, "faint")
        foot.addWidget(self.foot, 1)
        self._foot_row = QWidget(self)
        self._foot_row.setLayout(foot)
        foot.setContentsMargins(0, 0, 0, 0)
        self.add(self._foot_row)

        self.setMinimumHeight(340)
        self.setTabOrder(self.clear_btn, self._chips[0])
        for a, b in zip(self._chips, self._chips[1:]):
            self.setTabOrder(a, b)
        self.setTabOrder(self._chips[-1], self.scroll)
        self.setTabOrder(self.scroll, self.input)
        self.setTabOrder(self.input, self.send_btn)
        self.setTabOrder(self.send_btn, self.stop_btn)
        self._sync()

    def sizeHint(self):                                           # noqa: N802 (Qt name)
        """The column's width does not depend on what the card holds (the footer's length, an answer): it asks for
        PREFERRED_W and gives way to the notes down to the column's own minimum."""
        sh = super().sizeHint()
        return QSize(PREFERRED_W, max(sh.height(), 340))

    # ── building ──
    def _gate_box(self, parent):
        box = QWidget(parent)
        lay = QVBoxLayout(box)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        box.text = views.tlabel("", 13, 400, "text2", parent=box, wrap=True)
        box.text.setTextFormat(Qt.PlainText)
        lay.addWidget(box.text)
        box.button = Button("Open settings", "secondary", "s", parent=box)     # (its words follow the gate)
        box.button.clicked.connect(self._gate_action)
        lay.addWidget(box.button, 0, Qt.AlignLeft)
        box.hide()
        return box

    def _build_empty(self, parent):
        box = QWidget(parent)
        lay = QVBoxLayout(box)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        self._intro = views.tlabel(meeting_chat.INTRO, 13, 400, "text2", parent=box, wrap=True)
        self._intro.setTextFormat(Qt.PlainText)
        lay.addWidget(self._intro)
        self._chips = []
        for text in meeting_chat.STARTERS:
            chip = Button(text, "secondary", "m", parent=box, full=True)
            chip.setProperty("chat", "chip")
            chip.setFixedHeight(34)
            chip.setStyleSheet("QPushButton { text-align: left; padding: 0px 14px; border-radius: 17px; }")
            chip.clicked.connect(lambda _=False, t=text: self.send(t))
            lay.addWidget(chip)
            self._chips.append(chip)
        self._gate_in_empty = self._gate_box(box)
        lay.addWidget(self._gate_in_empty)
        return box

    # ── the meeting this card is about ──
    def bind(self, folder, data, busy=False, final=True):
        """NotesView.render: this is the meeting on screen. ``data``: views.read_meeting's dict; ``busy``: the
        session is working on it; ``final``: its final transcript.txt is written (see
        MeetingSession.transcript_final). The same meeting only updates the gate - the chat, a question being
        written and what is typed stay."""
        folder = str(folder or "")
        if folder != (self._folder or ""):
            self._switch(folder)
        self._data = data or {}
        self._busy, self._final = bool(busy), bool(final)
        self._sync()

    def unbind(self):
        """Another meeting is loading: a running answer ends here ("stopped", saved in its own folder) and the card
        waits to be bound again."""
        self._cancel_running(save=True)
        self._folder = None
        self._msgs, self._loaded = [], False
        self._load_token += 1
        self._data = {}
        self._rebuild()
        self._sync()

    def release(self):
        """The meeting is about to be moved to the trash or deleted: the answer being written ends and nothing more is
        saved (the file may not be written into a folder that is going away)."""
        self._cancel_running(save=False)
        self._folder = None
        self._msgs, self._loaded = [], False
        self._load_token += 1
        self._rebuild()
        self._sync()

    def focus_input(self):
        self.input.setFocus()

    def _switch(self, folder):
        self._cancel_running(save=True)
        self._folder = folder or None
        self._msgs, self._loaded = [], False
        self._load_token += 1
        token = self._load_token
        self._rebuild()
        if not folder:
            return

        def work():
            return meeting_store.load_chat(folder)
        self._bg.run(work, lambda res, err, t=token: self._loaded_chat(t, res if err is None else []))

    def _loaded_chat(self, token, rows):
        if token != self._load_token or self._running:
            return
        self._msgs = list(rows or [])
        self._loaded = True
        self._rebuild()
        self._sync()

    # ── what the card can do now ──
    def _signed_in(self):
        auth = getattr(self.page.app, "auth", None)
        return bool(getattr(auth, "is_authenticated", False))

    def _engine(self):
        cfg = self.page.cfg()
        pro, signed_in = self.page.is_pro(), self._signed_in()
        return meeting_chat.pick_engine(cfg, pro, bool(pro and signed_in), signed_in=signed_in)

    def _compute_gate(self):
        """("ready" | "loading" | "no_transcript" | "finishing" | the engine's problem, the sentence)."""
        if not self._folder:
            return "loading", ""                           # no meeting bound yet: nothing to say
        d = self._data or {}
        text = (d.get("transcript") or "").strip()
        if self._busy and (not self._final or not d.get("transcript_file")):
            return "finishing", meeting_chat.FINISHING
        if not text:
            return "no_transcript", meeting_chat.NO_TRANSCRIPT
        self._choice = self._engine()
        if self._choice.problem:
            return self._choice.problem, meeting_chat.problem_text(self._choice.problem, self._choice)
        return "ready", ""

    def refresh_gate(self):
        """A setting that decides the engine changed (Privacy mode, the AI writer, the plan)."""
        self._sync()

    def _ready(self):
        return self._gate[0] == "ready" and self._loaded and bool(self._folder)

    def _sync(self):
        """Draw what depends on the gate and the conversation: the empty state, the note above the box, the
        buttons, the footer."""
        self._gate = self._compute_gate()
        mode, text = self._gate
        empty = not self._msgs and not self._running
        self._empty.setVisible(empty)
        ready = mode == "ready"
        self._intro.setVisible(ready)
        for chip in self._chips:
            chip.setVisible(ready)
        action = self._gate_button(mode)
        for box, shown in ((self._gate_in_empty, empty and not ready), (self.gate_bar, not empty and not ready)):
            box.text.setText(text)
            box.text.setVisible(bool(text))
            if action:
                box.button.setText(action[0])
                box.button.setAccessibleName(action[0])
            box.button.setVisible(bool(action))
            box.setVisible(shown)
        usable = self._ready()
        self.input.setEnabled(usable)
        self.input.running = self._running
        self.input.setPlaceholderText(PLACEHOLDER if usable else "")
        self.send_btn.setEnabled(usable and not self._running)
        self.send_btn.setVisible(not self._running)
        self.stop_btn.setVisible(self._running)
        self.clear_btn.setVisible(bool(self._msgs) and not self._running)
        self._sync_footer()

    def _gate_button(self, mode):
        """(button text, what it does) for a gate that has a way out, else None."""
        if mode in ("needs_pro", "privacy_no_model", "needs_download"):
            return ("Open settings", "settings")
        if mode == "signed_out":
            return ("Sign in", "sign_in")
        return None

    def _gate_action(self):
        what = (self._gate_button(self._gate[0]) or ("", ""))[1]
        if what == "settings":
            self.page.call("show_settings", "speech")
        elif what == "sign_in":
            self.page.call("show_auth_gate")

    def _sync_footer(self):
        c = self._choice
        shown = self._gate[0] == "ready" and bool(c.label)
        self._foot_row.setVisible(shown)
        if not shown:
            return
        icon = {"cloud": "cloud", "local": "laptop", "own_key": "key"}.get(c.kind, "cloud")
        self._foot_icon.setPixmap(views.glyph(icon, 14, "faint", 1.8))
        note = LOCAL_NOTE if c.kind == "local" and (self.page.cfg() or {}).get("privacy_mode") else FOOTER_NOTE
        self.foot.setText("%s · %s" % (c.label, note))

    # ── the box ──
    def _on_text(self):
        text = self.input.toPlainText()
        if len(text) > meeting_chat.MAX_QUESTION_CHARS:
            self.input.blockSignals(True)
            self.input.setPlainText(text[:meeting_chat.MAX_QUESTION_CHARS])
            cur = self.input.textCursor()
            cur.movePosition(cur.MoveOperation.End)
            self.input.setTextCursor(cur)
            self.input.blockSignals(False)
            text = self.input.toPlainText()
        n = len(text)
        self.counter.setText("%s / %s" % (format(n, ","), format(meeting_chat.MAX_QUESTION_CHARS, ",")))
        self.counter.setVisible(n >= COUNTER_FROM)
        self._fit_input()

    def _fit_input(self):
        doc = self.input.document()
        lines = max(1, int(doc.documentLayout().documentSize().height()))
        line = self.input.fontMetrics().lineSpacing()
        h = int(lines * line + 2 * doc.documentMargin() + 14)      # + the box's padding and border
        self.input.setFixedHeight(max(INPUT_MIN_H, min(INPUT_MAX_H, h)))
        self.input.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded if h > INPUT_MAX_H else Qt.ScrollBarAlwaysOff)

    # ── asking ──
    def send(self, question=None):
        """Ask ``question`` (else what is typed). Nothing happens while an answer is coming, with no transcript,
        or with no engine that can answer."""
        text = (question if isinstance(question, str) else self.input.toPlainText()).strip()
        if not text or self._running:
            return
        self._gate = self._compute_gate()                  # the plan or Privacy mode may have changed since
        if not self._ready():
            self._sync()
            return
        text = text[:meeting_chat.MAX_QUESTION_CHARS]
        self._clear_error_row()
        self._msgs.append({"role": "user", "text": text, "ts": int(time.time())})
        self._add_user_row(text)
        self.input.clear()
        self.page.track("meeting_chat_asked", {"engine": self._choice.kind if self._choice.kind in
                                               ("cloud", "own_key", "local") else "cloud"})
        self._save()
        self._answer(text)

    def retry(self):
        """Try again: the last question asks again (the failed attempt, or the missing answer, goes away)."""
        if self._running or not self._msgs or self._msgs[-1].get("role") != "user":
            return
        self._gate = self._compute_gate()
        if not self._ready():
            self._sync()
            return
        self._clear_error_row()
        self._answer(self._msgs[-1]["text"])

    def _answer(self, question):
        folder = self._folder
        self._req += 1
        req = self._req
        self._running, self._partial, self._pending = True, "", None
        self._stick = True
        row = self._new_answer_row()
        waiting = False
        if self._choice.kind == "local":
            try:
                import local_llm
                waiting = bool(local_llm._in_use.get(local_llm.normalize_model_id(self._choice.engine_id)))
            except Exception:
                waiting = False
        row.show_thinking(WAITING_LOCAL if waiting else THINKING)
        self._live = row
        self._sync()
        history = list(self._msgs[:-1])
        page = self.page
        cfg, pro, signed_in = dict(page.cfg()), page.is_pro(), self._signed_in()
        resolve = getattr(page.app, "_resolve_action_engine", None)
        args = (req, folder, question, history, cfg, pro, signed_in, resolve)
        if SYNC_ASK:
            self._work(*args)
        else:
            threading.Thread(target=self._work, args=args, daemon=True, name="gusan-chat").start()

    def _work(self, req, folder, question, history, cfg, pro, signed_in, resolve):
        """Worker thread: ask, and say how it ended."""
        last = [0.0]

        def emit(sig, *args):
            try:
                sig.emit(*args)
            except RuntimeError:
                pass                                       # the card is gone

        def on_text(text):
            now = time.monotonic()
            if now - last[0] >= PARTIAL_MS / 1000.0:
                last[0] = now
                emit(self._bridge.partial, req, text)

        def cancelled():
            return req != self._req

        if resolve is None:                                # an app without the Pro routing (a test double)
            def resolve(pref, _cfg=cfg):
                return actions.normalize_action_model(pref if pref is not None else _cfg.get("action_model")), _cfg
        try:
            ans = meeting_chat.ask(folder, question, history, cfg=cfg, pro=pro, resolve=resolve, on_text=on_text,
                                   cancelled=cancelled, signed_in=signed_in)
            result = ("ok", ans)
        except meeting_chat.Cancelled as c:
            result = ("stopped", c.text)
        except meeting_chat.ChatError as e:
            result = ("error", (e.code, e.message))
        except Exception as e:                             # noqa: BLE001 - said in words, never as it is
            logger.warning("Ask about this meeting failed", exc_info=True)
            result = ("error", meeting_chat.friendly_error(str(e), "cloud"))
        emit(self._bridge.done, req, result)

    def _on_partial(self, req, text):
        if req != self._req or not self._running:
            return
        self._partial = text
        self._pending = (req, text)
        wait = STREAM_RENDER_MS - (time.monotonic() - self._drawn_at) * 1000.0
        if wait <= 0:
            self._draw_stream()
            return
        if not getattr(self, "_draw_timer", None):
            self._draw_timer = QTimer(self)
            self._draw_timer.setSingleShot(True)
            self._draw_timer.timeout.connect(self._draw_stream)
        if not self._draw_timer.isActive():
            self._draw_timer.start(int(wait) + 1)

    def _draw_stream(self):
        pending, self._pending = self._pending, None
        if pending is None or pending[0] != self._req or not self._running or self._live is None:
            return
        self._drawn_at = time.monotonic()
        self._live.show_streaming(pending[1])

    def _on_done(self, req, result):
        if req != self._req or not self._running:
            return                                         # a stopped or older question
        kind, value = result
        self._running = False
        self._pending = None
        row, self._live = self._live, None
        if kind == "ok":
            ans = value
            msg = {"role": "assistant", "text": ans.text, "status": "done", "engine": ans.engine_label,
                   "kind": ans.kind, "coverage": ans.coverage.as_dict(), "seconds": ans.seconds,
                   "ts": int(time.time())}
            self._msgs.append(msg)
            if row is not None:
                row.show_message(msg)
            self._save()
            self._sync()
            self._scroll_to_start_of(row)
            _announce(row)
            return
        if kind == "stopped":
            self._keep_stopped(row, value)
        else:                                              # a calm sentence; the question stays, the failure is not kept
            code, sentence = value
            if row is not None:
                row.show_error(sentence, retry=code != "allowance")
                self._error_row = row
                _announce(row)
        self._sync()

    def stop(self):
        """Stop the answer that is coming: what has arrived stays, marked "Stopped" (and is never sent back to
        the model); with nothing yet the question stays unanswered."""
        if not self._running:
            return
        had_focus = self.stop_btn.hasFocus()
        self._req += 1                                     # the worker sees it at its next word and ends
        self._running = False
        row, self._live = self._live, None
        self._keep_stopped(row, self._partial)
        self._sync()
        if had_focus:
            self.input.setFocus()

    def _keep_stopped(self, row, text):
        text = (text or "").strip()
        if text:
            msg = {"role": "assistant", "text": text, "status": "stopped", "ts": int(time.time()),
                   "engine": self._choice.label, "kind": self._choice.kind}
            self._msgs.append(msg)
            if row is not None:
                row.show_message(msg)
            self._save()
        elif row is not None:
            row.show_unanswered()
            self._error_row = row

    def _cancel_running(self, save):
        """The meeting changes or goes away while an answer is coming: it ends as stopped (``save``: kept in its
        own folder's chat)."""
        if not self._running:
            return
        self._req += 1
        self._running = False
        self._pending = None
        row, self._live = self._live, None
        text = (self._partial or "").strip()
        if text and save:
            self._msgs.append({"role": "assistant", "text": text, "status": "stopped", "ts": int(time.time()),
                               "engine": self._choice.label, "kind": self._choice.kind})
            self._save()
        if row is not None:
            row.hide()
            row.deleteLater()

    def clear(self):
        """Clear chat: after one question, the conversation is emptied (the meeting stays)."""
        if not self._msgs or self._running:
            return
        private = bool(getattr(self.window(), "hidden_from_share", False))
        if not mini_prompt.confirm(self, CLEAR_TITLE, CLEAR_TEXT, "Clear", "Keep", private=private):
            return
        self._msgs = []
        self._rebuild()
        self._save()
        self._sync()

    # ── saving (the meeting's chat.json) ──
    def _save(self):
        folder = self._folder
        if not folder:
            return
        text = meeting_chat.dump_chat(self._msgs)
        path = meeting_store.chat_path(folder)
        version = views.next_version()

        def work():
            if not os.path.isdir(folder):
                return False                               # the meeting is gone: never make its folder again
            return views.write_latest(str(path), text, version)
        self._bg.run(work, None)

    # ── rows ──
    def _insert(self, widget):
        self._col.insertWidget(self._col.count() - 1, widget)       # before the closing stretch
        self._rows.append(widget)

    def _add_user_row(self, text):
        row = QWidget(self._content)
        row.setAccessibleName("Your question")
        lay = QHBoxLayout(row)
        lay.setContentsMargins(36, 0, 2, 0)
        lay.addStretch(1)
        bubble = QFrame(row)
        bubble.setProperty("chat", "bubble")
        bl = QVBoxLayout(bubble)
        bl.setContentsMargins(12, 8, 12, 8)
        shown = text if len(text) <= QUESTION_SHOWN else text[:QUESTION_SHOWN].rstrip() + "…"
        lb = views.tlabel(mini_prompt.breakable(shown), 13.5, 400, "ink", parent=bubble, wrap=True)
        lb.setTextFormat(Qt.PlainText)                      # a question is text, never markup
        lb.setTextInteractionFlags(Qt.TextSelectableByMouse)
        bl.addWidget(lb)
        lay.addWidget(bubble)
        row.setAccessibleDescription(text[:2000])
        self._insert(row)
        return row

    def _new_answer_row(self):
        row = _AnswerRow(self, self._content)
        self._insert(row)
        return row

    def _clear_error_row(self):
        row, self._error_row = self._error_row, None
        if row is not None:
            try:
                row.hide()
                row.deleteLater()
                self._rows = [r for r in self._rows if r is not row]
            except RuntimeError:
                pass

    def _rebuild(self):
        """Draw the whole conversation again (a meeting was opened, the chat was cleared)."""
        self._error_row = None
        for w in self._rows:
            try:
                w.hide()
                self._col.removeWidget(w)
                w.deleteLater()
            except RuntimeError:
                pass
        self._rows = []
        for i, msg in enumerate(self._msgs):
            if msg["role"] == "user":
                self._add_user_row(msg["text"])
                nxt = self._msgs[i + 1] if i + 1 < len(self._msgs) else None
                if nxt is None or nxt["role"] != "assistant":
                    if i == len(self._msgs) - 1 and not self._running:
                        row = self._new_answer_row()
                        row.show_unanswered()
                        self._error_row = row
            else:
                row = self._new_answer_row()
                row.show_message(msg)
        self._stick = True

    # ── scrolling ──
    def _on_value(self, value):
        if self._programmatic:
            return
        bar = self.scroll.verticalScrollBar()
        self._stick = bar.maximum() - value <= 4

    def _on_range(self, _lo, hi):
        if self._stick:
            self._programmatic = True
            self.scroll.verticalScrollBar().setValue(hi)
            self._programmatic = False

    def _scroll_to_start_of(self, row):
        """An answer ended: when it is taller than the view the reader starts at its first line."""
        if row is None:
            return

        def put():
            try:
                bar = self.scroll.verticalScrollBar()
                if row.height() > self.scroll.viewport().height():
                    self._stick = False
                    self._programmatic = True
                    bar.setValue(max(0, row.mapTo(self._content, QPoint(0, 0)).y() - 6))
                    self._programmatic = False
            except RuntimeError:
                pass
        QTimer.singleShot(0, put)
