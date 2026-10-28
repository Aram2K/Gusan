"""The trash icon on the other lists of recordings - Dictation's history, Audio to text, the Live
Assistant's past sessions - and the one component they share with Meetings
(ui/components/row_actions.py). Offscreen, temp data folder only."""
import json
import time

import pytest
from PySide6.QtCore import QEvent, QPointF, Qt
from PySide6.QtGui import QEnterEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QFrame, QHBoxLayout, QWidget

from tests_qt.test_page_audio_to_text import _save, build as audio_build, store   # noqa: F401 (fixtures)
from tests_qt.test_page_home_dictation import dict_page, made, pin_clock           # noqa: F401 (fixtures)
from tests_qt.test_page_live_assistant import build as live_build, la              # noqa: F401 (fixtures)


def _spin(ms=30):
    end = time.monotonic() + ms / 1000.0
    while time.monotonic() < end:
        QApplication.processEvents()


def _wait_for(cond, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        QApplication.processEvents()
        if cond():
            return True
        time.sleep(0.01)
    return cond()


def _enter(w):
    QApplication.sendEvent(w, QEnterEvent(QPointF(4, 4), QPointF(4, 4), QPointF(4, 4)))
    _spin(10)


def _leave(w):
    QApplication.sendEvent(w, QEvent(QEvent.Leave))
    _spin(10)


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    from ui.components import row_actions
    from ui.pages import meeting_views
    monkeypatch.setattr(meeting_views, "SYNC_BG", True)
    monkeypatch.setattr(row_actions, "ANIMATE", False)


@pytest.fixture
def asked(monkeypatch):
    from ui.components import mini_prompt

    class Asked(list):
        answers = None
    log = Asked()
    answers = log.answers = []

    def confirm(parent, title, text, yes="OK", no="Cancel", danger=True, **kw):
        log.append({"title": title, "text": text, "yes": yes, "no": no, "danger": danger, "detail": kw.get("detail"),
                    "icon": kw.get("icon")})
        return answers.pop(0) if answers else True
    monkeypatch.setattr(mini_prompt, "confirm", confirm)
    return log


# ═══════════════════════════ the shared component ═══════════════════════════
def test_the_trash_button_names_itself_and_says_why_it_is_off(qapp):
    from ui.components import row_actions
    holder = QFrame()
    lay = QHBoxLayout(holder)
    pressed = []
    btn = row_actions.attach_trash(holder, lay, "Q4 roadmap", lambda: pressed.append(1))
    holder.resize(300, 40)
    holder.show()
    try:
        assert btn.accessibleName() == "Delete Q4 roadmap" and btn.toolTip() == "Delete"
        assert btn.focusPolicy() == Qt.NoFocus and (btn.width(), btn.height()) == (28, 28)
        assert not btn.isVisibleTo(holder) and btn.target_opacity() == 0.0
        btn.fade_to(1.0, animate=False)
        assert btn.isVisibleTo(holder)
        btn.click()
        assert pressed == [1]
        btn.set_blocked("Recording now")
        assert not btn.isEnabled() and btn.toolTip() == "Recording now" and btn.accessibleDescription() == "Recording now"
        assert btn.accessibleName() == "Delete Q4 roadmap"
        btn.click()
        assert pressed == [1]                                          # nothing to press
        btn.set_blocked("")
        assert btn.isEnabled() and btn.toolTip() == "Delete"
        btn.set_name("Weekly sync")
        assert btn.accessibleName() == "Delete Weekly sync"
        btn.fade_to(0.0, animate=False)
        assert not btn.isVisibleTo(holder)                             # hidden, but its place stays
        assert btn.sizePolicy().retainSizeWhenHidden()
    finally:
        holder.close()


def test_the_reveal_rule_hover_keyboard_narrow_and_off(qapp):
    from ui.components import row_actions
    holder = QFrame()
    lay = QHBoxLayout(holder)
    btn = row_actions.attach_trash(holder, lay, "x", lambda: None)
    rv = btn.reveal
    holder.resize(900, 40)
    holder.move(200, 300)                                              # (the offscreen pointer sits at 0, 0)
    holder.show()
    try:
        _spin(20)
        assert rv.target() == 0.0
        _enter(holder)
        assert rv.target() == 1.0 and btn.target_opacity() == 1.0
        _leave(holder)
        assert rv.target() == 0.0
        holder.setFocusPolicy(Qt.TabFocus)                             # now the keyboard can be on it
        holder.setFocus(Qt.TabFocusReason)
        _spin(20)
        assert rv.target() == row_actions.REST_OPACITY == 0.4
        holder.setFocusPolicy(Qt.NoFocus)
        holder.clearFocus()
        holder.resize(300, 40)                                         # narrow (touch): quiet but there
        _spin(20)
        assert rv.target() == 0.4
        rv.enabled = False                                             # while a list is selecting
        rv.refresh(animate=False)
        assert btn.target_opacity() == 0.0
        assert row_actions.FADE_MS == 150
    finally:
        holder.close()


def test_a_bar_message_with_a_long_name_wraps_and_a_screen_reader_hears_the_real_words(qapp):
    from ui.components import row_actions
    bar = row_actions.ActionBar("info")
    bar.resize(500, 60)
    bar.show()
    said = "Deleted “%s”." % ("W" * 200)
    bar.set_message(said)
    _spin(10)
    assert bar.message.minimumSizeHint().width() < 300                    # it can wrap: no 200-letter minimum
    assert bar.accessibleName() == said and "W" * 200 in bar.message.text().replace("​", "")
    bar.close()


# ═══════════════════════════ Dictation ═══════════════════════════
def test_a_dictation_row_has_a_trash_icon_that_asks_then_deletes_only_that_one(dict_page, asked):
    import history
    page, app, _info = dict_page("pro")
    before = history.load()
    rows = page.shown_rows()
    assert rows and all(r.trash.accessibleName().startswith("Delete ") for r in rows)
    row = rows[1]
    text = row.entry["text"]
    assert row.trash.accessibleName() == "Delete " + text[:40]
    _enter(row)
    assert row.trash.target_opacity() == 1.0
    asked.answers.append(False)
    row.trash.click()                                                    # cancelled: nothing happens
    assert len(asked) == 1 and asked[0]["title"] == "Delete this dictation?" and asked[0]["danger"] is True
    assert asked[0]["text"] == text and asked[0]["detail"] == "It is removed from this computer."
    assert (asked[0]["yes"], asked[0]["no"], asked[0]["icon"]) == ("Delete", "Cancel", "trash")
    assert history.load() == before
    row.trash.click()                                                    # confirmed
    after = history.load()
    assert len(after) == len(before) - 1 and all(e["text"] != text for e in after)
    assert [e for e in before if e["text"] != text] == after             # every other entry untouched, in order
    assert text not in [r.entry["text"] for r in page.shown_rows()]
    assert ("track", "history_cleared", {"count": 1, "meetings": 0, "from": "dictation"}) in app.calls


def test_the_delete_key_on_a_dictations_copy_button_asks_too(dict_page, asked):
    import history
    page, _app, _info = dict_page("pro")
    row = page.shown_rows()[0]
    n = len(history.load())
    row.copy_button.setFocus(Qt.TabFocusReason)
    QTest.keyClick(row.copy_button, Qt.Key_Delete)
    assert len(asked) == 1 and len(history.load()) == n - 1


def test_the_page_keeps_the_twin_of_a_deleted_dictation_on_screen(dict_page, asked):
    """The saved history loses one of two identical dictations; the list on screen must lose exactly
    one too (it used to drop both until the next reload)."""
    import history
    page, _app, _info = dict_page("pro")
    e = {"text": "Same words twice.", "language": "en", "backend": "local", "timestamp": "2026-10-04 11:00:00"}
    history.save_all([dict(e), dict(e)] + history.load())
    page._entries = history.load()
    page._render_list()
    twins = [r for r in page.shown_rows() if r.entry["text"] == e["text"]]
    assert len(twins) == 2
    twins[0].trash.click()
    assert len([x for x in history.load() if x["text"] == e["text"]]) == 1
    assert len([r for r in page.shown_rows() if r.entry["text"] == e["text"]]) == 1


def test_two_dictations_of_the_same_words_go_one_at_a_time(dict_page):
    import history
    e = {"text": "Same words.", "language": "en", "backend": "local", "timestamp": "2026-10-04 11:00:00"}
    history.save_all([dict(e), dict(e), {"text": "Other", "language": "en", "backend": "local",
                                          "timestamp": "2026-10-04 10:00:00"}])
    assert history.delete_entries([dict(e)]) == 1
    assert [x["text"] for x in history.load()] == ["Same words.", "Other"]
    assert history.delete_entries([{"text": "not there"}]) == 0


# ═══════════════════════════ Audio to text ═══════════════════════════
def test_an_audio_row_has_a_trash_icon_that_opens_its_own_question_in_the_row(audio_build, store):
    tid = _save(store, "board_call.mp3", 6000)
    keep = _save(store, "lecture_week3.mp4", 60)
    page, _ = audio_build()
    row = page.files.row(tid)
    assert row.trash.accessibleName() == "Delete board_call.mp3" and row.trash.toolTip() == "Delete"
    _enter(row)
    assert row.trash.target_opacity() == 1.0 and row.trash.isVisibleTo(row)
    row.trash.click()                                                    # the same Delete / Keep as the ⋯ menu
    assert row.delete_button.isVisibleTo(row) and row.keep_button.isVisibleTo(row)
    assert not row.more_button.isVisibleTo(row) and not row.trash.isVisibleTo(row)
    assert store.load_transcript(tid) is not None                        # nothing is deleted by the icon alone
    row.keep_button.click()                                              # Keep
    assert not row.delete_button.isVisibleTo(row) and row.more_button.isVisibleTo(row)
    _enter(row)
    assert row.trash.isVisibleTo(row)
    row.trash.click()
    row.delete_button.click()                                            # Delete
    assert _wait_for(lambda: page.files.row(tid) is None)
    assert page.files.row(keep) is not None


# ═══════════════════════════ the Live Assistant's past sessions ═══════════════════════════
def _live_session(root, name, title, seconds=2280):
    d = root / name
    d.mkdir()
    (d / "meta.json").write_text(json.dumps({"title": title, "duration_sec": seconds, "source": "live_assist",
                                             "started": "%s-%s-%s 14:18:00" % (name[:4], name[4:6], name[6:8])}),
                                 encoding="utf-8")
    (d / "transcript.txt").write_text("hello", encoding="utf-8")
    return d


@pytest.fixture
def meetings_root(tmp_path, monkeypatch):
    import storage
    root = tmp_path / "meetings"
    root.mkdir()
    real = storage.path_for
    monkeypatch.setattr(storage, "path_for", lambda name: root if name == "meetings" else real(name))
    return root


def test_a_past_session_is_deleted_from_its_row_with_the_same_question_and_undo(live_build, meetings_root, asked):
    a = _live_session(meetings_root, "20261007_141800", "Weekly sync")
    b = _live_session(meetings_root, "20261006_101000", "Customer call", 600)
    page, app = live_build("pro")
    sessions = [{"dir": str(a), "id": a.name, "started": "2026-10-07 14:18:00", "duration_sec": 2280,
                 "text": "“Aram, when can we launch?”", "answers": 6},
                {"dir": str(b), "id": b.name, "started": "2026-10-06 10:10:00", "duration_sec": 600,
                 "text": "Customer call", "answers": 0}]
    page.set_sessions(sessions)
    rows = page.session_rows
    assert len(rows) == 2 and rows[0].trash.accessibleName() == "Delete “Aram, when can we launch?”"
    _enter(rows[0])
    assert rows[0].trash.target_opacity() == 1.0
    asked.answers.append(False)
    rows[0].trash.click()
    assert asked[0]["title"] == "Delete “Aram, when can we launch?”?"
    assert "38 min" in asked[0]["text"] and a.is_dir()                   # cancelled: still there
    rows[0].trash.click()                                                # confirmed
    assert not a.exists() and b.is_dir()
    assert [r.sess["dir"] for r in page.session_rows] == [str(b)]
    bar = page.deleter.bar
    assert bar.isVisibleTo(page) and bar.message.text() == "Deleted 1 meeting."
    assert ("track", "meeting_deleted", {"count": 1, "from": "live"}) in app.calls
    bar.undo_button.click()
    assert a.is_dir() and (a / "transcript.txt").read_text(encoding="utf-8") == "hello"
    assert not (meetings_root / ".trash").exists()


def test_a_session_list_read_before_the_delete_does_not_bring_it_back(live_build, meetings_root, asked):
    a = _live_session(meetings_root, "20261007_141800", "Weekly sync")
    b = _live_session(meetings_root, "20261006_101000", "Customer call", 600)
    page, _app = live_build("pro")
    both = [{"dir": str(a), "id": a.name, "started": "2026-10-07 14:18:00", "duration_sec": 2280,
             "text": "Weekly sync", "answers": 0},
            {"dir": str(b), "id": b.name, "started": "2026-10-06 10:10:00", "duration_sec": 600,
             "text": "Customer call", "answers": 0}]
    page.set_sessions(list(both))
    page.session_rows[0].trash.click()
    assert [r.sess["dir"] for r in page.session_rows] == [str(b)]
    page.set_sessions(list(both))                                        # a read that began before the move
    assert [r.sess["dir"] for r in page.session_rows] == [str(b)]
    page.deleter.bar.undo_button.click()                                 # Undo: it may show again
    page.set_sessions(list(both))
    assert [r.sess["dir"] for r in page.session_rows] == [str(a), str(b)]


def test_the_session_being_recorded_cannot_be_deleted(live_build, meetings_root, asked):
    from tests_qt.test_page_meetings import FakeSession
    a = _live_session(meetings_root, "20261007_141800", "Weekly sync")
    session = FakeSession("recording")
    session.meeting_dir = str(a)
    page, app = live_build("pro")
    app.meeting_session = session
    page.set_sessions([{"dir": str(a), "id": a.name, "started": "2026-10-07 14:18:00", "duration_sec": 60,
                        "text": "Weekly sync", "answers": 0}])
    row = page.session_rows[0]
    assert not row.trash.isEnabled() and row.trash.toolTip() == "Recording now"
    row.trash.click()
    assert asked == [] and a.is_dir()


def test_the_delete_key_on_a_sessions_open_button_asks(live_build, meetings_root, asked):
    a = _live_session(meetings_root, "20261007_141800", "Weekly sync")
    page, _app = live_build("pro")
    page.set_sessions([{"dir": str(a), "id": a.name, "started": "2026-10-07 14:18:00", "duration_sec": 60,
                        "text": "Weekly sync", "answers": 0}])
    row = page.session_rows[0]
    row.open_button.setFocus(Qt.TabFocusReason)
    QTest.keyClick(row.open_button, Qt.Key_Delete)
    assert len(asked) == 1 and not a.exists()


# ═══════════════════════════ pictures: the same icon on every list, light and dark ═══════════════════════════
def _save_picture(widget, name, scheme):
    import os
    folder = os.environ.get("GUSAN_QT_SHOTS", "")
    if not folder:
        return
    os.makedirs(folder, exist_ok=True)
    assert widget.grab().save(os.path.join(folder, "meetings_delete_lists_%s_%s.png" % (name, scheme)))


def test_the_icon_looks_the_same_on_every_list(scheme, dict_page, audio_build, store, live_build, meetings_root):
    page, _app, _info = dict_page("pro")
    page.resize(1100, 700)
    page.show()
    _spin(40)
    row = page.shown_rows()[1]
    _enter(row)
    assert row.trash.shown_opacity() == 1.0
    _save_picture(page, "dictation", scheme)
    _leave(row)
    tid = _save(store, "board_call.mp3", 6000)
    _save(store, "lecture_week3.mp4", 60)
    apage, _ = audio_build()
    apage.resize(1100, 700)
    apage.show()
    _spin(40)
    arow = apage.files.row(tid)
    _enter(arow)
    assert arow.trash.shown_opacity() == 1.0
    _save_picture(apage, "audio_to_text", scheme)
    a = _live_session(meetings_root, "20261007_141800", "Weekly sync")
    lpage, _lapp = live_build("pro")
    lpage.resize(1100, 780)
    lpage.set_sessions([{"dir": str(a), "id": a.name, "started": "2026-10-07 14:18:00", "duration_sec": 2280,
                         "text": "“Aram, when can we launch?”", "answers": 6}])
    lpage.show()
    _spin(40)
    lrow = lpage.session_rows[0]
    _enter(lrow)
    assert lrow.trash.shown_opacity() == 1.0
    _save_picture(lpage, "live_sessions", scheme)
