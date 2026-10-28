"""The one question / message card of the app: a rounded white card, a title, a line or two, buttons.

Gusan never opens a QMessageBox or a QInputDialog (those are the OS's grey boxes; the stylesheet
gave their text one colour and the OS gave their panel another - dark navy words on a dark-grey
panel on a Windows in dark mode, see ui/theme.py). tests_qt/test_dialog_visibility.py renders every
variant below in a light and a dark OS and refuses the old classes. Use these:

    if mini_prompt.ask(parent, "Download Balanced · Whisper Small?",
                       "460 MB - Smart Actions will use it once it is ready.",
                       "Download", "Not now", icon="download"):
        ...
    if mini_prompt.confirm(parent, "Delete “Q4 roadmap”?", "Its notes, transcript and recording ...",
                           "Delete", "Cancel", detail="42 min", icon="trash"):    # red Delete, Enter = Cancel
        ...
    mini_prompt.notify(parent, "Couldn't save", "The file is open in another app.", level="error")
    key = mini_prompt.choose(parent, "Before you record", text, [("go", "I understand", "primary"),
                                                                  ("cancel", "Cancel", "secondary")])
    name = mini_prompt.ask_text(parent, "Rename meeting", "Name", text="Q4 roadmap")     # None = cancelled

``ask`` is the plain two-button question (Enter presses the first, Esc the second); ``confirm`` is the
same with a ``danger`` look (red icon and red button) and the SAFE button as the default, so a stray
Enter never deletes; ``notify`` is one button (Esc closes it too); ``choose`` takes any buttons
[(key, text, kind)] and returns the key (None for Esc / "cancel"); ``ask_text`` asks for one line.
Every text on the card is plain text (a calendar title is words, never markup). The card is opaque
white with a soft shadow (the dialog around it is see-through), centred over the Gusan window, in
front of it, and its words have names for screen readers. These functions are the one call a page
makes, so a test replaces one (monkeypatch ``mini_prompt.confirm``) instead of opening a window.

``confirm_download`` is the model download question (Smart Actions' picker); the Privacy mode
question lives in ui/privacy_prompt.py.

Three things a caller never has to think about: (1) a window can only be made on the window's own
thread, so a call from a worker thread is handed over to it (``on_gui_thread``) and waits for the
answer - it asks, it never crashes; (2) a name with no spaces (a URL in a meeting title, a long
file name) gets break points (``breakable``) instead of running out of the card; (3) when the Gusan
window is hidden or minimised there is nothing to sit over, so the card opens in the middle of the
screen the pointer is on.
"""

import logging
import re
import threading

from PySide6.QtCore import QCoreApplication, QObject, QPoint, QThread, Qt, Signal, Slot
from PySide6.QtGui import QCursor, QGuiApplication
from PySide6.QtWidgets import QDialog, QFrame, QHBoxLayout, QLabel, QLineEdit, QVBoxLayout

from ui import theme
from ui.components import icons
from ui.components.buttons import Button
from ui.components.surfaces import label

logger = logging.getLogger("transcribe")

_WIDTH = 392          # the card; the dialog adds the shadow's room around it
_SHADOW = 22
# tone -> (tile background, icon colour, default icon)
TONES = {
    "info": ("blue-sub", "blue-tx", "info"),
    "warn": ("amber-sub", "amber-tx", "alert"),
    "danger": ("red-sub", "red-tx", "alert"),
    "error": ("red-sub", "red-tx", "alert"),
}
_LEVELS = {"info": "info", "warning": "warn", "warn": "warn", "error": "error", "danger": "error"}

_LONG_WORD = re.compile(r"(\S{20})(?=\S)")
_BREAK = "​"                      # a zero-width space: a place where a line may end, drawn as nothing


def breakable(text):
    """``text`` with a break point inside every run of 20+ characters that has no space: a label wraps
    at spaces only, so a URL or a file name in a title used to run off the card's edge and be cut."""
    return _LONG_WORD.sub("\\1" + _BREAK, str(text or ""))


class _GuiThread(QObject):
    """Runs a call on the window's thread for a caller on another one (BlockingQueuedConnection)."""

    call = Signal(object)

    def __init__(self):
        super().__init__()
        app = QCoreApplication.instance()
        if app is not None and QThread.currentThread() is not app.thread():
            self.moveToThread(app.thread())         # (a thread may hand its own object over)
        self.call.connect(self._run, Qt.BlockingQueuedConnection)

    @Slot(object)
    def _run(self, box):
        try:
            box["result"] = box["fn"]()
        except BaseException as e:                   # noqa: BLE001 - re-raised in the caller's thread
            box["error"] = e


_gui = {"hop": None}
_gui_lock = threading.Lock()


def on_gui_thread(fn, *args, **kwargs):
    """``fn(*args, **kwargs)`` on the window's thread, and its result. On that thread already, a plain
    call. From a worker thread the call waits until the window's event loop has run it (so a question
    shown from there is asked, answered and returned - nothing is built off-thread)."""
    app = QCoreApplication.instance()
    if app is None or QThread.currentThread() is app.thread():
        return fn(*args, **kwargs)
    with _gui_lock:
        if _gui["hop"] is None:
            _gui["hop"] = _GuiThread()
    box = {"fn": lambda: fn(*args, **kwargs)}
    _gui["hop"].call.emit(box)
    if "error" in box:
        raise box["error"]
    return box.get("result")


def _on_gui(fn):
    """Decorator: the call is made on the window's thread whichever thread asks."""
    def wrapper(*args, **kwargs):
        return on_gui_thread(fn, *args, **kwargs)
    wrapper.__name__ = fn.__name__
    wrapper.__doc__ = fn.__doc__
    wrapper.__wrapped__ = fn
    return wrapper


class MiniPrompt(QDialog):
    """The card. ``yes_button`` is the default (Enter) and ``no_button`` is Esc, unless ``safe`` makes
    the no button the default. ``buttons`` [(key, text, kind)] replaces the two (``choice`` then holds
    the key pressed); ``field`` {"text", "placeholder", "max", "name"} adds a one-line field
    (``value()``); ``type_word`` keeps every red button off until that word is typed in the field;
    ``detail`` is more words (a string, or a list of lines) under the body."""

    def __init__(self, parent, title, body, yes_text, no_text, icon="info", *, tone="info", detail=None,
                 buttons=None, default=None, field=None, type_word=None, safe=False):
        super().__init__(parent.window() if parent is not None else None,
                         Qt.Dialog | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setModal(True)
        self.setStyleSheet(theme.stylesheet())
        self.setFont(theme.font(14))
        self.setWindowTitle(title)
        self.setAccessibleName(title)
        lines = [detail] if isinstance(detail, str) else [str(x) for x in (detail or []) if str(x).strip()]
        lines = [x for x in lines if x and x.strip()]
        self.setAccessibleDescription(" ".join([body or ""] + lines).strip())
        tone = tone if tone in TONES else "info"
        self.tone = tone
        self.choice = None
        self.field = None
        self._type_word = type_word or None

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

        tile_bg, tile_fg, tile_icon = TONES[tone]
        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.setSpacing(14)
        tile = QLabel(self.card)
        tile.setFixedSize(40, 40)
        tile.setAlignment(Qt.AlignCenter)
        tile.setPixmap(icons.pixmap(icon or tile_icon, 20, tile_fg, sw=2))
        tile.setStyleSheet("QLabel { background: %s; border: none; border-radius: 12px; }"
                           % theme.qss_color(tile_bg))
        tile.setAccessibleName("")
        top.addWidget(tile, 0, Qt.AlignTop)
        text = QVBoxLayout()
        text.setContentsMargins(0, 0, 0, 0)
        text.setSpacing(4)
        self.title_label = label(breakable(title), "h2", self.card, wrap=True)
        self.title_label.setTextFormat(Qt.PlainText)
        self.title_label.setFont(theme.font(15.5, 700))
        self.title_label.setAccessibleName(title)
        text.addWidget(self.title_label)
        self.body_label = label(breakable(body), "muted", self.card, wrap=True)
        self.body_label.setTextFormat(Qt.PlainText)
        self.body_label.setFont(theme.font(13, 400))
        self.body_label.setAccessibleName(body)
        text.addWidget(self.body_label)
        self.detail_labels = []
        for line in lines:
            lb = label(breakable(line), "muted", self.card, wrap=True)
            lb.setTextFormat(Qt.PlainText)
            lb.setFont(theme.font(13, 400))
            lb.setAccessibleName(line)
            text.addWidget(lb)
            self.detail_labels.append(lb)
        if field is not None:
            text.addSpacing(6)
            self.field = QLineEdit(self.card)
            self.field.setFont(theme.font(14))
            self.field.setFixedHeight(36)
            self.field.setText(str(field.get("text") or ""))
            self.field.setPlaceholderText(str(field.get("placeholder") or ""))
            self.field.setMaxLength(int(field.get("max") or 200))
            self.field.setAccessibleName(str(field.get("name") or title))
            text.addWidget(self.field)
        elif self._type_word:
            text.addSpacing(6)
            hint = label("Type %s to confirm." % self._type_word, "hint", self.card)
            hint.setTextFormat(Qt.PlainText)
            text.addWidget(hint)
            self.field = QLineEdit(self.card)
            self.field.setFont(theme.font(14))
            self.field.setFixedHeight(36)
            self.field.setPlaceholderText(self._type_word)
            self.field.setAccessibleName("Type %s to confirm" % self._type_word)
            text.addWidget(self.field)
        top.addLayout(text, 1)
        lay.addLayout(top)

        specs = list(buttons) if buttons else []
        if not specs:
            if no_text:
                specs.append(("no", no_text, "secondary"))
            specs.append(("yes", yes_text, "danger" if tone == "danger" else "primary"))
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        row.addStretch(1)
        self.buttons = {}
        for key, btext, kind in specs:
            b = Button(btext, kind, "m", parent=self.card)
            b.setAutoDefault(True)
            self.buttons[key] = b
            row.addWidget(b)
            b.clicked.connect(lambda _=False, k=key: self._pick(k))
        lay.addLayout(row)
        keys = [s[0] for s in specs]
        if buttons:
            first = default if default in self.buttons else (
                next((k for k in keys if k != "cancel"), keys[0]))
            self._default = first
            self.yes_button = self.buttons[first]
            self.no_button = self.buttons.get("cancel") or self.buttons[keys[-1]]
        else:
            self.yes_button = self.buttons["yes"]
            self.no_button = self.buttons.get("no", self.yes_button)
            self._default = "no" if (safe and "no" in self.buttons) else "yes"
        for k, b in self.buttons.items():
            b.setDefault(k == self._default)
        for a, b in zip(keys, keys[1:]):
            self.setTabOrder(self.buttons[a], self.buttons[b])
        if self.field is not None:
            self.setTabOrder(self.field, self.buttons[keys[0]])
            self.field.returnPressed.connect(self._enter_in_field)
            self.field.textChanged.connect(self._sync)
        self._sync()
        self.adjustSize()

    # ── answers ──
    def _pick(self, key):
        if key in ("no", "cancel"):
            self.choice = None
            self.reject()
            return
        self.choice = key
        self.accept()

    def _enter_in_field(self):
        b = self.buttons.get(self._default)
        if b is not None and b.isEnabled():
            b.click()

    def _sync(self):
        """Red buttons wait for the typed word (when one is asked for)."""
        if not self._type_word:
            return
        ok = (self.field.text() if self.field else "").strip() == self._type_word
        for b in self.buttons.values():
            if b.kind() == "danger":
                b.setEnabled(ok)

    @property
    def input(self):                      # the settings page's old AskDialog name for the typed-word field
        return self.field

    def value(self):
        """The field's text, trimmed ("" without a field)."""
        return self.field.text().strip() if self.field is not None else ""

    # ── on screen ──
    def showEvent(self, event):
        super().showEvent(event)
        self.adjustSize()                 # wrapped words know their height once the card is polished
        self._centre()
        try:
            self.raise_()
            self.activateWindow()
        except Exception:
            logger.debug("MiniPrompt could not take the front", exc_info=True)
        if self.field is not None and not self._type_word and self.field.isVisible():
            self.field.setFocus(Qt.OtherFocusReason)
            self.field.selectAll()
        elif self._type_word and self.field is not None:
            self.field.setFocus(Qt.OtherFocusReason)
        else:
            self.buttons[self._default].setFocus(Qt.OtherFocusReason)

    def _centre(self):
        """Over the Gusan window - or, with no window on screen (hidden to the tray, minimised: its
        position then means nothing), in the middle of the screen the pointer is on - and inside the
        screen."""
        try:
            host = self.parentWidget()
            if host is not None and host.isVisible() and not host.window().isMinimized():
                c = host.mapToGlobal(host.rect().center())
            else:
                scr = (QGuiApplication.screenAt(QCursor.pos()) or self.screen()
                       or QGuiApplication.primaryScreen())
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


def _run(dlg, private=False):
    """Exec the card (hidden from screen shares when ``private``) and let it go."""
    if private:
        try:
            from ui import glass
            dlg.winId()                          # the native window, before it is shown
            glass.exclude_from_capture(dlg, True)
        except Exception:
            logger.debug("MiniPrompt could not be hidden from capture", exc_info=True)
    try:
        return dlg.exec()
    finally:
        dlg.deleteLater()


@_on_gui
def ask(parent, title, body, yes_text, no_text, icon="info", private=False):
    """Show the card; True when the first button (or Enter) was pressed. Tests replace this.

    ``private``: asked from a window that is hidden from screen shares (the Live Assistant
    card), so the question itself is kept out of a share and a recording too, where the OS
    can do that."""
    dlg = MiniPrompt(parent, title, body, yes_text, no_text, icon=icon)
    return _run(dlg, private) == QDialog.Accepted


@_on_gui
def confirm(parent, title, text, confirm_label="OK", cancel_label="Cancel", danger=True, *, detail=None,
            icon=None, private=False, safe=None):
    """Ask before something that cannot be taken back (``danger``: red icon, red button, and the
    Cancel button is the default so Enter never confirms) or a plain yes / no. True = confirmed."""
    dlg = MiniPrompt(parent, title, text, confirm_label, cancel_label, icon=icon,
                     tone="danger" if danger else "info", detail=detail,
                     safe=bool(danger) if safe is None else bool(safe))
    return _run(dlg, private) == QDialog.Accepted


@_on_gui
def notify(parent, title, text, level="info", button="OK", *, detail=None, icon=None, private=False):
    """One message and one button. level: "info" | "warning" | "error". Returns None."""
    tone = _LEVELS.get(level, "info")
    dlg = MiniPrompt(parent, title, text, button, None, icon=icon, tone=tone, detail=detail)
    _run(dlg, private)


@_on_gui
def choose(parent, title, text, buttons, *, tone="info", detail=None, default=None, icon=None, private=False,
           type_word=None):
    """Any buttons [(key, text, kind)], kind as ui.components.Button's. Returns the key pressed, or
    None for Esc / a "cancel" key. ``default``: the key Enter presses (else the first that isn't
    "cancel"); ``type_word``: red buttons stay off until the word is typed."""
    dlg = MiniPrompt(parent, title, text, None, None, icon=icon, tone=tone, detail=detail,
                     buttons=buttons, default=default, type_word=type_word)
    if _run(dlg, private) == QDialog.Accepted:
        return dlg.choice
    return None


@_on_gui
def ask_text(parent, title, label_text, text="", ok_label="Save", cancel_label="Cancel", *, placeholder="",
             max_length=200, icon="pen", private=False):
    """Ask for one line. The trimmed text, or None when cancelled."""
    dlg = MiniPrompt(parent, title, label_text, ok_label, cancel_label, icon=icon,
                     field={"text": text, "placeholder": placeholder, "max": max_length, "name": label_text})
    if _run(dlg, private) == QDialog.Accepted:
        return dlg.value()
    return None


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


__all__ = ["MiniPrompt", "ask", "ask_text", "breakable", "choose", "confirm", "confirm_download", "download_body",
           "download_title", "notify", "on_gui_thread"]
