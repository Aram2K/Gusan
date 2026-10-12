"""ui/components/clipboard: rich copy (HTML + plain) and the worker-thread
path the paste uses. Offscreen Qt keeps its own clipboard, so nothing here
touches the real one."""
import threading
import time

import pytest


@pytest.fixture
def cb(qapp):
    from ui.components import clipboard
    assert clipboard.install()
    return clipboard


def _mime(qapp):
    from PySide6.QtGui import QGuiApplication
    return QGuiApplication.clipboard().mimeData()


def _spin(qapp, until, seconds=3.0):
    end = time.monotonic() + seconds
    while not until() and time.monotonic() < end:
        qapp.processEvents()
        time.sleep(0.005)


def test_exported_from_components(qapp):
    import ui.components as comps
    from ui.components import clipboard
    assert comps.clipboard is clipboard
    assert callable(clipboard.copy_rich) and callable(clipboard.set_rich_blocking)


def test_copy_rich_puts_html_and_plain(qapp, cb):
    assert cb.copy_rich("- [ ] Call David\n- [x] Book the room\n\n## Notes\n- **bold** point")
    mime = _mime(qapp)
    assert mime.hasHtml() and mime.hasText()
    assert mime.text() == "☐ Call David\n☑ Book the room\n\nNotes\n• bold point"
    html = mime.html()
    assert "☐&nbsp;Call David" in html and "<ul><li><b>bold</b> point</li></ul>" in html
    assert "<!--StartFragment-->" in html and "<!--EndFragment-->" in html
    assert 'charset="utf-8"' in html


def test_mime_is_made_by_qt_not_python(qapp, cb):
    """A Python-made QMimeData left on the clipboard crashes the app at exit."""
    import shiboken6
    assert cb.set_rich("p", "<p>h</p>")
    mime = _mime(qapp)
    assert not shiboken6.Shiboken.createdByPython(mime)
    assert sorted(mime.formats()) == ["text/html", "text/plain"]


def test_copy_rich_prose_is_plain_and_unchanged(qapp, cb):
    text = "Hi John,\n\nThe report is ready.\n\nBest,\nAram"
    assert cb.copy_rich(text)
    mime = _mime(qapp)
    assert mime.text() == text
    assert not mime.hasHtml()


def test_set_rich_on_the_gui_thread(qapp, cb):
    assert cb.set_rich("plain", "<p>x</p>")
    assert _mime(qapp).text() == "plain"
    assert "<p>x</p>" in _mime(qapp).html()
    assert cb.set_rich_blocking("again", "<p>y</p>")      # GUI thread: direct
    assert _mime(qapp).text() == "again"


def test_blocking_from_a_worker_thread(qapp, cb):
    result = {}

    def work():
        result["ok"] = cb.set_rich_blocking("from worker", "<p><b>w</b></p>", timeout=2.0)

    t = threading.Thread(target=work)
    t.start()
    _spin(qapp, lambda: not t.is_alive())
    t.join(1)
    assert result.get("ok") is True
    mime = _mime(qapp)
    assert mime.text() == "from worker"
    assert "<b>w</b>" in mime.html()


def test_timeout_returns_false_and_never_writes_late(qapp, cb):
    cb.set_rich("before", "")
    result = {}

    def work():
        result["ok"] = cb.set_rich_blocking("too late", "<p>late</p>", timeout=0.05)

    t = threading.Thread(target=work)
    t.start()
    t.join(2)                       # the GUI thread is not pumping events
    assert result.get("ok") is False
    for _ in range(20):
        qapp.processEvents()
    assert _mime(qapp).text() == "before"


def test_not_installed_from_a_worker(qapp, cb, monkeypatch):
    monkeypatch.setattr(cb, "_bridge", None)
    result = {}
    t = threading.Thread(target=lambda: result.setdefault("ok", cb.set_rich_blocking("x", "<p>x</p>")))
    t.start()
    t.join(2)
    assert result["ok"] is False
    # install() refuses off the GUI thread.
    t = threading.Thread(target=lambda: result.setdefault("install", cb.install()))
    t.start()
    t.join(2)
    assert result["install"] is False and not cb.installed()


def test_qt_html_renders_lists(qapp):
    import rich_text
    from PySide6.QtGui import QTextDocument
    doc = QTextDocument()
    doc.setHtml(rich_text.qt_html("- one\n- two\n\n- [ ] task"))
    block = doc.begin()
    assert block.textList() is not None and block.text() == "one"
    assert "☐" in doc.toPlainText() and "**" not in doc.toPlainText()
