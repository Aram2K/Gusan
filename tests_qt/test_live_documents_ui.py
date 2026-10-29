"""Live Assistant documents, the Qt side.

The Documents section of the Live Assistant page (ui/pages/live_documents.py): the picker, drag
and drop, one row per document with its "Use in answers" switch and remove, Clear all, calm notes
for files that can't be read, and reading with Cancel - all of it off the GUI thread, tested with
REAL worker threads. And the floating card (ui/live_assist.py): the chip above the ask box and its
list, files dropped on the card, answers that use the active documents (manual questions and
automatic ones), the one-time cloud question, Privacy mode, pictures.

The documents are built in memory (tests/live_docs_fixtures.py); the store lives in a temp folder
(live_docs.get_store is pointed at it), nothing is sent anywhere.
"""
import threading
import time
from types import SimpleNamespace
from unittest import mock

import pytest
from PySide6.QtCore import QMimeData, QObject, QPoint, QPointF, Qt, QUrl, Signal
from PySide6.QtGui import QDragEnterEvent, QDropEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QLabel, QMenu, QWidget

from tests import live_docs_fixtures as fx
from tests_qt.conftest import make_stub_app

LIVE_CFG = {"live_assist_private": True, "live_assist_auto_answer": True,
            "live_assist_screen_auto": True, "live_assist_language": "",
            "live_assist_output_language": "", "live_assist_theme": "light",
            "live_assist_opacity": 0.96, "live_assist_context": ""}


def _wait(cond, timeout=8.0):
    end = time.monotonic() + timeout
    while not cond() and time.monotonic() < end:
        QApplication.processEvents()
        time.sleep(0.005)
    QApplication.processEvents()
    return cond()


# ── shared fixtures ──
@pytest.fixture
def store(tmp_path, monkeypatch):
    """A DocStore in a temp folder, and live_docs.get_store() pointing at it."""
    import live_docs
    st = live_docs.DocStore(tmp_path / "live_docs")
    monkeypatch.setattr(live_docs, "get_store", lambda: st)
    return st


@pytest.fixture
def files(tmp_path):
    """write(name, bytes) -> a path in a temp folder."""
    root = tmp_path / "incoming"
    root.mkdir()

    def write(name, data):
        path = root / name
        path.write_bytes(data)
        return str(path)
    return write


def _pdf(files, name="Q3 plan.pdf", pages=3, text="Budget and hiring plan."):
    return files(name, fx.make_pdf([text] * pages))


def _docx(files, name="Supplier contract.docx", paragraphs=("The supplier delivers within 30 days.",
                                                              "Payment is due in 45 days.")):
    return files(name, fx.make_docx(list(paragraphs)))


def _png(files, name="whiteboard.png"):
    return files(name, fx.make_png((900, 600), color=(40, 120, 200)))


def _add(store, path):
    result = store.add_file(path)
    assert result.ok, result.message
    return result.info


def _mime(*paths, urls=()):
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(p)) for p in paths] + [QUrl(u) for u in urls])
    return mime


# ═══════════════════════════ the page ═══════════════════════════
@pytest.fixture
def build(qapp, store, monkeypatch):
    """build(tier="pro", **cfg) -> (page, app). set_setting writes cfg and tells the page, as the
    controller and the shell do."""
    from ui import theme
    from ui.pages import live_assistant
    monkeypatch.setattr(live_assistant, "load_sessions", lambda: [])
    made = []

    def _build(tier="pro", **cfg):
        app = make_stub_app(tier)
        app.cfg.update(LIVE_CFG)
        app.cfg.update(cfg)
        app.live_assist = None
        holder = {}

        def set_setting(key, value):
            app.calls.append(("set_setting", key, value))
            app.cfg[key] = value
            page = holder.get("page")
            if page is not None:
                page.on_setting_changed(key)
            return True
        app.set_setting = set_setting
        app.show_main = lambda *a, **k: app.calls.append(("show_main",) + a)
        host = QWidget()
        theme.apply(host)
        host.resize(1100, 1500)
        page = live_assistant.create(app)
        page.setParent(host)
        page.resize(1100, 1500)
        holder["page"] = page
        host.show()
        page.on_open()
        QApplication.processEvents()
        assert _wait(lambda: page.documents._loaded)
        made.append(host)
        return page, app

    yield _build
    for h in made:
        try:
            h.hide()
            h.deleteLater()
        except RuntimeError:                      # a test closed it already
            pass
    QApplication.processEvents()


def _idle(section):
    assert _wait(lambda: not section.is_reading()), "still reading"


def _tracked(app, event=None):
    return [c for c in app.calls if c[0] == "track" and (event is None or c[1] == event)]


def test_the_section_sits_between_about_my_calls_and_past_sessions(build):
    page, _ = build()
    from PySide6.QtCore import QPoint as P
    about = page.context_edit.mapTo(page, P(0, 0)).y()
    docs = page.documents.mapTo(page, P(0, 0)).y()
    past = page.sessions_card.mapTo(page, P(0, 0)).y()
    assert about < docs < past
    assert page.documents.accessibleName() == "Documents"


def test_empty_state_is_one_plain_line(build):
    import live_docs
    page, _ = build()
    sec = page.documents
    assert sec.empty_box.isVisibleTo(sec)
    assert sec.empty_box.line.text() == live_docs.EMPTY_LINE == "Add a document and the assistant can use it in its answers."
    assert "drop them here" in sec.empty_box.toolTip()                # the hint is a tooltip, not more text
    assert not sec.list_box.isVisibleTo(sec) and not sec.clear_button.isVisibleTo(sec)
    assert not sec.foot.isVisibleTo(sec)                     # nothing to say about where words go
    assert sec.add_button.text() == "Add documents" and sec.add_button.accessibleName() == "Add documents"


def test_adding_files_shows_one_row_each_with_the_switch_on(build, store, files):
    page, app = build()
    sec = page.documents
    paths = [_pdf(files), _docx(files), _png(files)]
    assert sec.add_paths(paths) == 3
    _idle(sec)
    names = {r.info.name: r for r in sec._rows.values()}
    assert set(names) == {"Q3 plan.pdf", "Supplier contract.docx", "whiteboard.png"}
    for row in names.values():
        assert row.toggle.isChecked() and row.info.active          # new documents start ON
    assert names["Q3 plan.pdf"].meta_label.full_text().startswith("3 pages")
    assert names["whiteboard.png"].meta_label.full_text().startswith("Picture")
    assert [t.kind() for t in (names["Q3 plan.pdf"].tile, names["Supplier contract.docx"].tile,
                               names["whiteboard.png"].tile)] == ["pdf", "doc", "image"]
    assert store.count() == 3 and store.active_count() == 3
    assert not sec.empty_box.isVisibleTo(sec) and sec.clear_button.isVisibleTo(sec)
    assert not sec.strip.isVisibleTo(sec)                           # the reading strip is gone
    # newest first
    assert [d.name for d in sec.documents()][0] == "whiteboard.png"


def test_reading_happens_on_a_worker_thread_never_the_gui_thread(build, files, monkeypatch):
    import doc_reader
    page, _ = build()
    seen = []
    real = doc_reader.read_document

    def spy(path, cancel=None, *a, **k):
        seen.append(threading.current_thread())
        return real(path, cancel, *a, **k)
    monkeypatch.setattr(doc_reader, "read_document", spy)
    page.documents.add_paths([_docx(files), _pdf(files)])
    _idle(page.documents)
    assert len(seen) == 2
    assert all(t is not threading.main_thread() for t in seen)


def test_the_window_stays_alive_while_a_file_is_read_and_cancel_stops_it(build, files, monkeypatch):
    import doc_reader
    page, app = build()
    sec = page.documents
    started, release = threading.Event(), threading.Event()
    real = doc_reader.read_document

    def slow(path, cancel=None, *a, **k):
        started.set()
        while not (cancel and cancel()) and not release.is_set():
            time.sleep(0.005)
        if cancel and cancel():
            return doc_reader.failure("cancelled")
        return real(path, cancel, *a, **k)
    monkeypatch.setattr(doc_reader, "read_document", slow)
    ticks = []
    from PySide6.QtCore import QTimer
    timer = QTimer()
    timer.timeout.connect(lambda: ticks.append(1))
    timer.start(10)
    sec.add_paths([_pdf(files, "Annual report.pdf"), _docx(files)])
    assert _wait(lambda: sec.strip.isVisibleTo(sec))
    assert started.is_set() and sec.is_reading()
    assert sec.strip.text_label.full_text() == "Reading Annual report.pdf"
    assert sec.strip.count_label.text() == "1 of 2"
    assert sec.strip.cancel_button.accessibleName() == "Stop reading"
    before = len(ticks)
    _wait(lambda: len(ticks) > before + 5, 2.0)
    assert len(ticks) > before + 5                                   # the GUI thread kept running
    sec.strip.cancel_button.click()
    assert sec.strip.text_label.full_text() == "Stopping…" and not sec.strip.cancel_button.isEnabled()
    _idle(sec)
    timer.stop()
    assert not sec._rows and not sec._notes                          # nothing added, no scary message
    assert not _tracked(app, "live_document_failed")                  # a cancel is not a failure
    assert not sec.strip.isVisibleTo(sec)


def test_a_quick_read_never_flashes_the_reading_strip(build, files, monkeypatch):
    # The strip waits STRIP_DELAY_MS before it shows. A slow machine (a CI runner) can take longer than
    # that to read even a tiny file, and then the strip is right to show: give this test a long wait so
    # it checks what it is about - nothing is shown at once and the timer stops when the read ends.
    from ui.pages import live_documents
    monkeypatch.setattr(live_documents, "STRIP_DELAY_MS", 60_000)
    page, _ = build()
    sec = page.documents
    shown = []
    real = sec.strip.show_reading
    monkeypatch.setattr(sec.strip, "show_reading", lambda *a: shown.append(a) or real(*a))
    sec.add_paths([_docx(files)])
    _idle(sec)
    time.sleep(0.05)
    QApplication.processEvents()
    assert shown == [] and not sec.strip.isVisibleTo(sec) and not sec._strip_timer.isActive()
    assert len(sec._rows) == 1


def test_files_wait_their_turn_when_more_are_added_while_reading(build, store, files, monkeypatch):
    import doc_reader
    page, _ = build()
    sec = page.documents
    release = threading.Event()
    real = doc_reader.read_document
    order = []

    def slow(path, cancel=None, *a, **k):
        order.append(path)
        release.wait(5)
        return real(path, cancel, *a, **k)
    monkeypatch.setattr(doc_reader, "read_document", slow)
    first, second = _docx(files, "one.docx"), _docx(files, "two.docx", ("Another file entirely.",))
    sec.add_paths([first])
    assert _wait(lambda: len(order) == 1)
    sec.add_paths([second])                                          # arrives while the first is read
    assert len(order) == 1
    release.set()
    assert _wait(lambda: len(order) == 2 and not sec.is_reading())
    assert order == [first, second] and store.count() == 2


def test_a_file_that_cannot_be_read_becomes_a_calm_note_not_an_error(build, store, files):
    page, app = build()
    sec = page.documents
    sec.add_paths([files("old-accounts.xyz", b"\x00\x01\x02garbage\xff" * 80),
                   files("scan.pdf", fx.make_pdf([""] * 3)),
                   files("hollow.docx", b"PK\x03\x04 this is not a zip at all")])
    _idle(sec)
    assert store.count() == 0 and not sec._rows
    notes = {n.name: n for n in sec._notes}
    assert set(notes) == {"old-accounts.xyz", "scan.pdf", "hollow.docx"}
    assert notes["old-accounts.xyz"].message == "Gusan can't read this kind of file yet."
    assert "can't read the words" in notes["scan.pdf"].message and "3 pages" in notes["scan.pdf"].message
    assert notes["hollow.docx"].message
    for n in notes.values():
        assert "limit" not in n.message.lower() and "exception" not in n.message.lower()
        assert n.accessibleName() == "%s: %s" % (n.name, n.message)
    # the x dismisses a note
    note = notes["old-accounts.xyz"]
    note.dismiss_button.click()
    assert _wait(lambda: note not in sec._notes)
    assert len(sec._notes) == 2
    # failure counts: a reason and a type class, never a name
    failed = _tracked(app, "live_document_failed")
    assert len(failed) == 3
    assert all(set(c[2]) == {"reason", "type"} for c in failed)
    assert "old-accounts" not in str(failed) and "scan" not in str(failed).replace("scanned", "")


def test_a_big_drop_adds_every_row_and_keeps_the_notes_in_check(build, store, files):
    from ui.pages.live_documents import DocRow, MAX_NOTES, NoteRow
    page, _ = build()
    sec = page.documents
    good = [files("n%02d.txt" % i, ("note number %d about topic %d" % (i, i)).encode()) for i in range(15)]
    junk = [files("junk%02d.xyz" % i, b"\x00\x01\x02garbage\xff" * 80 + bytes([i])) for i in range(20)]
    sec.add_paths(good + junk)
    _idle(sec)
    assert len(sec._rows) == 15 and store.count() == 15
    assert len(sec._notes) == MAX_NOTES                              # a folder of junk never floods the card
    lay = sec.list_lay
    kinds = [type(lay.itemAt(i).widget()) for i in range(lay.count())]
    assert kinds == [NoteRow] * MAX_NOTES + [DocRow] * 15           # notes first, then the documents
    assert [lay.itemAt(MAX_NOTES + i).widget().info.id for i in range(15)] == [d.id for d in sec.documents()]


def test_the_same_file_twice_says_so_once(build, files):
    page, _ = build()
    sec = page.documents
    path = _docx(files)
    sec.add_paths([path])
    _idle(sec)
    sec.add_paths([path])
    _idle(sec)
    assert len(sec._rows) == 1
    assert [n.message for n in sec._notes] == ["That one is already added."]
    sec.add_paths([path])
    _idle(sec)
    assert len(sec._notes) == 1                                     # not stacked up


def test_adding_counts_only_a_type_a_format_and_a_size(build, files):
    page, app = build()
    page.documents.add_paths([_pdf(files, "Secret strategy.pdf")])
    _idle(page.documents)
    added = _tracked(app, "live_document_added")
    assert len(added) == 1
    props = added[0][2]
    assert set(props) == {"type", "format", "size", "truncated"}
    assert props["type"] == "pdf" and props["format"] == "pdf"
    assert "Secret" not in str(app.calls) and "strategy" not in str(app.calls).lower()


def test_the_switch_chooses_what_the_assistant_may_use(build, store, files):
    from ui import live_docs_ui
    page, _ = build()
    sec = page.documents
    sec.add_paths([_pdf(files), _docx(files)])
    _idle(sec)
    heard = []
    live_docs_ui.bridge().changed.connect(lambda: heard.append(1))
    row = next(r for r in sec._rows.values() if r.info.name == "Supplier contract.docx")
    assert row.toggle.accessibleName() == "Use Supplier contract.docx in answers"
    row.toggle.click()
    assert not row.toggle.isChecked() and not store.get(row.info.id).active
    assert store.active_count() == 1
    assert row.name_label.property("off") is True                    # it looks switched off
    assert "not used in answers" in row.accessibleName()
    assert heard                                                      # the card's chip hears of it
    row.toggle.click()
    assert store.get(row.info.id).active and "used in answers" in row.accessibleName()
    assert "not used" not in row.accessibleName()
    assert row.name_label.property("off") is False


def test_remove_deletes_the_stored_data(build, store, files):
    page, _ = build()
    sec = page.documents
    sec.add_paths([_docx(files), _png(files)])
    _idle(sec)
    folder = store.directory()
    before = {p.name for p in folder.iterdir()}
    assert len([n for n in before if n.endswith((".json", ".jpg")) and n != "index.json"]) == 2
    row = next(r for r in sec._rows.values() if r.info.name == "whiteboard.png")
    row.remove_button.click()
    assert _wait(lambda: len(sec._rows) == 1)
    after = {p.name for p in folder.iterdir()}
    assert not any(n.endswith(".jpg") for n in after)               # the picture's pixels are gone too
    assert store.count() == 1
    assert row.remove_button.accessibleName() == "Remove whiteboard.png"


def test_delete_removes_the_focused_row_and_space_flips_the_switch(build, store, files):
    page, _ = build()
    sec = page.documents
    sec.add_paths([_pdf(files, "a.pdf"), _docx(files, "b.docx", ("bee",)), _docx(files, "c.docx", ("sea",))])
    _idle(sec)
    page.window().activateWindow()
    QApplication.processEvents()
    assert len(sec.documents()) == 3                                   # newest first
    first = sec._rows[sec.documents()[0].id]
    first.toggle.setFocus(Qt.TabFocusReason)
    QTest.keyClick(first.toggle, Qt.Key_Space)                       # Space flips the switch it is on
    assert not first.toggle.isChecked() and store.active_count() == 2
    QTest.keyClick(first.toggle, Qt.Key_Space)
    assert first.toggle.isChecked() and store.active_count() == 3
    QTest.keyClick(first.toggle, Qt.Key_Delete)                      # Delete removes the row the focus is on
    assert _wait(lambda: len(sec._rows) == 2)
    assert first.info.id not in {d.id for d in sec.documents()} and store.count() == 2
    # ...and the keyboard focus moved on to a remaining row, not nowhere
    focused = QApplication.focusWidget()
    assert any(focused in (r.toggle, r.remove_button) for r in sec._rows.values())
    # Delete on the x of a row works too (it reaches the row)
    row = sec._rows[sec.documents()[0].id]
    row.remove_button.setFocus(Qt.TabFocusReason)
    QTest.keyClick(row.remove_button, Qt.Key_Delete)
    assert _wait(lambda: len(sec._rows) == 1) and store.count() == 1


def test_every_control_in_the_section_has_a_name_and_a_tab_stop(build, files):
    from PySide6.QtWidgets import QAbstractButton
    page, _ = build()
    sec = page.documents
    sec.add_paths([_pdf(files), files("junk.xyz", b"\x00\x01\x02garbage\xff" * 80)])
    _idle(sec)
    seen = 0
    for w in sec.findChildren(QAbstractButton):
        name = w.accessibleName() or w.text()
        assert name.strip(), "%s has no name" % type(w).__name__
        if w.isVisibleTo(sec):
            assert int(w.focusPolicy().value) & int(Qt.TabFocus.value), "%r can't be reached with Tab" % name
        seen += 1
    assert seen >= 6
    row = next(iter(sec._rows.values()))
    assert row.accessibleName().startswith("Q3 plan.pdf, 3 pages") and row.accessibleName().endswith("used in answers")


def test_clear_all_asks_first_and_forgets_everything(build, store, files, monkeypatch):
    from ui.components import mini_prompt
    page, _ = build()
    sec = page.documents
    sec.add_paths([_docx(files, "x.docx", ("ex",)), _docx(files, "y.docx", ("why",)), _png(files)])
    _idle(sec)
    asked = []
    monkeypatch.setattr(mini_prompt, "ask", lambda parent, title, body, yes, no, icon="info", **kw:
                        asked.append((title, body, yes, no)) or False)
    sec.clear_button.click()
    assert store.count() == 3 and len(asked) == 1                    # "Keep them": nothing happens
    assert asked[0][0] == "Remove all documents?" and asked[0][2:] == ("Remove all", "Keep them")
    assert "limit" not in " ".join(asked[0]).lower()
    monkeypatch.setattr(mini_prompt, "ask", lambda *a, **k: True)
    sec.clear_button.click()
    assert store.count() == 0 and not sec._rows
    assert list(store.directory().glob("d*")) == []                  # no stored text or picture left
    assert sec.empty_box.isVisibleTo(sec) and not sec.clear_button.isVisibleTo(sec)


def test_clear_all_shows_only_with_two_or_more(build, files):
    page, _ = build()
    sec = page.documents
    sec.add_paths([_docx(files)])
    _idle(sec)
    assert not sec.clear_button.isVisibleTo(sec)
    sec.add_paths([_pdf(files)])
    _idle(sec)
    assert sec.clear_button.isVisibleTo(sec)


def test_the_header_keeps_its_button_whole_in_the_narrowest_window(build, files):
    """The smallest window leaves this card about 320 px: "Documents" and "Add documents" must
    both fit, so "Clear all" lives in the column head under them, not beside the button."""
    from PySide6.QtCore import QPoint as P
    page, _ = build()
    page.window().resize(728, 1500)
    page.resize(728, 1500)
    QApplication.processEvents()
    sec = page.documents
    sec.add_paths([_pdf(files), _docx(files)])
    _idle(sec)
    QApplication.processEvents()
    assert sec.width() < 360                                         # really the narrow case
    assert sec.clear_button.isVisibleTo(sec)
    add = sec.add_button
    assert add.width() >= add.sizeHint().width()                     # not squeezed: its words are whole
    assert add.mapTo(sec, P(add.width(), 0)).x() <= sec.width()      # and inside the card
    assert sec.title_label.geometry().right() < add.geometry().left()
    assert sec.clear_button.parentWidget() is sec.col_head           # left of "Use in answers", under the title
    assert sec.clear_button.mapTo(sec, P(0, 0)).y() > add.mapTo(sec, P(0, add.height())).y() - 1


def test_the_tab_order_follows_what_is_on_screen(build, files):
    page, _ = build()
    sec = page.documents
    sec.add_paths([_pdf(files, "a.pdf"), files("junk.xyz", b"\x00\x01\x02garbage\xff" * 80)])
    _idle(sec)
    page.window().activateWindow()
    sec.add_button.setFocus(Qt.TabFocusReason)
    QApplication.processEvents()
    seen = []
    for _ in range(5):
        w = QApplication.focusWidget()
        seen.append(w.accessibleName() if w is not None else "")
        page.window().focusNextChild()
        QApplication.processEvents()
    assert seen[:4] == ["Add documents", "Dismiss: junk.xyz", "Use a.pdf in answers", "Remove a.pdf"]
    sec.add_paths([_docx(files)])
    _idle(sec)
    sec.add_button.setFocus(Qt.TabFocusReason)
    page.window().focusNextChild()
    QApplication.processEvents()
    assert QApplication.focusWidget() is sec.clear_button            # two documents: "Clear all" is next


def test_the_footer_is_quiet_while_every_switch_is_off(build, files):
    from ui.pages import live_documents as ld
    page, _ = build("pro")
    sec = page.documents
    sec.add_paths([_pdf(files), _docx(files)])
    _idle(sec)
    assert sec.foot.isVisibleTo(sec) and sec.privacy_label.text() == ld.LINE_UNDECIDED
    rows = list(sec._rows.values())
    rows[0].toggle.setChecked(False)
    assert sec.foot.isVisibleTo(sec)                                 # one is still on
    rows[1].toggle.setChecked(False)
    assert not sec.foot.isVisibleTo(sec) and sec.privacy_label.text() == ""
    rows[0].toggle.setChecked(True)
    assert sec.foot.isVisibleTo(sec) and sec.privacy_label.text() == ld.LINE_UNDECIDED


def test_documents_stay_after_the_page_is_built_again(build, store, files):
    page, _ = build()
    page.documents.add_paths([_pdf(files)])
    _idle(page.documents)
    page2, _ = build()                                              # a new page, the same folder
    assert [d.name for d in page2.documents.documents()] == ["Q3 plan.pdf"]
    assert next(iter(page2.documents._rows.values())).toggle.isChecked()


def test_names_are_plain_text_everywhere(build, qapp):
    """A name is never taken for HTML, a link or a shortcut key."""
    import live_docs
    page, _ = build()
    sec = page.documents
    evil = '<b>bold</b> & <img src="file:///C:/x.png"> &amp; %s.pdf'
    info = live_docs.DocInfo("d" + "0" * 16, evil, "pdf", "pdf", "page", 1000, 2, 100, 1, False, True, 1.0, False)
    sec.set_documents([info])
    row = sec._rows[info.id]
    for lb in (row.name_label, row.meta_label):
        assert lb.textFormat() == Qt.PlainText
    assert row.name_label.full_text() == evil
    assert "<b>" not in row.name_label.toolTip().replace("<p style='white-space:normal'>", "")
    assert "&lt;b&gt;" in row.name_label.toolTip()                   # escaped in the tooltip
    assert row.accessibleName().startswith(evil)
    from ui.pages.live_documents import NoteRow
    note = NoteRow(evil, "<i>oops</i>", "warn")
    assert note.message_label.textFormat() == Qt.PlainText and note.name_label.textFormat() == Qt.PlainText
    assert note.message_label.text() == "<i>oops</i>"
    note.deleteLater()


def test_a_name_with_bidi_characters_is_shown_clean(build, store, files):
    page, _ = build()
    sec = page.documents
    sec.add_paths([_docx(files, "report\u202egnp.exe.docx")])
    _idle(sec)
    shown = [d.name for d in sec.documents()]
    assert shown and "\u202e" not in shown[0] and shown[0].endswith(".docx")


def test_free_people_can_add_documents_without_a_pro_window(build, files):
    page, app = build("free")
    page.documents.add_paths([_pdf(files)])
    _idle(page.documents)
    assert len(page.documents._rows) == 1
    assert not [c for c in app.calls if c[0] == "_pro_upsell"]


def test_a_long_name_is_cut_with_an_ellipsis_and_kept_whole_for_the_tooltip(build, qapp):
    import live_docs
    page, _ = build()
    sec = page.documents
    long_name = "A very long quarterly report about everything the company did in the third quarter " * 2 + ".pdf"
    info = live_docs.DocInfo("d" + "1" * 16, long_name[:80], "pdf", "pdf", "page", 5000, 4, 900, 1, False, True, 1.0, False)
    sec.set_documents([info])
    row = sec._rows[info.id]
    row.resize(360, row.height())
    QApplication.processEvents()
    assert row.name_label.text().endswith("…") and len(row.name_label.text()) < len(info.name)
    assert row.name_label.full_text() == info.name and info.name in row.name_label.accessibleName()


def test_the_page_can_open_with_the_documents_in_focus(build):
    page, _ = build()
    page.window().activateWindow()
    page.on_open(focus="documents")
    assert page.documents.add_button.hasFocus() or QApplication.focusWidget() is page.documents.add_button


def test_closing_the_page_while_a_file_is_read_is_harmless(build, files, monkeypatch, caplog):
    import doc_reader
    page, _ = build()
    release = threading.Event()
    real = doc_reader.read_document
    monkeypatch.setattr(doc_reader, "read_document",
                        lambda path, cancel=None, *a, **k: (release.wait(5), real(path, cancel, *a, **k))[1])
    page.documents.add_paths([_docx(files)])
    assert _wait(lambda: page.documents.is_reading())
    host = page.parentWidget()
    host.hide()
    host.deleteLater()
    page.deleteLater()
    QApplication.processEvents()
    release.set()
    time.sleep(0.3)
    for _ in range(20):
        QApplication.processEvents()
        time.sleep(0.01)
    assert not [r for r in caplog.records if r.levelname in ("ERROR", "CRITICAL")]


# ── drag and drop ──
def _drag_enter(widget, mime):
    ev = QDragEnterEvent(QPoint(120, 120), Qt.CopyAction, mime, Qt.LeftButton, Qt.NoModifier)
    widget.dragEnterEvent(ev)
    return ev


def _drop(widget, mime):
    ev = QDropEvent(QPointF(120, 120), Qt.CopyAction, mime, Qt.LeftButton, Qt.NoModifier)
    widget.dropEvent(ev)
    return ev


def test_files_dropped_on_the_page_are_added(build, store, files):
    page, _ = build()
    sec = page.documents
    assert page.acceptDrops() and not page.context_edit.acceptDrops()  # a drop on the About box isn't a pasted path
    mime = _mime(_pdf(files), _docx(files))
    assert _drag_enter(page, mime).isAccepted()
    card = page.context_card                                         # ONE card: the whole card is the drop target
    assert card._overlay.isVisibleTo(card)                           # "Drop files to add"
    ev = _drop(page, mime)
    assert ev.isAccepted() and not card._overlay.isVisibleTo(card)
    _idle(sec)
    assert store.count() == 2


def test_a_web_link_or_text_dragged_over_the_page_is_not_taken(build, store):
    page, _ = build()
    assert not _drag_enter(page, _mime(urls=["https://example.com/report.pdf"])).isAccepted()
    text = QMimeData()
    text.setText("just words")
    assert not _drag_enter(page, text).isAccepted()
    assert not _drop(page, _mime(urls=["https://example.com/a.pdf"])).isAccepted()
    assert store.count() == 0 and not page.context_card._overlay.isVisibleTo(page.context_card)


def test_a_folder_dropped_gets_a_plain_note(build, tmp_path):
    page, _ = build()
    folder = tmp_path / "many"
    folder.mkdir()
    _drop(page, _mime(folder))
    _idle(page.documents)
    assert [n.message for n in page.documents._notes] == ["That's a folder. Add the files inside it instead."]


def test_leaving_the_page_clears_the_drop_veil(build, files):
    page, _ = build()
    _drag_enter(page, _mime(_pdf(files)))
    card = page.context_card
    assert card._overlay.isVisibleTo(card)
    card.drag_leave()
    assert not card._overlay.isVisibleTo(card)


def test_a_huge_drop_is_cut_to_a_batch(build, files, monkeypatch):
    from ui import live_docs_ui
    page, _ = build()
    seen = []
    monkeypatch.setattr(live_docs_ui.AddQueue, "add", lambda self, paths: seen.extend(paths) or len(paths))
    many = [files("f%03d.txt" % i, b"x%d" % i) for i in range(live_docs_ui.MAX_BATCH + 20)]
    page.documents.drop(_mime(*many))
    assert len(seen) == live_docs_ui.MAX_BATCH
    assert any("took the first %d" % live_docs_ui.MAX_BATCH in n.message for n in page.documents._notes)


def test_the_file_picker_adds_what_was_chosen(build, store, files, monkeypatch):
    page, _ = build()
    sec = page.documents
    chosen = [_docx(files), _pdf(files)]
    monkeypatch.setattr(sec, "ask_open_paths", lambda: chosen)
    sec.add_button.click()
    _idle(sec)
    assert store.count() == 2
    monkeypatch.setattr(sec, "ask_open_paths", lambda: [])
    sec.add_button.click()                                           # cancelling the picker adds nothing
    assert store.count() == 2
    # the picker offers every kind of file
    with mock.patch("ui.pages.live_documents.QFileDialog.getOpenFileNames", return_value=([], "")) as pick:
        sec.__class__.ask_open_paths(sec)
    assert pick.call_args[0][3] == "All files (*)"


# ── where the words go ──
def test_the_footer_says_where_the_words_go(build, files):
    from ui.pages import live_documents as ld
    page, app = build("pro")
    sec = page.documents
    sec.add_paths([_pdf(files)])
    _idle(sec)
    assert sec.privacy_label.text() == ld.LINE_UNDECIDED and not sec.privacy_link.isVisibleTo(sec)
    app.cfg["live_docs_cloud_ok"] = True
    page.on_setting_changed("live_docs_cloud_ok")
    assert sec.privacy_label.text() == ld.LINE_YES % "Gusan Cloud"
    assert sec.privacy_link.text() == "Don't send" and sec.privacy_link.isVisibleTo(sec)
    sec.privacy_link.click()                                         # changes the answer, no question
    assert app.cfg["live_docs_cloud_ok"] is False
    assert sec.privacy_label.text() == ld.LINE_NO and sec.privacy_link.text() == "Allow"
    app.cfg["privacy_mode"] = True
    page.on_setting_changed("privacy_mode")
    assert sec.privacy_label.text() == ld.LINE_LOCAL and not sec.privacy_link.isVisibleTo(sec)


def test_allow_asks_the_question_in_plain_words(build, files, monkeypatch):
    from ui.components import mini_prompt
    page, app = build("pro")
    sec = page.documents
    sec.add_paths([_pdf(files)])
    _idle(sec)
    app.cfg["live_docs_cloud_ok"] = False
    page.on_setting_changed("live_docs_cloud_ok")
    asked = []
    monkeypatch.setattr(mini_prompt, "ask", lambda parent, title, body, yes, no, icon="info", **kw:
                        asked.append((title, body, yes, no)) or True)
    sec.privacy_link.click()
    assert app.cfg["live_docs_cloud_ok"] is True
    assert asked[0][0] == "Use your documents with Gusan Cloud?"
    assert "Only the few parts that match a question are sent" in asked[0][1]
    assert asked[0][2:] == ("Use my documents", "Keep on this computer")


def test_a_local_writer_keeps_everything_on_this_computer(build, files):
    from ui.pages import live_documents as ld
    page, _ = build("free")                                          # Free: This computer writes the answers
    sec = page.documents
    sec.add_paths([_pdf(files)])
    _idle(sec)
    assert sec.privacy_label.text() == ld.LINE_LOCAL


# ── page and card stay in step ──
def test_a_change_on_the_card_reaches_the_page(build, store, files):
    from ui import live_docs_ui
    page, _ = build()
    sec = page.documents
    sec.add_paths([_docx(files)])
    _idle(sec)
    doc = sec.documents()[0]
    store.set_active(doc.id, False)                                  # what the card's list does
    live_docs_ui.bridge().changed.emit()
    assert _wait(lambda: not next(iter(sec._rows.values())).toggle.isChecked())


# ═══════════════════════════ ui.live_docs_ui ═══════════════════════════
def test_dropped_paths_takes_only_local_files_once_each(tmp_path):
    from ui import live_docs_ui
    a = tmp_path / "a.txt"
    a.write_text("a")
    paths, more = live_docs_ui.dropped_paths(_mime(a, a, urls=["https://x.test/b.pdf", "ftp://x.test/c"]))
    assert paths == [QUrl.fromLocalFile(str(a)).toLocalFile()] and not more
    assert live_docs_ui.dropped_paths(None) == ([], False)
    assert live_docs_ui.dropped_paths(QMimeData()) == ([], False)
    many = [tmp_path / ("m%d.txt" % i) for i in range(live_docs_ui.MAX_BATCH + 5)]
    got, more = live_docs_ui.dropped_paths(_mime(*many))
    assert len(got) == live_docs_ui.MAX_BATCH and more
    assert live_docs_ui.wants_files(_mime(a)) and not live_docs_ui.wants_files(_mime(urls=["https://x.test/a"]))


def test_a_job_always_reports_back_even_when_the_store_breaks(qapp, store, files):
    from ui import live_docs_ui
    boom = SimpleNamespace(add_file=mock.Mock(side_effect=RuntimeError("disk on fire")))
    job = live_docs_ui.AddJob([_docx(files), _pdf(files)], store=boom)
    got = {}
    job.finished.connect(lambda results, cancelled: got.update(results=results, cancelled=cancelled))
    job.start()
    assert _wait(lambda: "results" in got)
    assert len(got["results"]) == 2 and not any(r.ok for r in got["results"])
    assert all(r.reason == "error" and r.message for r in got["results"])
    assert not got["cancelled"]


def test_set_choice_works_without_a_controller_setting(qapp):
    from ui import live_docs_ui
    app = SimpleNamespace(cfg={"live_docs_cloud_ok": None}, save_config=mock.Mock())
    assert live_docs_ui.set_choice(app, True)
    assert app.cfg["live_docs_cloud_ok"] is True
    app.save_config.assert_called_once()
    seen = []
    app2 = SimpleNamespace(cfg={}, set_setting=lambda k, v: seen.append((k, v)) or False)
    live_docs_ui.set_choice(app2, False)
    assert seen == [("live_docs_cloud_ok", False)] and app2.cfg["live_docs_cloud_ok"] is False


def test_the_private_question_is_hidden_from_a_share(qapp, monkeypatch):
    from ui import glass
    from ui.components import mini_prompt
    hidden = []
    monkeypatch.setattr(glass, "exclude_from_capture", lambda w, on=True: hidden.append(on) or True)
    monkeypatch.setattr(mini_prompt.MiniPrompt, "exec", lambda self: QDialog.Accepted)
    assert mini_prompt.ask(None, "t", "b", "y", "n", private=True) is True
    assert hidden == [True]
    hidden.clear()
    assert mini_prompt.ask(None, "t", "b", "y", "n") is True
    assert hidden == []                                              # only when asked from the hidden card


# ═══════════════════════════ the card ═══════════════════════════
class _CardApp(QObject):
    """The controller bits the card uses."""
    sig_setting_changed = Signal(str)
    sig_auth_changed = Signal()

    def __init__(self, cfg=None, pro=True):
        super().__init__()
        self.cfg = dict(cfg or {})
        self.pro = pro
        self.tracked = []
        self.upsells = []

    def is_pro(self):
        return self.pro

    def set_setting(self, key, value):
        self.cfg[key] = value
        self.sig_setting_changed.emit(key)
        return True

    def schedule_config_save(self):
        pass

    def save_config(self):
        pass

    def track(self, event, props=None):
        self.tracked.append((event, props))

    def _pro_upsell(self, feature=None):
        self.upsells.append(feature)


@pytest.fixture
def glass_ok():
    from ui import glass
    with mock.patch.object(glass, "capture_exclusion_supported", return_value=True), \
            mock.patch.object(glass, "is_remote_session", return_value=False), \
            mock.patch.object(glass, "exclude_from_capture", return_value=True), \
            mock.patch.object(glass, "is_excluded_from_capture", return_value=True), \
            mock.patch.object(glass, "macos_26_or_later", return_value=False):
        yield glass


@pytest.fixture
def card(qapp, store, glass_ok):
    from ui import live_assist
    made = []

    def build(pro=True, **cfg):
        app = _CardApp(cfg, pro)
        c = live_assist.LiveAssistOverlay(main_app=app)
        made.append(c)
        return c, app
    yield build
    for c in made:
        c.hide()
        c.deleteLater()
    QApplication.processEvents()


def _engine_ids():
    import actions
    local = next(k for k, v in actions.ACTION_MODELS.items() if v.get("kind") == "local_llm")
    return {"managed": actions.API_MANAGED_ID, "cloud": actions.API_CEREBRAS_ID, "local": local}


@pytest.fixture
def answer(monkeypatch):
    """answer(card, app, route, question=..., auto=...) runs a real worker thread with a fake
    model and returns what the model was handed: {"context", "config", "text"}. ``engine``
    names another engine than the route's usual one."""
    import actions
    engines = _engine_ids()

    def run(c, app, route="managed", question="What is the payment term?", auto=False,
            live_text="So what are the payment terms for the supplier?", screen=False, extra_cfg=None,
            image_status=None, engine=None):
        seen = {}

        def fake_stream(context, mode, on_token, source_lang="auto", target_lang="en", model=None, config=None):
            seen["context"], seen["config"], seen["model"] = context, dict(config or {}), model
            if image_status:
                config["_image_status"].update(image_status)
            on_token("Payment is due in 45 days.")
            return "Payment is due in 45 days."
        monkeypatch.setattr(actions, "process_stream", fake_stream)
        cfg = dict(app.cfg)
        cfg.update(extra_cfg or {})
        c._resolve_engine = lambda: (engine or engines[route], dict(cfg))
        c._live_text = live_text
        c.btn_screen.setChecked(bool(screen))
        c.suggest(question, auto=auto)
        assert _wait(lambda: not c._suggesting and "context" in seen), "no answer"
        return seen
    return run


def _ask_spy(monkeypatch, answers=(True,)):
    """Replace the one-time question: it returns the next of ``answers`` and records each call."""
    from ui.components import mini_prompt
    calls = []
    it = iter(answers)

    def fake_ask(parent, title, body, yes, no, icon="info", **kw):
        calls.append({"title": title, "body": body, "yes": yes, "no": no, "kw": kw})
        return next(it, False)
    monkeypatch.setattr(mini_prompt, "ask", fake_ask)
    return calls


def _contract(store, files):
    return _add(store, _docx(files, "Supplier contract.docx",
                             ("The supplier delivers within 30 days.",
                              "Payment terms: payment is due in 45 days.")))


# -- the chip --
def test_no_chip_without_documents(card):
    c, _ = card()
    assert c.docs_bar.isHidden()


def test_the_chip_counts_the_active_documents_and_follows_the_switches(card, store, files):
    from ui import live_docs_ui
    c, _ = card()
    c.show()
    a, b = _add(store, _docx(files, "a.docx", ("alpha",))), _add(store, _docx(files, "b.docx", ("beta",)))
    live_docs_ui.bridge().changed.emit()
    assert not c.docs_bar.isHidden() and c.docs_chip.text() == "2 documents"
    assert "a.docx" in c.docs_chip.toolTip() and "choose" in c.docs_chip.toolTip()
    store.set_active(a.id, False)
    live_docs_ui.bridge().changed.emit()
    assert c.docs_chip.text() == "1 document"
    store.set_active(b.id, False)
    live_docs_ui.bridge().changed.emit()
    assert c.docs_chip.text() == "Documents off" and c.docs_chip.property("off") == "true"
    store.clear()
    live_docs_ui.bridge().changed.emit()
    assert c.docs_bar.isHidden()


def test_the_chip_opens_a_list_with_a_switch_for_each_document(card, store, files):
    from ui import live_docs_ui
    c, _ = card()
    c.show()
    one = _add(store, _docx(files, "A & B plan.docx", ("alpha",)))
    two = _add(store, _docx(files, "notes.docx", ("beta",)))
    live_docs_ui.bridge().changed.emit()
    c.docs_chip.click()
    menu = c._doc_menu
    assert isinstance(menu, QMenu) and menu.parent() is c            # the card's own: hidden from a share with it
    items = [a for a in menu.actions() if a.isCheckable()]
    assert [a.text() for a in items] == ["A && B plan.docx", "notes.docx"]     # & is shown, never a shortcut key
    assert [a.isChecked() for a in items] == [True, True]
    items[1].setChecked(False)
    assert not store.get(two.id).active and store.get(one.id).active
    assert c.docs_chip.text() == "1 document"
    assert "notes.docx isn't used in answers" in c.lbl_status.text()
    menu.close()


def test_a_change_on_the_page_reaches_the_card(card, store, files):
    from ui import live_docs_ui
    c, _ = card()
    c.show()
    _add(store, _docx(files))
    live_docs_ui.bridge().changed.emit()
    assert c.docs_chip.text() == "1 document"


def test_the_status_line_shows_file_names_as_plain_text(card):
    c, _ = card()
    assert c.lbl_status.textFormat() == Qt.PlainText


# -- files dropped on the card --
def test_files_dropped_on_the_card_become_documents(card, store, files):
    c, app = card()
    c.show()
    assert c.acceptDrops()
    for w in (c.input_ask, c.txt_summary, c.txt_suggestion):
        assert not w.acceptDrops()                                    # they hand drops up to the card
    mime = _mime(_pdf(files), _png(files))
    assert _drag_enter(c, mime).isAccepted() and c.drop_veil.isVisibleTo(c)
    assert _drop(c, mime).isAccepted() and not c.drop_veil.isVisibleTo(c)
    assert _wait(lambda: store.count() == 2 and not c._doc_queue.is_busy())
    assert c.docs_chip.text() == "2 documents"
    assert "Added" in c.lbl_status.text()
    assert [e for e, _p in app.tracked].count("live_document_added") == 2


def test_a_dropped_file_that_cannot_be_read_says_so_on_the_card(card, store, files):
    c, app = card()
    c.show()
    _drop(c, _mime(files("junk.xyz", b"\x00\x01\x02garbage\xff" * 80)))
    assert _wait(lambda: not c._doc_queue.is_busy() and "junk.xyz" in c.lbl_status.text())
    assert "can't read this kind of file yet" in c.lbl_status.text()
    assert store.count() == 0 and c.docs_bar.isHidden()
    assert [e for e, _p in app.tracked] == ["live_document_failed"]


def test_text_or_links_dropped_on_the_card_are_ignored(card):
    c, _ = card()
    c.show()
    assert not _drag_enter(c, _mime(urls=["https://example.com/a.pdf"])).isAccepted()
    assert not c.drop_veil.isVisibleTo(c)


def test_no_drop_while_the_card_is_collapsed(card, files):
    c, _ = card()
    c.show()
    c.set_expanded(False)
    assert not _drag_enter(c, _mime(_pdf(files))).isAccepted()


def test_a_file_dropped_on_an_open_editor_is_a_document_not_a_pasted_path(card):
    """The context (and invite) editors would paste a dropped file's path as text: they hand the
    drop up to the card instead, as the ask box and the answer box do."""
    c, _ = card()
    c.show()
    assert not c.ctx_edit.acceptDrops()
    inv = getattr(c, "inv_edit", None)                                # (the calendar invite's editor)
    assert inv is None or not inv.acceptDrops()


def test_removing_a_document_while_an_answer_is_written_is_harmless(card, answer, store, files, monkeypatch):
    import actions
    from ui import live_docs_ui
    c, app = card(live_docs_cloud_ok=True)
    c.show()
    info = _contract(store, files)
    live_docs_ui.bridge().changed.emit()
    assert c.docs_chip.text() == "1 document"
    started, release, seen = threading.Event(), threading.Event(), {}

    def slow_stream(context, mode, on_token, source_lang="auto", target_lang="en", model=None, config=None):
        seen["context"] = context
        started.set()
        release.wait(8)
        on_token("Payment is due in 45 days.")
        return "Payment is due in 45 days."
    monkeypatch.setattr(actions, "process_stream", slow_stream)
    c._resolve_engine = lambda: (_engine_ids()["managed"], dict(app.cfg))
    c._live_text = "So what are the payment terms for the supplier?"
    c.suggest("What is the payment term?")
    assert _wait(lambda: started.is_set())
    assert store.remove(info.id)                                      # the page's x, mid-answer
    live_docs_ui.bridge().changed.emit()
    assert c.docs_bar.isHidden()                                      # the chip follows at once
    release.set()
    assert _wait(lambda: not c._suggesting and "45 days" in c.txt_suggestion.toPlainText())
    assert "payment is due in 45 days" in seen["context"]             # what was already built stays as built
    nxt = answer(c, app, "managed", question="And the delivery time?")
    assert "Reference material" not in nxt["context"]                 # the next answer has no document


def test_a_document_that_vanishes_before_an_answer_is_left_out_quietly(card, answer, store, files):
    c, app = card(live_docs_cloud_ok=True)
    info = _contract(store, files)
    (store.directory() / (info.id + ".json")).unlink()                # its stored text is gone
    store._cache.clear()
    seen = answer(c, app, "managed")
    assert "Reference material" not in seen["context"]                # no crash, no half block
    assert _wait(lambda: "Payment is due in 45 days." in c.txt_suggestion.toPlainText())


# -- answers --
def test_without_documents_the_prompt_is_exactly_what_it_was(card, answer, monkeypatch):
    import live_docs
    c, app = card()
    seen = answer(c, app, "managed")
    assert live_docs.DOC_HEADER not in seen["context"] and "Reference material" not in seen["context"]
    assert seen["context"].endswith("User's question: What is the payment term?")
    assert "_image_png_b64" not in seen["config"]


def test_a_local_answer_gets_the_best_parts_within_the_small_budget(card, answer, store, files):
    import live_docs
    from live_context import LOCAL_DOC_CHARS
    c, app = card(live_docs_cloud_ok=None)
    _contract(store, files)
    filler = "\n\n".join("Section %d covers shipping, packaging and customs paperwork." % i for i in range(400))
    _add(store, files("big.txt", filler.encode()))
    seen = answer(c, app, "local")                                    # no question asked: this computer
    ctx = seen["context"]
    assert live_docs.DOC_HEADER in ctx
    block = ctx[ctx.index(live_docs.DOC_HEADER):ctx.index(live_docs.DOC_END) + len(live_docs.DOC_END)]
    assert len(block) <= LOCAL_DOC_CHARS
    assert "Payment terms: payment is due in 45 days." in ctx and "[from Supplier contract.docx" in ctx
    assert ctx.index(live_docs.DOC_BEGIN) < ctx.index("Conversation (latest part):") < ctx.index("User's question:")
    assert _wait(lambda: "from your documents" in c.lbl_status.text())
    assert c.lbl_status.text().count("from your documents") == 1


def _block(ctx):
    import live_docs
    return ctx[ctx.index(live_docs.DOC_HEADER):ctx.index(live_docs.DOC_END) + len(live_docs.DOC_END)]


def test_a_cloud_answer_may_carry_more_than_a_local_one(card, answer, store, files):
    from live_context import CLOUD_DOC_CHARS, LOCAL_DOC_CHARS
    c, app = card(live_docs_cloud_ok=True)
    zebra = "\n\n".join("Zebra crossing rule %d applies to pedestrians and cyclists near schools." % i
                        for i in range(600))                          # every part matches the question
    _add(store, files("zebra.txt", zebra.encode()))
    local = _block(answer(c, app, "local", question="What is the zebra rule?")["context"])
    cloud = _block(answer(c, app, "managed", question="What is the zebra rule?")["context"])
    assert len(local) <= LOCAL_DOC_CHARS
    assert LOCAL_DOC_CHARS < len(cloud) <= CLOUD_DOC_CHARS


def test_a_switched_off_document_is_never_used(card, answer, store, files):
    c, app = card(live_docs_cloud_ok=True)
    info = _contract(store, files)
    store.set_active(info.id, False)
    ctx = answer(c, app, "managed")["context"]
    assert "Supplier contract" not in ctx and "45 days" not in ctx


def test_an_automatic_answer_uses_the_documents_by_what_was_said(card, answer, store, files):
    import live_docs
    c, app = card(live_docs_cloud_ok=True)
    _contract(store, files)
    _add(store, files("holiday.txt", b"The office is closed on the first of May for the holiday."))
    ctx = answer(c, app, "managed", question="", auto=True,
                 live_text="We should settle this. What are the payment terms for the supplier?")["context"]
    assert live_docs.DOC_HEADER in ctx and "payment is due in 45 days" in ctx
    assert "first of May" not in ctx                                  # only what the talk was about
    assert "User's question:" not in ctx


def test_the_one_time_question_is_asked_for_a_typed_question_and_remembered(card, answer, store, files, monkeypatch):
    import live_docs
    c, app = card(live_docs_cloud_ok=None)
    _contract(store, files)
    calls = _ask_spy(monkeypatch, [True])
    ctx = answer(c, app, "managed")["context"]
    assert len(calls) == 1
    assert calls[0]["title"] == "Use your documents with Gusan Cloud?"
    assert "files stay on this computer" in calls[0]["body"]
    assert app.cfg["live_docs_cloud_ok"] is True                      # remembered
    assert live_docs.DOC_HEADER in ctx
    answer(c, app, "managed", question="And the delivery time?")
    assert len(calls) == 1                                            # never asked twice


def test_saying_no_keeps_the_documents_home_and_is_remembered(card, answer, store, files, monkeypatch):
    import live_docs
    c, app = card(live_docs_cloud_ok=None)
    _contract(store, files)
    calls = _ask_spy(monkeypatch, [False])
    seen = answer(c, app, "managed")
    assert len(calls) == 1 and app.cfg["live_docs_cloud_ok"] is False
    assert live_docs.DOC_HEADER not in seen["context"] and "45 days" not in seen["context"]
    assert "documents stayed on this computer" in c.lbl_status.text()
    answer(c, app, "managed", question="Again?")
    assert len(calls) == 1


def test_a_question_for_an_own_key_names_that_service(card, answer, store, files, monkeypatch):
    c, app = card(live_docs_cloud_ok=None)
    _contract(store, files)
    calls = _ask_spy(monkeypatch, [True])
    answer(c, app, "cloud")
    assert calls[0]["title"] == "Use your documents with the AI service you added a key for?"


def test_an_automatic_answer_never_interrupts_with_the_question(card, answer, store, files, monkeypatch):
    import live_docs
    c, app = card(live_docs_cloud_ok=None)
    _contract(store, files)
    calls = _ask_spy(monkeypatch, [True])
    seen = answer(c, app, "managed", question="", auto=True)
    assert calls == []                                                # nothing popped up mid-call
    assert live_docs.DOC_HEADER not in seen["context"]
    assert _wait(lambda: "documents not used yet" in c.lbl_status.text())
    assert app.cfg.get("live_docs_cloud_ok") is None                  # still undecided


def test_the_question_waits_while_asked_and_a_newer_one_cancels_it(card, answer, store, files, monkeypatch):
    """A worker waiting for the answer gives up when a newer question took over."""
    c, app = card(live_docs_cloud_ok=None)
    _contract(store, files)
    c._doc_asking = True                                              # a question is already open
    seen = answer(c, app, "managed")
    assert "Reference material" not in seen["context"]                # not asked twice: sent without
    assert "documents not used yet" in c.lbl_status.text()
    c._doc_asking = False


def test_privacy_mode_is_absolute_even_with_a_yes(card, answer, store, files, monkeypatch):
    import live_docs
    c, app = card(live_docs_cloud_ok=True, privacy_mode=True)
    _contract(store, files)
    calls = _ask_spy(monkeypatch, [True])
    seen = answer(c, app, "managed")                                  # (a cloud engine can't happen in Privacy mode; belt and braces)
    assert live_docs.DOC_HEADER not in seen["context"] and calls == []
    assert "documents stayed on this computer" in c.lbl_status.text()
    seen = answer(c, app, "local", question="What are the payment terms?")   # on this computer they are fine
    assert live_docs.DOC_HEADER in seen["context"]


def test_start_asks_before_the_first_question_and_warms_the_documents(card, store, files, monkeypatch):
    import actions
    c, app = card(live_docs_cloud_ok=None)
    _contract(store, files)
    calls = _ask_spy(monkeypatch, [True])
    warmed = []
    monkeypatch.setattr(actions, "warm_up", lambda engine, cfg: warmed.append(engine))
    monkeypatch.setattr(store, "warm", lambda: warmed.append("docs"))
    c._resolve_engine = lambda: (_engine_ids()["managed"], dict(app.cfg))
    c._warm_up_engine()                                               # what Start runs on a worker
    assert _wait(lambda: len(calls) == 1)
    assert calls[0]["title"].startswith("Use your documents with Gusan Cloud")
    assert warmed[0] == "docs" and warmed[-1] == _engine_ids()["managed"]   # before the possibly slow wake-up
    assert app.cfg["live_docs_cloud_ok"] is True


def test_start_does_not_ask_for_a_local_engine_or_without_documents(card, store, files, monkeypatch):
    import actions
    c, app = card(live_docs_cloud_ok=None)
    calls = _ask_spy(monkeypatch, [True])
    monkeypatch.setattr(actions, "warm_up", lambda engine, cfg: None)
    c._resolve_engine = lambda: (_engine_ids()["managed"], dict(app.cfg))
    c._warm_up_engine()                                               # no documents
    _contract(store, files)
    c._resolve_engine = lambda: (_engine_ids()["local"], dict(app.cfg))
    c._warm_up_engine()                                               # this computer
    QApplication.processEvents()
    assert calls == []


def test_the_question_from_the_hidden_card_is_hidden_from_a_share_too(card, store, files, monkeypatch):
    c, app = card(live_docs_cloud_ok=None)
    c._exclusion_ok = True
    calls = _ask_spy(monkeypatch, [True])
    req = {"gen": 0, "managed": True, "event": threading.Event(), "answer": None}
    c._on_doc_ask(req)
    assert calls[0]["kw"] == {"private": True} and req["event"].is_set() and req["answer"] is True
    c._exclusion_ok = False                                           # visible to a share anyway
    app.cfg["live_docs_cloud_ok"] = None
    c._on_doc_ask({"gen": 0, "managed": True, "event": threading.Event(), "answer": None})
    assert calls[1]["kw"] == {}


def test_a_decided_question_is_not_asked_again_by_a_late_request(card, monkeypatch):
    c, app = card(live_docs_cloud_ok=False)
    calls = _ask_spy(monkeypatch, [True])
    req = {"gen": 0, "managed": True, "event": threading.Event(), "answer": None}
    c._on_doc_ask(req)
    assert calls == [] and req["answer"] is False and req["event"].is_set()


# -- pictures --
def test_a_picture_rides_after_the_screenshot_on_a_cloud_route(card, answer, store, files, monkeypatch):
    from ui import live_assist
    c, app = card(live_docs_cloud_ok=True)
    pic = _add(store, _png(files, "whiteboard.png"))
    monkeypatch.setattr(live_assist, "capture_screen_b64", lambda *a, **k: "SCREEN")
    seen = answer(c, app, "managed", screen=True, question="What is on the whiteboard?")
    images = seen["config"]["_image_png_b64"]
    assert isinstance(images, list) and images[0] == "SCREEN" and len(images) == 2
    assert images[1].startswith("/9j/")                               # the stored, downscaled JPEG
    assert "whiteboard.png" in seen["context"] and "Pictures from the user's documents are attached" in seen["context"]
    assert pic.picture


def test_a_picture_alone_goes_as_a_list_of_one(card, answer, store, files):
    c, app = card(live_docs_cloud_ok=True)
    _add(store, _png(files, "whiteboard.png"))
    seen = answer(c, app, "cloud", screen=False, question="Read the whiteboard")
    assert isinstance(seen["config"]["_image_png_b64"], list) and len(seen["config"]["_image_png_b64"]) == 1


def test_pictures_never_go_to_this_computer_or_in_privacy_mode(card, answer, store, files):
    c, app = card(live_docs_cloud_ok=True)
    _add(store, _png(files, "whiteboard.png"))
    local = answer(c, app, "local", question="Read the whiteboard")
    assert "_image_png_b64" not in local["config"] and "Pictures from the user's" not in local["context"]
    assert _wait(lambda: "pictures need Gusan Cloud or your own key" in c.lbl_status.text())
    c2, app2 = card(live_docs_cloud_ok=True, privacy_mode=True)
    private = answer(c2, app2, "managed", question="Read the whiteboard")
    assert "_image_png_b64" not in private["config"] and "Pictures from the user's" not in private["context"]


def test_a_model_that_cannot_see_images_says_the_pictures_were_not_read(card, answer, store, files):
    c, app = card(live_docs_cloud_ok=True)
    _add(store, _png(files, "whiteboard.png"))
    answer(c, app, "cloud", question="Read the whiteboard", image_status={"dropped": True})
    assert _wait(lambda: "Pictures from your documents weren't read" in c.lbl_status.text())


def test_the_screenshot_note_still_means_the_screen(card, answer, store, files, monkeypatch):
    from ui import live_assist
    c, app = card(live_docs_cloud_ok=True)
    _contract(store, files)
    monkeypatch.setattr(live_assist, "capture_screen_b64", lambda *a, **k: "SCREEN")
    seen = answer(c, app, "managed", screen=True)
    assert seen["config"]["_image_png_b64"] == "SCREEN"               # only the screenshot: the plain string as ever
    assert _wait(lambda: "screen seen" in c.lbl_status.text() and "from your documents" in c.lbl_status.text())


# -- the answer box stays text --
def test_a_hostile_document_cannot_put_an_image_in_the_answer(card, store, files):
    """The reply path is covered in test_live_reply_safety.py; here a document that makes the
    model write one is simulated end to end."""
    c, app = card()
    c._on_suggestion("Here: ![x](file://attacker/share/a.png) and <img src='\\\\attacker\\s\\b.png'>", "")
    html = c.txt_suggestion.toHtml()
    assert "<img" not in html and "attacker" not in html
    assert "Here:" in c.txt_suggestion.toPlainText()
