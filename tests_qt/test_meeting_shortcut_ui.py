"""Two founder asks (2026-11-21), offscreen:

1. A long meeting name is never cut at its START ("European AI Championship Paris" read "opean AI
   Championship Paris" next to the pencil): the start card shows the beginning and "…" (the whole name in
   the tooltip), the pencil / Tab edit it from its first letter, and the open meeting's and the live view's
   headers, the list rows and Coming up end in "…" too.
2. The meeting shortcut (Alt+M): its keycaps under Record and on Home's "Record a meeting" tile, the rail's
   "Shortcut" row that changes it, Settings' row that links there, and a press (main._on_meeting_hotkey)
   doing exactly what Record does - or opening the meeting that is recording, never stopping it."""
import json

import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from tests_qt.conftest import make_stub_app
from tests_qt.test_page_meetings import FakeSession

LONG = "European AI Championship Paris"
LONGER = "European AI Championship Paris - final round with the jury, the partners, the press and the sponsors"


@pytest.fixture(autouse=True)
def _inline_bg(monkeypatch):
    from ui.pages import meeting_views
    monkeypatch.setattr(meeting_views, "SYNC_BG", True)


@pytest.fixture
def meetings_root(tmp_path, monkeypatch):
    import storage
    root = tmp_path / "meetings"
    root.mkdir()
    real = storage.path_for
    monkeypatch.setattr(storage, "path_for", lambda name: root if name == "meetings" else real(name))
    return root


@pytest.fixture
def shell(qapp, monkeypatch):
    from ui import shell as shell_mod
    from ui.pages import meetings as page_mod
    monkeypatch.setattr(page_mod, "_ensure_consent", lambda parent, app: True)
    made = []

    def build(tier="pro", width=1100, **attrs):
        app = make_stub_app(tier)
        app.cfg["meeting_hotkey"] = "alt+m"
        app.suspend_hotkeys = lambda on: app.calls.append(("suspend_hotkeys", on))
        for k, v in attrs.items():
            setattr(app, k, v)
        win = shell_mod.MainWindow(app)
        win.resize(width, 820)
        win.show()
        win.activateWindow()
        made.append(win)
        QApplication.processEvents()
        return win, app

    yield build
    for w in made:
        w.hide()
        w.deleteLater()
    QApplication.processEvents()


def _spin(n=4):
    for _ in range(n):
        QApplication.processEvents()


def _meetings(win, **opts):
    page = win.open("meetings", **opts)
    _spin()
    return page


# ── 1. a long name ───────────────────────────────────────────────────────────
@pytest.mark.parametrize("title", [LONG, LONGER])
def test_a_long_calendar_name_shows_its_beginning(shell, meetings_root, title):
    win, _app = shell("pro", width=1000)
    card = _meetings(win).list_view.start_card
    card.set_calendar_meeting({"id": "e1", "title": title, "others": ["Anna"], "start": None})
    _spin()
    edit = card.title_edit
    assert edit.text() == title                                  # the whole name is kept
    assert edit.shown_text().startswith("European")              # its beginning on screen, never its end
    if title == LONGER:
        assert edit.is_elided()
        assert edit.shown_text().endswith("…") and len(edit.shown_text()) < len(title)
        assert title in edit.toolTip()                           # the whole name on hover
        assert edit.width() < edit.fontMetrics().horizontalAdvance(title)
    else:
        assert edit.shown_text() in (title, edit.fontMetrics().elidedText(title, Qt.ElideRight,
                                                                          edit._text_rect().width()))


def test_a_short_name_has_no_dots_and_no_tooltip(shell, meetings_root):
    win, _app = shell("pro")
    edit = _meetings(win).list_view.start_card.title_edit
    edit.setText("Standup")
    _spin()
    assert not edit.is_elided() and edit.shown_text() == "Standup" and edit.toolTip() == ""


def test_the_pencil_edits_the_whole_name_from_its_start(shell, meetings_root):
    session = FakeSession()
    win, _app = shell("pro", width=1000, meeting_session=session)
    page = _meetings(win)
    card = page.list_view.start_card
    card.set_calendar_meeting({"id": "e1", "title": LONGER, "others": ["Anna"], "start": None})
    _spin()
    edit = card.title_edit
    card.pen_btn.click()
    _spin()
    assert edit.hasFocus()
    assert not edit.is_elided() and edit.toolTip() == ""          # editing: the real text, no "…"
    assert edit.selectedText() == LONGER                         # all of it, typing replaces it...
    assert edit.cursorPosition() == 0                            # ...shown from the first letter
    assert edit.cursorRect().left() < 12
    QTest.keyClicks(edit, "Board sync")
    assert edit.text() == "Board sync"
    edit.setCursorPosition(0)
    QTest.keyClick(edit, Qt.Key_End)
    QTest.keyClicks(edit, " - Paris")
    assert edit.text() == "Board sync - Paris"
    card.record_btn.setFocus()
    _spin()
    card.record_btn.click()
    assert session.calls[0][1]["title"] == "Board sync - Paris"  # the edited name records


def test_a_click_on_the_shown_beginning_lands_there(shell, meetings_root):
    win, _app = shell("pro", width=1000)
    card = _meetings(win).list_view.start_card
    card.set_calendar_meeting({"id": "e1", "title": LONGER, "others": [], "start": None})
    edit = card.title_edit
    card.pen_btn.click()
    QTest.keyClick(edit, Qt.Key_End)                             # edited at its end: QLineEdit scrolled there
    _spin()
    card.record_btn.setFocus()
    _spin()
    assert edit.is_elided()
    x = edit._text_rect().left() + edit.fontMetrics().horizontalAdvance("Europ")
    QTest.mouseClick(edit, Qt.LeftButton, Qt.NoModifier, QPoint(x, edit.height() // 2))
    _spin()
    assert edit.hasFocus() and 4 <= edit.cursorPosition() <= 6   # by the "p" of "European", where it was shown
    QTest.keyClicks(edit, "X")
    assert edit.text().startswith("Europ") and "X" in edit.text()[4:7]


def test_tab_into_a_long_name_starts_at_its_beginning(shell, meetings_root):
    win, _app = shell("pro", width=1000)
    card = _meetings(win).list_view.start_card
    card.set_calendar_meeting({"id": "e1", "title": LONGER, "others": [], "start": None})
    edit = card.title_edit
    edit.setFocus(Qt.TabFocusReason)                             # Tab selects all, with the cursor at the END
    _spin()
    assert edit.selectedText() == LONGER and edit.cursorPosition() == 0
    card.record_btn.setFocus()
    _spin()
    assert edit.is_elided() and edit.shown_text().startswith("European")


def _saved(root, title):
    d = root / "20261101_100000"
    d.mkdir()
    (d / "meta.json").write_text(json.dumps({"title": title, "duration_sec": 600, "started": "2026-11-01 10:00:00",
                                             "seen": True, "attendees": "Anna"}), encoding="utf-8")
    (d / "notes.md").write_text("## Summary\nA long one.", encoding="utf-8")
    return d


def test_the_open_meeting_and_its_row_end_in_dots_not_wider_than_the_window(shell, meetings_root):
    folder = _saved(meetings_root, LONGER)
    win, _app = shell("pro", width=1000)
    page = _meetings(win)
    row = page.list_view.meetings.rows[0]
    assert row.title_label.full_text() == LONGER and row.title_label.text().startswith("European")
    page.open_notes(str(folder))
    _spin(6)
    title = page.notes_view.title_label
    assert title.text() == LONGER                                # the whole name (exports, rename, tests)
    assert title.is_elided() and title.shown_text().startswith("European") and title.shown_text().endswith("…")
    assert LONGER in title.toolTip()
    # it never makes the page wider than its words would (the toolbar's right end went off the window)
    assert title.minimumSizeHint().width() < 100
    assert title.width() < title.fontMetrics().horizontalAdvance(LONGER)


def test_the_live_view_header_ends_in_dots(shell, meetings_root):
    session = FakeSession("recording")
    session.title = LONGER
    win, _app = shell("pro", width=1000, meeting_session=session)
    page = _meetings(win)
    assert page.current_view() == "live"
    _spin(4)
    head = page.live_view.header.title_label
    assert head.text() == LONGER and head.accessibleName() == LONGER
    assert head.is_elided() and head.shown_text().startswith("European")
    assert head.minimumSizeHint().width() < 100
    assert head.width() < head.fontMetrics().horizontalAdvance(LONGER)


# ── 2. the meeting shortcut ──────────────────────────────────────────────────
def _keys(hk):
    from ui.components.controls import keycap_labels
    return " + ".join(keycap_labels(hk))


def test_keycaps_under_record_follow_the_shortcut(shell, meetings_root):
    win, app = shell("free")
    page = _meetings(win)
    card = page.list_view.start_card
    assert card.record_keys.accessibleName() == _keys("alt+m") and card.record_keys.isVisibleTo(card)
    assert _keys("alt+m") in card.record_btn.toolTip()
    app.cfg["meeting_hotkey"] = "ctrl+shift+m"
    page.on_setting_changed("meeting_hotkey")
    assert card.record_keys.accessibleName() == _keys("ctrl+shift+m")
    assert page.list_view.rail.shortcut.hotkey() == "ctrl+shift+m"
    app.cfg["meeting_hotkey"] = ""                               # none set: no keycaps
    page.on_setting_changed("meeting_hotkey")
    assert not card.record_keys.isVisibleTo(card)
    assert page.list_view.rail.shortcut.none_label.isVisibleTo(page.list_view.rail.shortcut)


def test_the_rail_shortcut_row_changes_it(shell, meetings_root):
    win, app = shell("free")
    rail = _meetings(win).list_view.rail
    sc = rail.shortcut
    assert sc.hotkey() == "alt+m" and rail.shortcut_row.label.text() == "Shortcut"
    sc.capture.start_capture()
    assert ("suspend_hotkeys", True) in app.calls                # the global shortcuts wait meanwhile
    QTest.keyClick(sc.change, Qt.Key_M, Qt.ControlModifier | Qt.ShiftModifier)
    assert ("suspend_hotkeys", False) in app.calls
    import hotkeys
    want, _problem = hotkeys.combine("m", ctrl=True, shift=True)      # "ctrl+shift+m" (a Mac: "shift+cmd+m")
    assert ("set_setting", "meeting_hotkey", want) in app.calls
    assert sc.error() == ""


@pytest.mark.parametrize("combo, said", [("alt+r", "Dictation uses %s."),
                                         ("alt+s", "%s runs Smart Actions. Pick another."),
                                         ("ctrl+alt+a", "%s opens Live Assistant. Pick another."),
                                         ("ctrl+shift+q", "Couldn't use %s.")])
def test_a_refused_shortcut_says_why_and_keeps_the_old_one(shell, meetings_root, combo, said):
    win, app = shell("free")
    app.set_setting = lambda key, value: app.calls.append(("set_setting", key, value)) and False
    rail = _meetings(win).list_view.rail
    rail.shortcut.capture.captured.emit(combo)
    assert rail.shortcut.hotkey() == "alt+m"
    assert rail.shortcut.error() == said % _keys(combo)


def test_settings_lists_it_and_opens_its_row(shell, meetings_root):
    win, app = shell("free")
    page = win.open("settings")
    _spin()
    assert [r.name for r in page.shortcut_rows] == ["Dictate", "Smart Actions", "Record a meeting", "Live Assistant"]
    row = page.shortcut_rows[2]
    assert row.keys.accessibleName() == "Change the Record a meeting shortcut (%s)" % _keys("alt+m")
    app.cfg["meeting_hotkey"] = ""
    page.on_setting_changed("meeting_hotkey")
    _spin()
    assert row.keys.accessibleName() == "Set a Record a meeting shortcut"
    row.keys.click()
    _spin()
    meetings = win._pages["meetings"]
    assert win.current_route() == "meetings" and meetings.current_view() == "list"
    assert meetings.list_view.rail.shortcut.change.hasFocus()


def test_home_record_a_meeting_tile_shows_its_keycaps(shell, meetings_root):
    win, app = shell("free", width=1200)
    page = win.open("home")
    _spin(6)
    tile = page.tile_meeting
    assert tile.sub.text() == _keys("alt+m") and tile.sub.showing_keys()     # keycaps, like Dictate's Alt + R
    assert tile.accessibleName() == "Record a meeting, " + _keys("alt+m")
    assert "AI notetaker" in tile.toolTip()
    # every tile's name on one line, every second line in one keycap-tall slot
    assert len({t.sub_slot.height() for t in page.tiles}) == 1
    assert len({t.title_label.mapTo(t, t.title_label.rect().topLeft()).y() for t in page.tiles}) == 1
    app.cfg["meeting_hotkey"] = "ctrl+shift+m"
    page.on_setting_changed("meeting_hotkey")
    assert tile.sub.text() == _keys("ctrl+shift+m")
    app.cfg["meeting_hotkey"] = ""
    page.on_setting_changed("meeting_hotkey")
    assert tile.sub.text() == "Set shortcut"
    tile.click()
    assert win.current_route() == "meetings"


def _press(win, app):
    """main._on_meeting_hotkey on this window (show_main -> win.open, as AppController.show_main does)."""
    import main
    app.show_main = lambda route=None, **opts: (app.calls.append(("show_main", route, opts)),
                                                win.open(route, **opts), win)[2]
    app._hotkeys_suspended = False
    app._quitting = False
    main.AppController._on_meeting_hotkey(app)
    _spin()


@pytest.mark.parametrize("tier", ["free", "pro"])
def test_alt_m_records_like_the_button_with_the_pages_settings(shell, meetings_root, monkeypatch, tier):
    import meeting_session as msess
    monkeypatch.setattr(msess, "has_loopback", lambda: True)     # this system hears the call: every source
    session = FakeSession()
    win, app = shell(tier, meeting_session=session)
    page = _meetings(win)
    page.open_notes(str(_saved(meetings_root, "Old one")))       # elsewhere on the page: Record still goes
    card = page.list_view.start_card
    card.title_edit.setText("Board sync")
    card.title_edit.textEdited.emit("Board sync")
    card.sync_settings()
    i = card.mode_combo.findData("default_mic")                  # Record from: this meeting only
    card.mode_combo.setCurrentIndex(i)
    card.mode_combo.activated.emit(i)
    i = card.lang_combo.findData("de")
    card.lang_combo.setCurrentIndex(i)
    want_mode, want_lang = card.audio_mode(), card.language()
    assert (want_mode, want_lang) == ("default_mic", "de")
    win.open("home")
    _press(win, app)
    starts = [c for c in session.calls if c[0] == "start"]
    assert len(starts) == 1
    kw = starts[0][1]
    assert (kw["title"], kw["audio_mode"], kw["language"], kw["source"]) == ("Board sync", want_mode, want_lang, "page")
    # Recording is free: a Free person gets what Record gives them - no Pro window (it would come from
    # page.record(), the button's own path, if Record ever asked for Pro).
    assert not [c for c in app.calls if c[0] == "_pro_upsell"]
    assert win.current_route() == "meetings" and page.current_view() == "live"


def test_alt_m_respects_the_recording_notice_like_the_button(shell, meetings_root, monkeypatch):
    from ui.pages import meetings as page_mod
    monkeypatch.setattr(page_mod, "_ensure_consent", lambda parent, app: False)     # "Cancel" on the notice
    session = FakeSession()
    win, app = shell("pro", meeting_session=session)
    _meetings(win)
    _press(win, app)
    assert session.calls == []


def test_alt_m_while_recording_opens_that_meeting_and_never_stops_it(shell, meetings_root):
    for phase in ("recording", "paused"):
        session = FakeSession(phase)
        win, app = shell("pro", meeting_session=session)
        page = _meetings(win)
        page.show_list()
        win.open("home")
        _press(win, app)
        assert win.current_route() == "meetings" and page.current_view() == "live", phase
        assert session.calls == [], phase                        # no start, no stop, no pause
        assert session.phase == phase
