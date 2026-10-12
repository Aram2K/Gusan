"""Rich clipboard (Gusan 2.0, D9): HTML + nice plain text in one copy.

    copy_rich(md) -> bool          GUI thread. Markdown with markup goes on the
                                   clipboard as HTML (rich_text.to_html) AND
                                   plain text (rich_text.to_plain); text
                                   without markup as plain text, unchanged.
    set_rich(plain, html) -> bool  GUI thread. Both flavours (html may be "").
    set_rich_blocking(plain, html, timeout=0.6) -> bool
                                   Any thread. On the GUI thread it is
                                   set_rich; from a worker it hops to the GUI
                                   thread and waits. False when not installed,
                                   on timeout (nothing is written later) or on
                                   error: the caller then falls back to plain
                                   pyperclip.copy.
    install() -> bool              Call once on the GUI thread at start (main
                                   does); creates the bridge set_rich_blocking
                                   needs. Safe to call again. set_rich and
                                   copy_rich install it too.

Qt writes Windows "HTML Format" (CF_HTML) + CF_UNICODETEXT, and macOS
public.html + plain text, from one QMimeData.
"""

import logging
import threading

from PySide6.QtCore import QCoreApplication, QObject, QStringListModel, QThread, Qt, Signal
from PySide6.QtGui import QGuiApplication

import rich_text

logger = logging.getLogger("transcribe")

_bridge = None
_install_lock = threading.Lock()


class _Bridge(QObject):
    """Lives on the GUI thread; runs callables emitted from any thread."""
    _call = Signal(object)

    def __init__(self):
        super().__init__()
        self._call.connect(self._run, Qt.QueuedConnection)

    def _run(self, fn):
        try:
            fn()
        except Exception:
            logger.warning("clipboard bridge call failed", exc_info=True)


def _on_gui_thread():
    app = QCoreApplication.instance()
    if app is None:
        return False
    try:
        return QThread.currentThread() == app.thread()
    except Exception:
        return threading.current_thread() is threading.main_thread()


def install():
    """Create the GUI-thread bridge (once). True when it is ready."""
    global _bridge
    if _bridge is not None:
        return True
    if not _on_gui_thread():
        return False
    with _install_lock:
        if _bridge is None:
            _bridge = _Bridge()
    return True


def installed():
    return _bridge is not None


def _document(html):
    """A whole HTML document with the fragment markers, so Qt's CF_HTML
    header points at our fragment and apps read it as UTF-8."""
    if "<!--StartFragment-->" in html:
        return html
    return ('<html><head><meta charset="utf-8"></head><body><!--StartFragment-->%s'
            '<!--EndFragment--></body></html>' % html)


def _new_mime():
    """An empty QMimeData made by Qt (C++), not by Python. The clipboard owns
    and deletes it, maybe only after Python has shut down; deleting a
    Python-made QMimeData then crashes the app at exit (seen offscreen). A
    model's mimeData() is a plain C++ QMimeData; its one format is removed."""
    try:
        model = QStringListModel([""])
        mime = model.mimeData([model.index(0, 0)])
        if mime is None:
            return None
        for fmt in list(mime.formats()):
            mime.removeFormat(fmt)
        return mime
    except Exception:
        logger.warning("No clipboard data object", exc_info=True)
        return None


def set_rich(plain, html):
    """Put plain text (+ HTML when given) on the clipboard. GUI thread only."""
    install()
    try:
        cb = QGuiApplication.clipboard()
        mime = _new_mime()
        if mime is None:
            cb.setText(plain or "")
            return True
        mime.setText(plain or "")
        if html:
            mime.setHtml(_document(html))
        cb.setMimeData(mime)
        return True
    except Exception:
        logger.warning("Rich copy failed", exc_info=True)
        return False


def copy_rich(md):
    """Copy Smart Action / notes Markdown the way people paste it."""
    md = md or ""
    if rich_text.has_markup(md):
        return set_rich(rich_text.to_plain(md), rich_text.to_html(md))
    install()
    try:
        QGuiApplication.clipboard().setText(md)
        return True
    except Exception:
        logger.warning("Copy failed", exc_info=True)
        return False


def set_rich_blocking(plain, html, timeout=0.6):
    """set_rich from any thread; waits up to ``timeout`` seconds."""
    if _on_gui_thread():
        return set_rich(plain, html)
    bridge = _bridge
    if bridge is None:
        return False
    done = threading.Event()
    lock = threading.Lock()
    state = {"started": False, "cancelled": False, "ok": False}

    def run():
        with lock:
            if state["cancelled"]:
                return
            state["started"] = True
        try:
            state["ok"] = bool(set_rich(plain, html))
        finally:
            done.set()

    try:
        bridge._call.emit(run)
    except Exception:
        logger.warning("Rich copy could not reach the GUI thread", exc_info=True)
        return False
    if done.wait(max(0.0, float(timeout))):
        return state["ok"]
    with lock:
        if not state["started"]:
            state["cancelled"] = True       # never write after the caller gave up
            return False
    # It is running right now: let it finish (setting the clipboard is quick).
    done.wait(max(0.2, float(timeout)))
    return state["ok"] if done.is_set() else False
