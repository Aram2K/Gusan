"""Delete straight from the Meetings list (ui/pages/meetings.py MeetingsList / MeetingRow,
ui/pages/meeting_delete.py, meeting_trash.py): the trash icon on a row, selecting several, the
Delete key, the question, the worker thread, a meeting that can't go, Undo, and what is on screen.
Offscreen, real temp folders, never the real library."""
import json
import os
import threading
import time

import pytest
from PySide6.QtCore import QEvent, QPointF, Qt
from PySide6.QtGui import QEnterEvent, QGuiApplication
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLabel

from tests_qt.conftest import make_stub_app
from tests_qt.test_page_meetings import FakeSession, NOTES, _meeting, _open

SHOTS = os.environ.get("GUSAN_QT_SHOTS", "")           # a folder: the picture tests save their pictures there


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
    QApplication.processEvents()
    return cond()


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    from ui.components import row_actions
    from ui.pages import meeting_views
    monkeypatch.setattr(meeting_views, "SYNC_BG", True)        # background work inline
    monkeypatch.setattr(row_actions, "ANIMATE", False)         # fades and folds finish at once


@pytest.fixture
def meetings_root(tmp_path, monkeypatch):
    import storage
    root = tmp_path / "meetings"
    root.mkdir()
    real = storage.path_for
    monkeypatch.setattr(storage, "path_for", lambda name: root if name == "meetings" else real(name))
    return root


class Session(FakeSession):
    """A session whose background speaker pass can be on."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.pass_state = ""

    def speakers_state(self, folder=None):
        return self.pass_state


@pytest.fixture
def shell(qapp, monkeypatch):
    from ui import shell as shell_mod
    from ui.pages import meetings as page_mod
    monkeypatch.setattr(page_mod, "_ensure_consent", lambda parent, app: True)
    made = []

    def build(tier="pro", **attrs):
        app = make_stub_app(tier)
        for k, v in attrs.items():
            setattr(app, k, v)
        win = shell_mod.MainWindow(app)
        win.resize(1000, 760)
        made.append(win)
        return win, app

    yield build
    for w in made:
        w.hide()
        w.deleteLater()
    QApplication.processEvents()


@pytest.fixture
def asked(monkeypatch):
    """Every question the card would ask; each answer is taken from ``answer`` (a list: True, False...)."""
    from ui.components import mini_prompt

    class Asked(list):
        answers = None

    log = Asked()
    answers = log.answers = []

    def confirm(parent, title, text, confirm_label="OK", cancel_label="Cancel", danger=True, **kw):
        log.append({"title": title, "text": text, "yes": confirm_label, "no": cancel_label, "danger": danger,
                    "detail": kw.get("detail"), "icon": kw.get("icon")})
        return answers.pop(0) if answers else True
    monkeypatch.setattr(mini_prompt, "confirm", confirm)
    return log


def _three(root):
    a = _meeting(root, "20260929_110000", "Q4 roadmap")
    b = _meeting(root, "20260928_100000", "Weekly product sync", duration_sec=1800)
    c = _meeting(root, "20260927_090000", "Design review", duration_sec=3600)
    return a, b, c


def _list(win):
    page = _open(win)
    return page, page.list_view.meetings


def _titles(ml):
    return [r.meeting["title"] for r in ml.rows]


def _enter(row):
    QApplication.sendEvent(row, QEnterEvent(QPointF(5, 5), QPointF(5, 5), QPointF(5, 5)))
    _spin(10)


def _leave(row):
    QApplication.sendEvent(row, QEvent(QEvent.Leave))
    _spin(10)


# ═══════════════════════════ the icon on a row ═══════════════════════════
def test_every_row_has_a_trash_icon_that_shows_on_hover_and_keyboard_focus(shell, meetings_root):
    _three(meetings_root)
    win, _app = shell("pro")
    win.show()
    page, ml = _list(win)
    row = ml.rows[0]
    btn = row.trash
    assert btn.toolTip() == "Delete" and btn.accessibleName() == "Delete Q4 roadmap"
    assert btn.isEnabled() and btn.focusPolicy() == Qt.NoFocus      # the Delete key is the keyboard's way
    _spin(20)
    assert btn.target_opacity() == 0.0 and not btn.isVisibleTo(row)       # nothing on a quiet list...
    _enter(row)
    assert btn.target_opacity() == 1.0 and btn.isVisibleTo(row)           # ...the pointer brings it in
    assert btn.shown_opacity() == 1.0
    _leave(row)
    assert btn.target_opacity() == 0.0 and not btn.isVisibleTo(row)
    row.setFocus(Qt.TabFocusReason)
    _spin(20)
    assert btn.target_opacity() == 0.4 and btn.isVisibleTo(row)           # the keyboard on the row: quiet but there
    other = ml.rows[1]
    other.setFocus(Qt.TabFocusReason)
    _spin(20)
    assert btn.target_opacity() == 0.0 and ml.rows[1].trash.target_opacity() == 0.4


def test_a_narrow_row_keeps_its_icon_at_forty_percent(shell, meetings_root):
    _three(meetings_root)
    win, _app = shell("pro")
    win.show()
    page, ml = _list(win)
    row = ml.rows[2]
    row.trash.reveal.narrow_below = 10000                                # a window as narrow as a phone
    row.trash.reveal.refresh()
    assert row.trash.target_opacity() == 0.4
    _enter(row)
    assert row.trash.target_opacity() == 1.0                              # (and full on hover)


def test_the_fade_takes_150_ms(qapp, shell, meetings_root, monkeypatch):
    from ui.components import row_actions
    monkeypatch.setattr(row_actions, "ANIMATE", True)
    _three(meetings_root)
    win, _app = shell("pro")
    win.show()
    page, ml = _list(win)
    row = ml.rows[0]
    assert row.trash._anim.duration() == 150
    _enter(row)
    assert row.trash.target_opacity() == 1.0
    assert _wait_for(lambda: row.trash.shown_opacity() == 1.0, 2.0)


def test_pressing_the_trash_asks_and_never_opens_the_meeting(shell, meetings_root, asked):
    _three(meetings_root)
    win, _app = shell("pro")
    win.show()
    page, ml = _list(win)
    asked.answers.append(False)                                           # "Cancel"
    row = ml.rows[0]
    _enter(row)
    QTest.mouseClick(row.trash, Qt.LeftButton)
    assert page.current_view() == "list"                                  # not the notes
    assert len(asked) == 1 and asked[0]["danger"] is True
    assert [r.meeting["title"] for r in ml.rows] == ["Q4 roadmap", "Weekly product sync", "Design review"]
    assert (meetings_root / "20260929_110000").is_dir()                   # cancelled: nothing happened


def test_the_question_names_the_meeting_and_says_what_goes(shell, meetings_root, asked):
    _three(meetings_root)
    win, _app = shell("pro")
    page, ml = _list(win)
    asked.answers.append(False)
    ml.rows[1].trash.click()
    q = asked[0]
    assert q["title"] == "Delete “Weekly product sync”?"
    assert "30 min" in q["text"] and "Mon 28 Sep" in q["text"]            # the length and the day
    assert q["detail"] == "Its notes, transcript, recording and speaker names are removed from this computer."
    assert (q["yes"], q["no"], q["icon"]) == ("Delete", "Cancel", "trash")


def test_a_meeting_is_deleted_off_the_list_and_can_be_brought_back(shell, meetings_root, asked):
    a, b, c = _three(meetings_root)
    win, _app = shell("pro")
    win.show()
    page, ml = _list(win)
    ml.rows[1].trash.click()                                              # Weekly product sync, confirmed
    assert _titles(ml) == ["Q4 roadmap", "Design review"]
    assert not b.exists() and a.exists() and c.exists()
    bar = ml.deleter.bar
    assert bar.isVisibleTo(ml) and bar.message.text() == "Deleted 1 meeting."
    assert bar.undo_button.isVisibleTo(bar) and bar.undo_button.accessibleName() == "Undo"
    assert (meetings_root / ".trash").is_dir()
    bar.undo_button.click()                                               # Undo
    assert _wait_for(lambda: _titles(ml) == ["Q4 roadmap", "Weekly product sync", "Design review"])
    assert b.is_dir() and json.loads((b / "meta.json").read_text(encoding="utf-8"))["title"] == "Weekly product sync"
    assert (b / "notes.md").read_text(encoding="utf-8") == NOTES          # exactly as it was
    assert not bar.isVisibleTo(ml)
    assert not (meetings_root / ".trash").exists()                        # nothing left behind


def test_the_bar_closes_after_ten_seconds_and_the_folder_is_deleted_for_good(shell, meetings_root, asked,
                                                                          monkeypatch):
    _a, b, _c = _three(meetings_root)
    win, _app = shell("pro")
    page, ml = _list(win)
    ml.rows[1].trash.click()
    d = ml.deleter
    assert d._timer.isActive() and d._timer.interval() == 10 * 1000
    assert len(d.pending()) == 1
    d._timer.stop()
    d.dismiss()                                                           # what the timer (or the ×) does
    assert not d.bar.isVisibleTo(ml) and d.pending() == []
    assert not (meetings_root / ".trash").exists() and not b.exists()


def test_the_undo_bar_is_in_sight_however_far_the_list_is_scrolled(shell, meetings_root, asked):
    """The list is long and the person deletes from wherever they have scrolled to. A bar at the list's top
    would be out of sight (an Undo nobody sees is no Undo): it is a toast over the bottom of the page."""
    from PySide6.QtCore import QRect
    for i in range(24):
        _meeting(meetings_root, "20260%d%02d_110000" % (9 if i < 28 else 10, i + 1), "Meeting %02d" % i)
    win, _app = shell("pro")
    win.resize(1000, 700)
    win.show()
    page, ml = _list(win)
    holder = page._holders["list"]
    sb = holder.verticalScrollBar()
    assert _wait_for(lambda: len(ml.rows) == 24 and sb.maximum() > 400)
    sb.setValue(sb.maximum())                                             # the end of the list
    _spin(30)
    ml.rows[-1].trash.click()
    bar = ml.deleter.bar
    assert bar.isVisible() and bar.docked() and bar.parentWidget() is page
    assert bar.message.text() == "Deleted 1 meeting." and bar.undo_button.isVisible()
    geo = QRect(bar.geometry())
    assert QRect(0, 0, page.width(), page.height()).contains(geo)         # inside the page...
    assert geo.bottom() >= page.height() - 40 and abs(geo.center().x() - page.width() // 2) <= 2    # ...at its bottom, centred
    assert geo.width() <= 560
    sb.setValue(0)                                                        # scroll to the top: the bar does not move
    _spin(30)
    assert bar.isVisible() and QRect(bar.geometry()) == geo
    top = page.childAt(geo.center())
    assert top is bar or bar.isAncestorOf(top)                           # in front of the rows, not behind them
    win.resize(700, 600)                                                  # a smaller window: it follows the bottom
    _spin(30)
    assert bar.geometry().bottom() >= page.height() - 40 and bar.width() <= page.width() - 40
    bar.undo_button.click()
    assert _wait_for(lambda: len(ml.rows) == 24) and not bar.isVisible()


def test_two_quick_deletes_share_one_undo(shell, meetings_root, asked):
    a, b, c = _three(meetings_root)
    win, _app = shell("pro")
    page, ml = _list(win)
    ml.rows[0].trash.click()
    ml.rows[0].trash.click()                                              # the next one is the first row now
    assert ml.deleter.bar.message.text() == "Deleted 2 meetings."
    ml.deleter.bar.undo_button.click()
    assert _wait_for(lambda: len(ml.rows) == 3)
    assert a.is_dir() and b.is_dir() and c.is_dir()


def test_the_last_meeting_deleted_brings_the_example_back(shell, meetings_root, asked):
    _meeting(meetings_root, "20260929_110000", "Q4 roadmap")
    win, _app = shell("pro")
    win.show()
    page, ml = _list(win)
    ml.rows[0].trash.click()
    assert ml.rows == [] and ml.example.isVisibleTo(ml) and not ml.search.isVisibleTo(ml)
    assert not ml.select_button.isVisibleTo(ml)


def test_telemetry_counts_but_never_names(shell, meetings_root, asked):
    _three(meetings_root)
    win, app = shell("pro")
    page, ml = _list(win)
    ml.rows[0].trash.click()
    sent = [c for c in app.calls if c[:2] == ("track", "meeting_deleted")]
    assert sent == [("track", "meeting_deleted", {"count": 1, "from": "row"})]


# ═══════════════════════════ select several ═══════════════════════════
def test_select_shows_check_boxes_and_a_bar_with_the_count(shell, meetings_root, asked):
    _three(meetings_root)
    win, _app = shell("pro")
    win.show()
    page, ml = _list(win)
    assert ml.select_button.isVisibleTo(ml) and ml.select_button.accessibleName() == "Select meetings to delete"
    assert not ml.select_bar.isVisibleTo(ml)
    ml.select_button.click()
    assert ml.select_mode and ml.select_bar.isVisibleTo(ml) and not ml.select_button.isVisibleTo(ml)
    assert all(r.check.isVisibleTo(r) for r in ml.rows)
    assert ml.select_bar.message.text() == "Choose the meetings to delete" and not ml.select_bar.delete_button.isEnabled()
    QTest.mouseClick(ml.rows[0], Qt.LeftButton)
    QTest.mouseClick(ml.rows[2], Qt.LeftButton)
    assert page.current_view() == "list"                                  # ticking never opens
    assert ml.select_bar.message.text() == "2 selected" and ml.select_bar.delete_button.isEnabled()
    assert [r.is_checked() for r in ml.rows] == [True, False, True]
    assert ml.rows[0].accessibleName().endswith(", selected") and ml.rows[1].accessibleName().endswith(", not selected")
    QTest.mouseClick(ml.rows[0], Qt.LeftButton)                           # again: off
    assert ml.select_bar.message.text() == "1 selected"
    assert all(not r.trash.isVisibleTo(r) for r in ml.rows)               # no trash icons while selecting


def test_shift_click_selects_a_range_and_ctrl_a_everything(shell, meetings_root, asked):
    for i in range(6):
        _meeting(meetings_root, "2026092%d_110000" % (i + 1), "Meeting %d" % i)
    win, _app = shell("pro")
    win.show()
    page, ml = _list(win)
    ml.enter_select()
    QTest.mouseClick(ml.rows[1], Qt.LeftButton)
    QTest.mouseClick(ml.rows[4], Qt.LeftButton, Qt.ShiftModifier)
    assert [r.is_checked() for r in ml.rows] == [False, True, True, True, True, False]
    ml.rows[0].setFocus(Qt.TabFocusReason)
    QTest.keyClick(ml.rows[0], Qt.Key_A, Qt.ControlModifier)
    assert all(r.is_checked() for r in ml.rows) and ml.select_bar.message.text() == "6 selected"
    ml.select_bar.cancel_button.click()
    assert not ml.select_mode and all(not r.is_checked() and not r.check.isVisibleTo(r) for r in ml.rows)


def test_ctrl_a_on_a_row_starts_selecting(shell, meetings_root, asked):
    _three(meetings_root)
    win, _app = shell("pro")
    win.show()
    page, ml = _list(win)
    ml.rows[1].setFocus(Qt.TabFocusReason)
    QTest.keyClick(ml.rows[1], Qt.Key_A, Qt.ControlModifier)
    assert ml.select_mode and ml.select_bar.message.text() == "3 selected"


def test_one_question_for_the_whole_batch_and_one_undo(shell, meetings_root, asked):
    a, b, c = _three(meetings_root)
    win, _app = shell("pro")
    win.show()
    page, ml = _list(win)
    ml.enter_select()
    QTest.mouseClick(ml.rows[0], Qt.LeftButton)
    QTest.mouseClick(ml.rows[1], Qt.LeftButton)
    ml.select_bar.delete_button.click()
    assert len(asked) == 1                                                # one question, not two
    q = asked[0]
    assert q["title"] == "Delete 2 meetings?"
    assert q["text"].startswith("Their notes, transcripts, recordings and speaker names are removed from this computer.")
    assert "Together 1 h 02 min" not in q["text"] and "Together" in q["text"]
    assert q["detail"] == "“Q4 roadmap” and “Weekly product sync”"
    assert _titles(ml) == ["Design review"] and not ml.select_mode        # done: back to a calm list
    assert not a.exists() and not b.exists() and c.is_dir()
    assert ml.deleter.bar.message.text() == "Deleted 2 meetings."
    ml.deleter.bar.undo_button.click()
    assert _wait_for(lambda: len(ml.rows) == 3) and a.is_dir() and b.is_dir()


def test_a_batch_of_many_names_three_and_counts_the_rest(shell, meetings_root, asked):
    for i in range(6):
        _meeting(meetings_root, "2026092%d_110000" % (i + 1), "Meeting %d" % i)
    win, _app = shell("pro")
    page, ml = _list(win)
    ml.select_all()
    asked.answers.append(False)
    ml.select_bar.delete_button.click()
    assert asked[0]["title"] == "Delete 6 meetings?"
    assert asked[0]["detail"].endswith(" and 3 more") and asked[0]["detail"].count("“") == 3
    assert ml.select_mode and ml.select_bar.message.text() == "6 selected"    # cancelled: still selecting


def test_escape_leaves_selecting(shell, meetings_root, asked):
    _three(meetings_root)
    win, _app = shell("pro")
    win.show()
    page, ml = _list(win)
    ml.enter_select()
    ml.rows[0].setFocus(Qt.TabFocusReason)
    QTest.keyClick(ml.rows[0], Qt.Key_Escape)
    assert not ml.select_mode and ml.select_button.isVisibleTo(ml) and not ml.select_bar.isVisibleTo(ml)


# ═══════════════════════════ the keyboard ═══════════════════════════
def test_the_delete_key_on_a_row_asks_the_same_question(shell, meetings_root, asked):
    a, b, c = _three(meetings_root)
    win, _app = shell("pro")
    win.show()
    page, ml = _list(win)
    ml.rows[2].setFocus(Qt.TabFocusReason)
    QTest.keyClick(ml.rows[2], Qt.Key_Delete)
    assert len(asked) == 1 and asked[0]["title"] == "Delete “Design review”?"
    assert not c.exists() and a.is_dir() and b.is_dir()
    assert page.current_view() == "list"
    assert [c for c in _app.calls if c[:2] == ("track", "meeting_deleted")][0][2]["from"] == "key"


def test_after_a_delete_the_keyboard_moves_to_the_next_row(shell, meetings_root, asked):
    """Deleting the row the keyboard is on must not drop the focus into the void (Tab would start again
    from the top of the window): it goes to the next meeting - or the one before, for the last."""
    for i in range(4):
        _meeting(meetings_root, "2026092%d_110000" % (i + 1), "Meeting %d" % i)
    win, _app = shell("pro")
    win.show()
    page, ml = _list(win)
    assert _titles(ml) == ["Meeting 3", "Meeting 2", "Meeting 1", "Meeting 0"]
    ml.rows[1].setFocus(Qt.TabFocusReason)
    QTest.keyClick(ml.rows[1], Qt.Key_Delete)
    assert _titles(ml) == ["Meeting 3", "Meeting 1", "Meeting 0"]
    _spin(20)
    assert ml.rows[1].hasFocus() and ml.rows[1].meeting["title"] == "Meeting 1"      # the next one
    assert ml.rows[1].property("focusVisible")                                        # with its ring, for the keyboard
    ml.rows[2].setFocus(Qt.TabFocusReason)
    QTest.keyClick(ml.rows[2], Qt.Key_Delete)
    _spin(20)
    assert _titles(ml) == ["Meeting 3", "Meeting 1"] and ml.rows[1].hasFocus()        # the last one: the one before
    ml.rows[1].setFocus(Qt.TabFocusReason)
    QTest.keyClick(ml.rows[1], Qt.Key_Delete)
    QTest.keyClick(ml.rows[0], Qt.Key_Delete)                                          # (the only one left)
    assert ml.rows == [] and ml.example.isVisibleTo(ml)


def test_a_meeting_that_could_not_be_deleted_gets_the_keyboard_back(shell, meetings_root, asked, monkeypatch):
    import meeting_trash
    from ui.pages import meeting_delete
    monkeypatch.setattr(meeting_delete, "RETRY_WAIT_S", 0)
    a, b, c = _three(meetings_root)
    monkeypatch.setattr(meeting_trash, "move_to_trash",
                        lambda f, now=None: (_ for _ in ()).throw(meeting_trash.TrashError("busy", "in use")))
    win, _app = shell("pro")
    win.show()
    page, ml = _list(win)
    ml.rows[1].setFocus(Qt.TabFocusReason)
    QTest.keyClick(ml.rows[1], Qt.Key_Delete)
    _spin(20)
    assert len(ml.rows) == 3 and ml.rows[1].hasFocus() and ml.rows[1].isEnabled()


def test_the_delete_key_while_selecting_deletes_the_ticked_ones(shell, meetings_root, asked):
    a, b, c = _three(meetings_root)
    win, _app = shell("pro")
    win.show()
    page, ml = _list(win)
    ml.enter_select()
    QTest.mouseClick(ml.rows[0], Qt.LeftButton)
    QTest.mouseClick(ml.rows[1], Qt.LeftButton)
    ml.rows[2].setFocus(Qt.TabFocusReason)
    QTest.keyClick(ml.rows[2], Qt.Key_Delete)
    assert asked[0]["title"] == "Delete 2 meetings?"
    assert c.is_dir() and not a.exists() and not b.exists()


def test_enter_still_opens_a_meeting(shell, meetings_root, asked):
    _three(meetings_root)
    win, _app = shell("pro")
    win.show()
    page, ml = _list(win)
    ml.rows[0].setFocus(Qt.TabFocusReason)
    QTest.keyClick(ml.rows[0], Qt.Key_Return)
    assert page.current_view() == "notes" and asked == []


# ═══════════════════════════ what can't go ═══════════════════════════
def test_a_meeting_being_recorded_cannot_be_deleted(shell, meetings_root, asked):
    a, b, c = _three(meetings_root)
    session = Session("recording")
    session.meeting_dir = str(a)
    win, _app = shell("pro", meeting_session=session)
    win.show()
    page, ml = _list(win)
    row = ml.row_for(str(a))
    assert row.blocked() == "Recording now" and not row.trash.isEnabled()
    assert row.trash.toolTip() == "Recording now" and row.trash.accessibleName() == "Delete Q4 roadmap"
    assert ml.rows[1].blocked() == "" and ml.rows[1].trash.isEnabled()
    ml.rows[0].trash.click()                                              # disabled: nothing to press
    assert asked == []
    ml.select_all()
    assert row.is_checked() is False and ml.select_bar.message.text() == "2 selected"   # not even selectable
    assert ml.ask_delete([row.meeting], "row") is False and asked == []
    assert ml.deleter.bar.isVisibleTo(ml) and "still being worked on" in ml.deleter.bar.message.text()
    assert a.is_dir()


def test_pressing_the_greyed_icon_does_not_open_the_meeting_either(shell, meetings_root, asked):
    """Spec: clicking the trash icon NEVER opens the meeting - also while it is greyed (a meeting that is
    being recorded). A disabled QAbstractButton swallows mouse events (no click-through to the row); this
    pins that, so a change of the icon's class or of its enabled state cannot bring the click-through back."""
    a, b, c = _three(meetings_root)
    session = Session("recording")
    session.meeting_dir = str(a)
    win, _app = shell("pro", meeting_session=session)
    win.show()
    page, ml = _list(win)
    page.show_list()                                                      # (a recording opens the live view by itself)
    _spin(30)
    row = ml.row_for(str(a))
    _enter(row)
    assert row.trash.isVisible() and not row.trash.isEnabled()
    QTest.mouseClick(row.trash, Qt.LeftButton)
    _spin(30)
    assert page.current_view() == "list" and asked == []                  # nothing opened, nothing asked
    other = ml.rows[1]
    _enter(other)
    QTest.mouseClick(other, Qt.LeftButton, pos=other.rect().center())      # the row itself still opens
    assert page.current_view() == "notes"


def test_notes_being_written_or_names_being_added_say_finishing_up(shell, meetings_root, asked):
    a, b, c = _three(meetings_root)
    session = Session("processing")
    session.meeting_dir = str(a)
    win, _app = shell("pro", meeting_session=session)
    win.show()
    page, ml = _list(win)
    assert ml.row_for(str(a)).trash.toolTip() == "Finishing up"
    session.phase = "idle"
    page.list_view.sync_session()
    assert ml.row_for(str(a)).blocked() == ""
    session.pass_state = "running"                                        # the speaker names, in the background
    session.speakers_changed = type("S", (), {"emit": staticmethod(lambda f: None)})
    page._on_speakers_changed(str(b))
    assert all(r.blocked() == "Finishing up" for r in ml.rows)            # it is the session's pass
    session.pass_state = ""
    page._on_speakers_changed(str(b))
    assert all(r.blocked() == "" for r in ml.rows)


# ═══════════════════════════ the work happens off the window's thread ═══════════════════════════
def test_deleting_runs_on_a_worker_the_row_dims_and_the_window_stays_alive(shell, meetings_root, asked, monkeypatch):
    from ui.pages import meeting_views
    import meeting_trash
    a, b, c = _three(meetings_root)
    release = threading.Event()
    started = threading.Event()
    seen = {}
    real = meeting_trash.move_to_trash

    def slow(folder, now=None):
        seen["thread"] = threading.current_thread()
        started.set()
        assert release.wait(5)
        return real(folder, now)
    monkeypatch.setattr(meeting_trash, "move_to_trash", slow)
    win, _app = shell("pro")
    win.show()
    page, ml = _list(win)
    monkeypatch.setattr(meeting_views, "SYNC_BG", False)                  # from here the work really leaves the thread
    ml.rows[1].trash.click()                                              # returns at once
    assert started.wait(5) and seen["thread"] is not threading.current_thread()
    _spin(30)
    row = ml.row_for(str(b))
    assert row is not None and row.is_deleting() and row.deleting_label.isVisibleTo(row)
    assert row.deleting_label.text() == "Deleting…" and not row.isEnabled()
    assert ml.rows[0].isEnabled() and not ml.rows[0].is_deleting()        # only that row
    QTest.mouseClick(row, Qt.LeftButton)
    assert page.current_view() == "list"                                  # a row on its way can't be opened
    assert b.is_dir()                                                     # still there while the work waits
    release.set()
    assert _wait_for(lambda: _titles(ml) == ["Q4 roadmap", "Design review"])
    assert not b.exists() and _wait_for(lambda: ml.deleter.bar.isVisibleTo(ml))


def test_a_meeting_that_cannot_be_removed_stays_and_the_others_go(shell, meetings_root, asked, monkeypatch):
    import meeting_trash
    a, b, c = _three(meetings_root)
    real = meeting_trash.move_to_trash

    def picky(folder, now=None):
        if str(folder).endswith("20260928_100000"):
            raise meeting_trash.TrashError("busy", "in use")
        return real(folder, now)
    monkeypatch.setattr(meeting_trash, "move_to_trash", picky)
    win, _app = shell("pro")
    win.show()
    page, ml = _list(win)
    ml.select_all()
    ml.select_bar.delete_button.click()
    assert _titles(ml) == ["Weekly product sync"]                          # kept, and back to normal
    row = ml.rows[0]
    assert not row.is_deleting() and row.isEnabled() and row.deleting_label.isHidden()
    assert b.is_dir() and not a.exists() and not c.exists()
    bar = ml.deleter.bar
    assert bar.property("tone") == "error"
    assert bar.message.text() == ("Deleted 2 meetings. “Weekly product sync” is open in another program. "
                                  "Close it and try again.")
    bar.undo_button.click()
    assert _wait_for(lambda: len(ml.rows) == 3)


def test_the_error_words_for_a_folder_with_no_permission_and_one_outside(shell, meetings_root, asked, monkeypatch,
                                                                         tmp_path):
    import meeting_trash
    a, b, c = _three(meetings_root)
    outside = tmp_path / "important"
    outside.mkdir()
    (outside / "keep.txt").write_text("mine", encoding="utf-8")
    win, _app = shell("pro")
    page, ml = _list(win)
    ml.deleter.delete([{"dir": str(outside), "title": "Not a meeting"}], "row")      # path safety, end to end
    assert (outside / "keep.txt").read_text(encoding="utf-8") == "mine"
    assert ml.deleter.bar.message.text() == "“Not a meeting” isn't in Gusan's meetings folder, so it was left alone."
    monkeypatch.setattr(meeting_trash, "move_to_trash",
                        lambda f, now=None: (_ for _ in ()).throw(meeting_trash.TrashError("denied")))
    ml.deleter.delete([ml.rows[0].meeting], "row")
    assert ml.deleter.bar.message.text() == (
        "Gusan isn't allowed to remove “Q4 roadmap”. It may be read-only or open in another program.")
    assert a.is_dir()


def test_a_loaded_recording_is_let_go_before_its_folder_moves(shell, meetings_root, asked):
    a, b, c = _three(meetings_root)
    win, _app = shell("pro")
    win.show()
    page, ml = _list(win)
    nv = page.notes_view
    nv.folder = str(a)
    released = []
    nv.audio_player.stop = lambda release=False: released.append(release)
    ml.rows[0].trash.click()
    assert released == [True]                                             # the player let go of the file first
    assert nv.folder is None                                              # and the notes view forgot the meeting


def test_delete_everything_in_settings_also_empties_what_waits_for_undo(shell, meetings_root, asked):
    import meeting_store
    a, b, c = _three(meetings_root)
    win, _app = shell("pro")
    page, ml = _list(win)
    ml.rows[0].trash.click()
    assert (meetings_root / ".trash").is_dir()
    meeting_store.delete_all_meetings()
    assert list(meetings_root.iterdir()) == []


def test_quitting_deletes_what_waits(shell, meetings_root, asked):
    a, b, c = _three(meetings_root)
    win, _app = shell("pro")
    page, ml = _list(win)
    ml.rows[0].trash.click()
    ml.deleter._on_quit()                                                 # aboutToQuit
    assert not (meetings_root / ".trash").exists()


# ═══════════════════════════ the list stays true to the disk ═══════════════════════════
def test_a_list_read_that_began_before_the_delete_does_not_bring_it_back(shell, meetings_root, asked):
    """The page re-reads the library on a worker thread (after a rename, a calendar change, a finished
    meeting). A read that started before the folder moved can arrive after it: it must not put the
    meeting back on screen - until an Undo has really brought it back."""
    a, b, c = _three(meetings_root)
    win, _app = shell("pro")
    win.show()
    page, ml = _list(win)
    stale = page.list_view.items()                                        # what that earlier read held
    assert len(stale) == 3
    ml.rows[1].trash.click()
    assert _titles(ml) == ["Q4 roadmap", "Design review"]
    page.list_view._on_items(stale, None)                                 # ...and arrives now
    assert _titles(ml) == ["Q4 roadmap", "Design review"] and not b.exists()
    ml.deleter.bar.undo_button.click()                                    # Undo lifts the filter for that one
    assert _wait_for(lambda: len(ml.rows) == 3)
    page.list_view._on_items(stale, None)
    assert _titles(ml) == ["Q4 roadmap", "Weekly product sync", "Design review"]


def test_a_row_that_is_not_built_yet_is_not_built_once_its_folder_is_gone(shell, meetings_root, asked, monkeypatch):
    from ui.pages import meetings as page_mod
    for i in range(6):
        _meeting(meetings_root, "2026092%d_110000" % (i + 1), "Meeting %d" % i)
    monkeypatch.setattr(page_mod.MeetingsList, "FIRST_ROWS", 2)           # the rest comes in slices
    monkeypatch.setattr(page_mod.MeetingsList, "SLICE_S", 0.0)            # (one row each)
    win, _app = shell("pro")
    win.show()
    page, ml = _list(win)
    ml.set_items(page.list_view.items())
    assert len(ml.rows) == 2 and ml._row_state is not None                # still being built
    late = ml._shown[4]                                                   # a meeting whose row does not exist yet
    ml.deleter.delete([late], "row")
    assert _wait_for(lambda: ml._row_state is None)
    assert len(ml.rows) == 5 and late["title"] not in _titles(ml)
    assert [r._first for r in ml.rows] == [True, False, False, False, False]
    assert not os.path.isdir(late["dir"])


def test_the_ticks_survive_a_refresh_and_a_vanished_meeting_is_unticked(shell, meetings_root, asked):
    """The list is rebuilt after a rename, a calendar change, a finished meeting: what was ticked stays
    ticked (and Select stays on); a meeting that went from the disk meanwhile is no longer counted."""
    import shutil
    a, b, c = _three(meetings_root)
    win, _app = shell("pro")
    win.show()
    page, ml = _list(win)
    ml.enter_select()
    QTest.mouseClick(ml.rows[0], Qt.LeftButton)
    QTest.mouseClick(ml.rows[1], Qt.LeftButton)
    page.list_view.refresh()                                              # a re-render
    assert ml.select_mode and [r.is_checked() for r in ml.rows] == [True, True, False]
    assert ml.select_bar.message.text() == "2 selected" and all(r.check.isVisibleTo(r) for r in ml.rows)
    shutil.rmtree(str(b))                                                 # one of the ticked ones is gone
    page.list_view.refresh()
    assert _titles(ml) == ["Q4 roadmap", "Design review"] and ml.select_bar.message.text() == "1 selected"
    assert [r.is_checked() for r in ml.rows] == [True, False]


def test_the_selected_count_is_what_delete_would_delete(shell, meetings_root, asked):
    _three(meetings_root)
    win, _app = shell("pro")
    win.show()
    page, ml = _list(win)
    ml.select_all()
    assert ml.select_bar.message.text() == "3 selected"
    ml.search.setText("Design")                                           # a search hides two of the ticked ones
    ml._run_search()
    assert _titles(ml) == ["Design review"]
    assert ml.select_bar.message.text() == "1 selected"
    asked.answers.append(False)
    ml.select_bar.delete_button.click()
    assert asked[0]["title"] == "Delete “Design review”?"                  # the question agrees with the bar
    ml.search.setText("")
    ml._run_search()
    assert ml.select_bar.message.text() == "3 selected"                   # the others were still ticked


def test_the_open_meeting_that_was_deleted_elsewhere_is_not_shown_as_an_empty_meeting(shell, meetings_root, asked):
    """Meetings had a meeting open, the person went to the Live Assistant page and deleted that
    session from its list; coming back must show the list, not a blank meeting that can't be saved."""
    import shutil
    a, b, c = _three(meetings_root)
    win, _app = shell("pro")
    page = _open(win, folder=str(a))
    assert page.current_view() == "notes" and page.notes_view.folder == str(a)
    shutil.rmtree(str(a))
    page.on_open()                                                        # the page comes back to the front
    assert page.current_view() == "list" and page.notes_view.folder is None
    assert _wait_for(lambda: "Q4 roadmap" not in _titles(page.list_view.meetings))


def test_a_folder_a_program_is_about_to_let_go_of_is_tried_again(shell, meetings_root, asked, monkeypatch):
    """The audio player (or a virus scanner) can still hold the recording for a moment after the question
    was answered: Windows then refuses to rename the folder. Two more tries, then the plain error."""
    import meeting_trash
    from ui.pages import meeting_delete
    monkeypatch.setattr(meeting_delete, "RETRY_WAIT_S", 0)
    a, b, c = _three(meetings_root)
    real = meeting_trash.move_to_trash
    calls = []

    def flaky(folder, now=None):
        calls.append(str(folder))
        if len(calls) < 3:
            e = PermissionError(13, "Access is denied")
            e.winerror = 5
            raise meeting_trash.TrashError("denied", str(e))
        return real(folder, now)
    monkeypatch.setattr(meeting_trash, "move_to_trash", flaky)
    win, _app = shell("pro")
    win.show()
    page, ml = _list(win)
    ml.rows[0].trash.click()
    assert len(calls) == 3 and not a.exists()                             # the third try went through
    assert ml.deleter.bar.message.text() == "Deleted 1 meeting." and ml.deleter.bar.property("tone") == "info"


def test_a_real_refusal_waits_once_for_the_whole_batch_not_for_every_folder(shell, meetings_root, asked, monkeypatch):
    import meeting_trash
    from ui.pages import meeting_delete
    monkeypatch.setattr(meeting_delete, "RETRY_WAIT_S", 0)
    a, b, c = _three(meetings_root)
    calls = []

    def refuse(folder, now=None):
        calls.append(str(folder))
        raise meeting_trash.TrashError("denied", "no")
    monkeypatch.setattr(meeting_trash, "move_to_trash", refuse)
    win, _app = shell("pro")
    win.show()
    page, ml = _list(win)
    ml.select_all()
    ml.select_bar.delete_button.click()
    assert len(calls) == (meeting_delete.RETRIES + 1) + 1 + 1             # the first folder tried 3 times, the others once
    assert a.is_dir() and b.is_dir() and c.is_dir() and len(ml.rows) == 3
    assert "isn't allowed to remove" in ml.deleter.bar.message.text()
    assert "2 more couldn't be deleted either." in ml.deleter.bar.message.text()


def test_a_very_long_name_is_cut_in_the_question_and_in_the_bar(shell, meetings_root, asked):
    from ui.pages import meeting_delete
    long_name = "Quarterly planning " + "x" * 300
    m = {"dir": "x", "title": long_name, "duration_sec": 600, "started": "2026-09-29 11:00:00"}
    title, _text, _detail = meeting_delete.words([m])
    assert title.startswith("Delete “Quarterly planning ") and title.endswith("…”?") and len(title) < 100
    _t, _x, detail = meeting_delete.words([m, dict(m, title="Short"), dict(m, title="Other")])
    assert "…" in detail and len(detail) < 130
    short = meeting_delete.words([dict(m, title="Q4 roadmap")])[0]
    assert short == "Delete “Q4 roadmap”?"                                # a normal name is not touched


# ═══════════════════════════ the detail view uses the same question ═══════════════════════════
def test_the_open_meetings_delete_is_counted_like_the_lists(shell, meetings_root, asked):
    a, b, c = _three(meetings_root)
    win, app = shell("pro")
    page = _open(win, folder=str(a))
    page.notes_view.delete()
    assert ("track", "meeting_deleted", {"count": 1, "from": "notes"}) in app.calls


def test_the_open_meeting_asks_the_same_question_and_goes_through_the_trash_with_undo(shell, meetings_root, asked):
    a, b, c = _three(meetings_root)
    win, _app = shell("pro")
    win.show()
    page = _open(win, folder=str(a))
    nv = page.notes_view
    nv.delete()
    assert asked[0]["title"] == "Delete “Q4 roadmap”?" and asked[0]["danger"] is True
    ml = page.list_view.meetings
    assert not a.exists() and page.current_view() == "list"
    assert (meetings_root / ".trash").is_dir()                            # moved, not removed: like the list's Delete
    bar = ml.deleter.bar
    assert bar.isVisibleTo(page) and bar.message.text() == "Deleted 1 meeting."
    assert bar.undo_button.isVisibleTo(bar)                               # "Deleted 1 meeting. Undo" for ten seconds
    assert _titles(ml) == ["Weekly product sync", "Design review"]
    bar.undo_button.click()
    assert _wait_for(lambda: _titles(ml) == ["Q4 roadmap", "Weekly product sync", "Design review"])
    assert a.is_dir() and (a / "notes.md").read_text(encoding="utf-8") == NOTES      # exactly as it was
    assert not (meetings_root / ".trash").exists()


def test_the_open_meeting_is_removed_for_good_once_the_bar_closes(shell, meetings_root, asked):
    a, _b, _c = _three(meetings_root)
    win, _app = shell("pro")
    win.show()
    page = _open(win, folder=str(a))
    page.notes_view.delete()
    d = page.list_view.meetings.deleter
    assert len(d.pending()) == 1
    d._timer.stop()
    d.dismiss()                                                           # what the timer (or the x) does
    assert d.pending() == [] and not (meetings_root / ".trash").exists() and not a.exists()


def test_the_open_meeting_with_a_names_pass_running_stops_it_and_still_goes(shell, meetings_root, asked):
    from types import SimpleNamespace
    a, _b, _c = _three(meetings_root)
    win, app = shell("pro")
    win.show()
    stopped = []
    app.meeting_session = SimpleNamespace(
        active_dir=lambda: None, phase="idle", speakers_state=lambda folder: "running",
        stop_speaker_pass=lambda folder, timeout=8.0: stopped.append(str(folder)) or True)
    page = _open(win, folder=str(a))
    page.notes_view.delete()
    assert stopped == [str(a)]                                            # asked while the folder was still there
    assert not a.exists() and (meetings_root / ".trash").is_dir()         # and then it moved to the trash


def test_rename_asks_in_the_app_s_own_card(shell, meetings_root, monkeypatch):
    from ui.components import mini_prompt
    a, _b, _c = _three(meetings_root)
    win, _app = shell("pro")
    page = _open(win, folder=str(a))
    seen = []
    monkeypatch.setattr(mini_prompt, "ask_text", lambda parent, title, label, text="", **kw:
                        seen.append((title, label, text)) or "Roadmap review")
    page.notes_view.rename()
    assert seen == [("Rename meeting", "Name", "Q4 roadmap")]
    assert page.notes_view.title_label.text() == "Roadmap review"
    monkeypatch.setattr(mini_prompt, "ask_text", lambda *a, **k: None)           # cancelled
    page.notes_view.rename()
    assert page.notes_view.title_label.text() == "Roadmap review"


# ═══════════════════════════ pictures, light and dark ═══════════════════════════
def _shot(widget, name, scheme):
    if not SHOTS:
        return
    os.makedirs(SHOTS, exist_ok=True)
    assert widget.grab().save(os.path.join(SHOTS, "meetings_delete_%s_%s.png" % (name, scheme)))


def test_the_list_in_every_state(scheme, shell, meetings_root, monkeypatch):
    """The list as a person sees it - on a light and on a dark OS (the pictures are the same: the
    app is light either way) - and the card that asks."""
    import ui.winfit
    from ui import shell as shell_mod
    from ui.components import mini_prompt, row_actions
    from ui.pages import meeting_views
    import meeting_trash
    monkeypatch.setattr(row_actions, "ANIMATE", False)
    monkeypatch.setattr(ui.winfit, "settle_on_screen", lambda *a, **k: None)     # the offscreen screen is 800x600
    monkeypatch.setattr(ui.winfit, "fit_on_screen", lambda *a, **k: None)
    for i, (name, title, dur) in enumerate((("20260929_110000", "Q4 roadmap", 2520),
                                            ("20260928_100000", "Weekly product sync", 1800),
                                            ("20260927_090000", "Design review", 3600),
                                            ("20260926_150000", "Hiring plan with a very long name that has to be cut "
                                                                "at the end of the line", 900),
                                            ("20260925_150000", "Customer call", 600))):
        _meeting(meetings_root, name, title, duration_sec=dur)
    win, _app = shell("pro")
    win.resize(*shell_mod.DEFAULT_SIZE)
    win.show()
    page, ml = _list(win)
    holder = page._holders["list"]
    holder.verticalScrollBar().setValue(holder.verticalScrollBar().maximum())
    _spin(50)

    def pic(name):
        _spin(30)
        _shot(win, name, scheme)

    pic("1_quiet")
    _enter(ml.rows[1])
    pic("2_hover")
    assert ml.rows[1].trash.shown_opacity() == 1.0
    _leave(ml.rows[1])
    ml.rows[3].setFocus(Qt.TabFocusReason)
    pic("3_keyboard_focus")
    ml.enter_select()
    for r in ml.rows[:3]:
        QTest.mouseClick(r, Qt.LeftButton)
    assert ml.select_bar.message.text() == "3 selected"
    pic("4_selected_3")
    # the question
    seen = {}

    def exec_(self):
        self.show()
        QApplication.processEvents()
        _shot(self, "5_confirm", scheme)
        seen["dlg"] = self
        return 0
    monkeypatch.setattr(mini_prompt.MiniPrompt, "exec", exec_)
    ml.select_bar.delete_button.click()
    assert "dlg" in seen and seen["dlg"].tone == "danger" and seen["dlg"].no_button.isDefault()
    seen["dlg"].close()
    # deleting: a row on its way
    monkeypatch.setattr(meeting_views, "SYNC_BG", False)
    release = threading.Event()
    real = meeting_trash.move_to_trash

    def slow(folder, now=None):
        assert release.wait(5)
        return real(folder, now)
    monkeypatch.setattr(meeting_trash, "move_to_trash", slow)
    monkeypatch.setattr(mini_prompt, "confirm", lambda *a, **k: True)
    ml.selected = {m["dir"] for m in ml._shown[:2]}
    ml.delete_selected()
    assert _wait_for(lambda: any(r.is_deleting() for r in ml.rows))
    pic("6_deleting")
    release.set()
    assert _wait_for(lambda: len(ml.rows) == 3 and ml.deleter.bar.isVisibleTo(ml))
    pic("7_undo_bar")
    for r in ml.rows:
        assert not r.is_deleting()
    ml.deleter.bar.undo_button.click()
    assert _wait_for(lambda: len(ml.rows) == 5)
