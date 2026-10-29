"""Gusan 2.0 Home and Dictation pages (ui/pages/home.py, ui/pages/dictation.py)
and the shortcut capture button (ui/components/hotkey_capture.py).

Built offscreen with the stub controller from conftest, Free / Pro / Guest;
the stores (history.json, meetings, transcripts) are real files in the
session's throwaway data folder."""
import json
import shutil
from datetime import datetime, timedelta

import pytest
from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (QAbstractButton, QApplication, QComboBox, QLineEdit, QPlainTextEdit, QWidget)

from tests_qt.conftest import make_stub_app

NOW = datetime(2026, 10, 4, 12, 0, 0)
D_ANNA = "Hi Anna, following up on yesterday’s call — the revised timeline is attached."


# ── fixtures ─────────────────────────────────────────────────────────────────
def _write_store(empty=False):
    import history
    import meeting_store
    import transcript_store
    for d in (meeting_store.meetings_dir(), transcript_store.transcripts_dir()):
        if d.exists():
            shutil.rmtree(d)
    if empty:
        history.save_all([])
        return {}
    mdir = meeting_store.meetings_dir() / "20260929_143000"
    entries = [
        {"text": D_ANNA, "language": "en", "backend": "local", "timestamp": "2026-10-04 11:42:00"},
        {"text": "Hi David, the domain renewal is done.", "language": "en", "backend": "local",
         "timestamp": "2026-10-04 11:05:00", "action": "write_email"},
        {"text": "Nos vemos mañana a las 10 en la oficina.", "language": "es", "backend": "local",
         "timestamp": "2026-10-04 09:30:00"},
        {"text": "Décalons l’appel à vendredi, 15 h.", "language": "fr", "backend": "local",
         "timestamp": "2026-10-03 18:40:00"},
        {"text": "Q4 roadmap - Meeting Notes\n\nShip it.", "language": "en", "backend": "meeting",
         "timestamp": "2026-09-29 15:13:00", "audio_dir": str(mdir)},
        {"text": "Remind me to renew the domain.", "language": "en", "backend": "local",
         "timestamp": "2026-09-29 09:10:00"},
        {"text": "Old one", "language": "", "backend": "local", "timestamp": "2026-08-01 10:00:00"},
    ]
    history.save_all(entries)
    mdir.mkdir(parents=True, exist_ok=True)
    (mdir / "meta.json").write_text(json.dumps({
        "title": "Q4 roadmap", "attendees": "Anna, David", "duration_sec": 2520,
        "started": "2026-09-29 14:30:00"}), "utf-8")
    (mdir / "notes.md").write_text("## Summary\nShip the beta.\n", "utf-8")
    tdir = transcript_store.transcripts_dir() / "20261003_172000"
    tdir.mkdir(parents=True, exist_ok=True)
    (tdir / "segments.json").write_text(json.dumps([{"start": 0, "end": 3, "text": "Week three.",
                                                      "speaker": None}]), "utf-8")
    (tdir / "meta.json").write_text(json.dumps({
        "title": "lecture_week3.mp4", "source_name": "lecture_week3.mp4", "duration_sec": 3120,
        "created": "2026-10-03T17:20:00"}), "utf-8")
    return {"meeting_dir": str(mdir), "transcript_id": "20261003_172000"}


class FakeCalendar(QObject):
    changed = Signal(list)

    def __init__(self, connected=True, title="Weekly product sync"):
        super().__init__()
        self.available = True
        self.is_connected = connected
        self.title = title

    def next_meeting(self):
        if not self.is_connected:
            return None
        start = datetime.now().astimezone().replace(hour=14, minute=30, second=0, microsecond=0)
        return {"id": "e1", "title": self.title, "start": start, "end": start + timedelta(minutes=30)}


def _app(tier="free", **cfg):
    app = make_stub_app(tier)
    app.auth.user_id = "u-test" if tier != "guest" else None
    # No "cleanup_remove_fillers" here: a config without the key removes filler words (on by default).
    app.cfg.update({"whisper_model": "small", "save_history": True, "cleanup_enabled": True,
                    "restore_clipboard": True, "overlay_theme": "auto",
                    "overlay_private": True, "live_assist_private": True, "output_action": "transcribe_only",
                    "action_model": "rule_based"})
    app.cfg.update(cfg)
    app.show_main = lambda route=None, **opts: app.calls.append(("show_main", route, opts))

    def set_settings(updates):
        app.calls.append(("set_settings", dict(updates)))
        return {k: True for k in updates}
    app.set_settings = set_settings
    return app


@pytest.fixture
def pin_clock(qapp, monkeypatch):
    import ui.pages.dictation as dictation
    import ui.pages.home as home
    monkeypatch.setattr(home, "_now", lambda: NOW)
    monkeypatch.setattr(dictation, "_now", lambda: NOW)
    yield


@pytest.fixture
def made(qapp):
    widgets = []
    yield widgets
    for w in widgets:
        w.hide()
        w.deleteLater()
    QApplication.processEvents()


@pytest.fixture
def home_page(qapp, pin_clock, made):
    from ui.pages import home

    def build(tier="free", data=True, **cfg):
        info = _write_store(empty=not data)
        app = _app(tier, **cfg)
        page = home.create(app)
        page.sync_io = True
        page.refresh(sync=True)
        made.append(page)
        return page, app, info
    return build


@pytest.fixture
def dict_page(qapp, pin_clock, made):
    from ui.pages import dictation

    def build(tier="free", data=True, **cfg):
        info = _write_store(empty=not data)
        app = _app(tier, **cfg)
        page = dictation.create(app)
        page.sync_io = True
        page.refresh(sync=True)
        made.append(page)
        return page, app, info
    return build


@pytest.fixture
def shell(qapp, pin_clock, made):
    from ui import shell as shell_mod

    def build(tier="free", **cfg):
        _write_store()
        app = _app(tier, **cfg)
        win = shell_mod.MainWindow(app)
        made.append(win)
        return win, app
    return build


def _names_missing(root):
    missing = []
    for b in root.findChildren(QAbstractButton):
        if not (b.text() or "").strip() and not (b.accessibleName() or "").strip():
            missing.append("%s(%s)" % (type(b).__name__, b.objectName()))
    for w in root.findChildren(QWidget):
        if isinstance(w, (QLineEdit, QComboBox, QPlainTextEdit)) and not (w.accessibleName() or "").strip():
            missing.append(type(w).__name__)
    return missing


# ── pure helpers ─────────────────────────────────────────────────────────────
def test_time_labels_and_durations():
    from ui.pages import home as H
    assert H.group_of(datetime(2026, 10, 4, 9, 0), NOW) == "Today"
    assert H.group_of(datetime(2026, 10, 3, 9, 0), NOW) == "Yesterday"
    assert H.group_of(datetime(2026, 9, 29, 9, 0), NOW) == "Earlier this week"
    assert H.group_of(datetime(2026, 8, 1, 9, 0), NOW) == "Earlier"
    assert H.time_text(datetime(2026, 10, 3, 18, 40), NOW) == "18:40"
    assert H.time_text(datetime(2026, 9, 29, 9, 0), NOW) == "Tue"
    assert H.time_text(datetime(2026, 8, 1, 9, 0), NOW) == "1 Aug"
    assert H.short_duration(2520) == "42 min"
    assert H.short_duration(6000) == "1 h 40 min"
    assert H.short_duration(45) == "45 s"
    assert H.parse_when("2026-10-04 11:42:00") == datetime(2026, 10, 4, 11, 42)
    assert H.parse_when("2026-10-03T17:20:00") == datetime(2026, 10, 3, 17, 20)
    assert H.parse_when("2026-10-03T17:20:00+00:00").tzinfo is None
    assert H.parse_when("") is None


def test_recent_merges_the_three_stores_and_dedupes_meetings(qapp):
    from ui.pages import home as H
    info = _write_store()
    items = H.recent_items(*H.load_sources())
    kinds = [(it["kind"], it["label"]) for it in items]
    assert kinds[:4] == [("dictation", "Dictation · EN"), ("smart", "Smart Action · Email"),
                         ("dictation", "Dictation · ES"), ("dictation", "Dictation · FR")]
    assert ("audio", "Audio to text · 52 min") in kinds
    # The history's meeting entry and the meeting folder are ONE row.
    meetings = [it for it in items if it["kind"] == "meeting"]
    assert len(meetings) == 1
    assert meetings[0]["main"] == "Q4 roadmap" and meetings[0]["status"] == "Notes ready"
    assert meetings[0]["open"] == ("meetings", {"folder": info["meeting_dir"]})   # Meetings' on_open option
    assert ("dictation", "Dictation") in kinds             # no language -> no code
    # Search matches every word, any case.
    assert [it["main"] for it in H.filter_items(items, "ROADMAP")] == ["Q4 roadmap"]
    assert H.filter_items(items, "nothing like this") == []


def test_recent_keeps_a_meeting_entry_whose_folder_is_gone(qapp):
    from ui.pages import home as H
    items = H.recent_items([{"text": "Standup - Meeting Notes\n\nx", "backend": "meeting",
                             "timestamp": "2026-10-01 10:00:00", "audio_dir": r"C:\gone\20261001_100000"}], [], [])
    assert [(it["kind"], it["main"]) for it in items] == [("meeting", "Standup")]


def test_active_meeting_reads_as_recording(qapp):
    from ui.pages import home as H
    m = {"dir": r"C:\m\1", "id": "1", "title": "Sync", "has_notes": False, "has_transcript": False,
         "started": "2026-10-04 11:00:00", "duration_sec": 0}
    items = H.recent_items([], [m], [], active_dir=r"C:\m\1", active_state="recording")
    assert items[0]["status"] == "Recording"
    assert items[0]["open"] == ("meetings", {"view": "live"})      # the live view, not empty notes


# ── Home ─────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("tier", ["free", "pro", "guest"])
def test_home_builds_for_every_plan(home_page, tier):
    page, app, _ = home_page(tier)
    assert page.header.title_label.text() == ("Welcome" if tier == "guest" else "Welcome, Aram")
    assert [t.title() for t in page.tiles] == ["Dictate", "Smart Actions", "Record a meeting", "Live Assistant",
                                               "Audio to text"]
    # No PRO tags on Home for any plan: a Free user who starts a Pro feature
    # gets the Pro window there.
    assert [t.pro_tag is not None for t in page.tiles] == [False] * 5
    # Every tile's second line has the keycaps' height, so the names line up.
    assert len({t.sub_slot.height() for t in page.tiles}) == 1
    page.resize(1100, 700)
    page.show()
    assert len({t.title_label.mapTo(t, t.title_label.rect().topLeft()).y() for t in page.tiles}) == 1
    page.hide()
    assert page.hero_keys.keys() == ["Alt", "R"]
    assert page.status_label.text() == "Ready"
    assert page.lang_chip.text() == "Detect language"
    assert page.model_chip.accessibleName() == "Balanced · Whisper Small"
    assert page.where_chip.text() == "On this computer"
    assert page.tile_meeting.sub_text() == "AI notetaker"
    assert page.tile_live.sub.text() == "Ctrl + Alt + A"            # its shortcut, like Smart Actions
    assert len(page.shown_rows()) == 6                     # the mockup's six
    assert not _names_missing(page)
    assert not any(c[0] == "_pro_upsell" for c in app.calls)


def _with_rename_note(app, due=True):
    app.rename_note_pending = due
    app.rename_note_due = lambda: app.rename_note_pending
    app.dismiss_rename_note = lambda: (app.calls.append(("dismiss_rename_note",)),
                                       setattr(app, "rename_note_pending", False))
    return app


def test_home_tells_1x_upgraders_once_that_transcribe_is_now_gusan(qapp, pin_clock, made):
    from ui.pages import home
    _write_store()
    app = _with_rename_note(_app("free"))
    page = home.create(app)
    made.append(page)
    note = page.rename_note
    assert page.rename_note_text.text() == ("Transcribe is now Gusan. Same app — your settings, history "
                                            "and sign-in are kept.")
    assert not note.isHidden()
    assert app.calls.count(("dismiss_rename_note",)) == 0     # not until it is on screen
    page.resize(1000, 800)
    page.show()
    QApplication.processEvents()
    assert note.isVisible()
    assert app.calls.count(("dismiss_rename_note",)) == 1     # shown once: remembered
    page.hide()
    page.show()                                               # still up for this run...
    QApplication.processEvents()
    assert note.isVisible() and app.calls.count(("dismiss_rename_note",)) == 1
    page.rename_note_close.click()                            # ...until ✕
    assert note.isHidden()
    assert page.rename_note_close.accessibleName() == "Dismiss"
    # The next launch (a new page) has no line, and nobody else ever sees one.
    for a in (app, _app("free"), _with_rename_note(_app("pro"), due=False)):
        p = home.create(a)
        made.append(p)
        assert p.rename_note.isHidden()


def test_home_recent_rows_and_groups(home_page):
    page, _, _ = home_page()
    rows = page.shown_rows()
    assert [r.time_label.text() for r in rows] == ["11:42", "11:05", "09:30", "18:40", "17:20", "Tue"]
    assert rows[0].main.text() == D_ANNA
    assert rows[4].main.text() == "lecture_week3.mp4 · Transcript ready"
    assert rows[5].kind_label.text() == "Meeting · 42 min"
    page.search.setText("lecture")
    page._render_list()
    assert [r.main.text() for r in page.shown_rows()] == ["lecture_week3.mp4 · Transcript ready"]
    page.search.setText("zzz")
    page._render_list()
    assert page.shown_rows() == []


def test_home_empty_states(home_page):
    page, app, _ = home_page(data=False)
    assert page.shown_rows() == []
    texts = [w.text() for w in page.list_box.findChildren(type(page.caption)) if w.isVisibleTo(page.list_box)]
    assert any(t.startswith("Nothing yet. Press Alt + R") for t in texts)
    app.cfg["privacy_mode"] = True
    page._render_list()
    texts = [w.text() for w in page.list_box.findChildren(type(page.caption)) if w.isVisibleTo(page.list_box)]
    assert "Privacy mode is on: nothing new is saved." in texts


def test_home_tiles_and_rows_open_pages(home_page):
    page, app, info = home_page()
    page.tile_meeting.click()
    page.tile_audio.click()
    rows = page.shown_rows()
    rows[0].open_button.click()
    rows[4].open_button.click()
    rows[5].open_button.click()
    opened = [c[1:] for c in app.calls if c[0] == "show_main"]
    assert opened == [("meetings", {}), ("audio_to_text", {}),
                      ("dictation", {"entry": "2026-10-04 11:42:00"}),
                      ("audio_to_text", {"transcript_id": info["transcript_id"]}),
                      ("meetings", {"folder": info["meeting_dir"]})]


def test_home_tiles_route_inside_the_window(shell):
    win, app = shell("free")
    win.show()
    home = win.open("home")
    home.tile_dictate.click()
    assert win.current_route() == "dictation"
    win.open("home")
    QTest.keyClick(home.tile_smart, Qt.Key_Return)        # keyboard reaches the tiles
    assert win.current_route() == "smart_actions"
    assert all(t.focusPolicy() & Qt.TabFocus for t in home.tiles)


def test_home_copy_puts_the_text_on_the_clipboard(home_page):
    page, _, _ = home_page()
    rows = page.shown_rows()
    rows[0].copy_button.click()
    assert QGuiApplication.clipboard().text() == D_ANNA
    rows[5].copy_button.click()                            # a meeting copies its notes
    assert "Ship the beta." in QGuiApplication.clipboard().text()


TODO_MD = "- [ ] Call David\n- [ ] Book the **room**"


def test_home_shows_and_copies_smart_actions_without_markdown(home_page):
    # D9: a Smart Action's Markdown reads as plain text in Recent (and in the
    # tray, which shows the same "main"), and copies as HTML + nice plain text.
    import history
    page, _, info = home_page()
    entries = history.load()
    entries.insert(0, {"text": TODO_MD, "language": "en", "backend": "local", "timestamp": "2026-10-04 11:50:00",
                       "action": "make_todo_list"})
    entries.insert(1, {"text": "- spoken dash and *stars*", "language": "en", "backend": "local",
                       "timestamp": "2026-10-04 11:45:00"})
    history.save_all(entries)
    page.refresh(sync=True)
    rows = page.shown_rows()
    assert rows[0].main.text() == "☐ Call David ☐ Book the room"
    assert rows[1].main.text() == "- spoken dash and *stars*"           # a plain dictation is never rendered
    cb = QGuiApplication.clipboard()
    rows[0].copy_button.click()
    assert cb.text() == "☐ Call David\n☐ Book the room"
    assert cb.mimeData().hasHtml() and "<b>room</b>" in cb.mimeData().html()
    rows[1].copy_button.click()
    assert cb.text() == "- spoken dash and *stars*" and not cb.mimeData().hasHtml()
    page.search.setText("roadmap")
    page._render_list()
    meeting = next(r for r in page.shown_rows() if r.item["kind"] == "meeting")
    meeting.copy_button.click()                             # the notes: rendered, with HTML
    assert cb.text() == "Summary\nShip the beta." and cb.mimeData().hasHtml()


def test_home_copies_a_meeting_without_notes_as_readable_paragraphs(home_page):
    from ui.pages import home as H
    page, _, info = home_page()
    import pathlib
    folder = pathlib.Path(info["meeting_dir"])
    (folder / "notes.md").unlink()
    talk = " ".join("Point %d of the plan is settled now." % i for i in range(40))
    (folder / "transcript.txt").write_text("Speaker 1: " + talk, "utf-8")
    text, rich = H.read_copy(("meeting", str(folder)))
    assert not rich and text.startswith("Speaker 1: Point 0") and "\n\n" in text
    assert text.replace("\n\n", " ") == "Speaker 1: " + talk


def test_home_smart_actions_tile_shows_its_shortcut(home_page):
    # D7: the Smart Actions tile has its own shortcut's keycaps, like Dictate.
    page, app, _ = home_page("pro")
    assert page.tile_smart.sub.text() == "Alt + S"                      # the default (cfg has none yet)
    assert page.tile_smart.accessibleName() == "Smart Actions, Alt + S"
    app.cfg["smart_actions_hotkey"] = "ctrl+shift+e"
    page.on_setting_changed("smart_actions_hotkey")
    assert page.tile_smart.sub.text() == "Ctrl + Shift + E"
    assert page.tile_smart.accessibleName().startswith("Smart Actions, Ctrl + Shift + E")
    app.cfg["smart_actions_hotkey"] = ""                               # it clashed at the upgrade
    page.on_setting_changed("smart_actions_hotkey")
    assert page.tile_smart.sub.text() == "Set shortcut"
    assert not page.tile_smart.sub.showing_keys()
    app.cfg["live_assist_hotkey"] = ""                                 # Live Assistant: same rules
    page.on_setting_changed("live_assist_hotkey")
    assert page.tile_live.sub.text() == "Set shortcut"
    assert page.hero_keys.keys() == ["Alt", "R"]                       # the hero stays on dictation


def test_home_next_meeting_chip_follows_the_calendar(home_page):
    page, app, _ = home_page()
    assert page.next_chip.isHidden()                       # no calendar service: no chip
    app.calendar = FakeCalendar(connected=True)
    app.calendar.changed.connect(lambda *_: page._update_next())
    page._update_next()
    assert not page.next_chip.isHidden()
    assert page.next_chip.title() == "Weekly product sync"
    assert page.next_chip.time() == "14:30"
    assert "Weekly product sync" in page.next_chip.accessibleName()
    page.next_chip.click()
    assert ("show_main", "meetings", {}) in app.calls
    app.calendar.is_connected = False
    app.calendar.changed.emit([])
    assert page.next_chip.isHidden()


def _meeting_at(start, title="Weekly product sync", minutes=30):
    return {"id": "e1", "title": title, "start": start, "end": start + timedelta(minutes=minutes)}


def test_home_chip_says_now_while_the_meeting_runs(home_page):
    # G10: a running meeting shows "Now: <title>" (time word "Now"); before and after, "Next: ..." as built.
    page, app, _ = home_page()
    now = datetime.now().astimezone()
    cal = FakeCalendar(connected=True)
    state = {"v": ("now", _meeting_at(now - timedelta(minutes=5)))}
    cal.now_or_next = lambda: state["v"]
    app.calendar = cal
    page._update_next()
    chip = page.next_chip
    assert not chip.isHidden()
    assert chip.title() == "Weekly product sync" and chip.time() == "Now" and chip.prefix() == "Now:"
    assert chip.accessibleName() == "Meeting now: Weekly product sync"
    assert chip.toolTip() == "Opens Meetings"                # a started meeting is past its reminder
    # The meeting ended and another is next: the chip reads as it always did.
    state["v"] = ("next", _meeting_at(now.replace(hour=14, minute=30, second=0, microsecond=0)))
    page._update_next()
    assert chip.prefix() == "Next:" and chip.time() == "14:30"
    assert chip.accessibleName() == "Next meeting: Weekly product sync at 14:30"
    assert chip.toolTip() == "Gusan asks to record 1 min before"
    # Nothing running, nothing next (or the calendar went): no chip.
    state["v"] = ("", None)
    page._update_next()
    assert chip.isHidden()
    state["v"] = ("now", _meeting_at(now - timedelta(minutes=5)))
    page._update_next()
    assert not chip.isHidden()
    cal.is_connected = False
    page._update_next()
    assert chip.isHidden()


def test_home_chip_keeps_working_with_a_service_that_has_no_now(home_page):
    page, app, _ = home_page()
    app.calendar = FakeCalendar(connected=True)             # next_meeting() only
    page._update_next()
    assert page.next_chip.prefix() == "Next:" and page.next_chip.time() == "14:30"


def test_home_chip_width_follows_its_words(home_page):
    page, app, _ = home_page()
    now = datetime.now().astimezone()
    cal = FakeCalendar(connected=True)
    cal.now_or_next = lambda: ("now", _meeting_at(now - timedelta(minutes=1), title="Quarterly planning"))
    app.calendar = cal
    page._update_next()
    running = page.next_chip.width()
    cal.now_or_next = lambda: ("next", _meeting_at(now + timedelta(hours=1), title="Quarterly planning"))
    page._update_next()
    assert page.next_chip.width() > running                 # "Next: ... · 14:30" is longer than "Now: ..."


def test_home_hero_follows_settings(home_page):
    page, app, _ = home_page("pro")
    app.cfg.update(hotkey="ctrl+shift+space", language="multi", backend="managed", managed_provider="gemini",
                   live_assist_hotkey="alt+e")
    for key in ("hotkey", "language", "backend", "live_assist_hotkey"):
        page.on_setting_changed(key)
    assert page.hero_keys.keys() == ["Ctrl", "Shift", "Space"]
    assert page.hero_keys.sizeHint().width() <= 380        # a long shortcut is drawn smaller
    assert page.hero_keys.key_height() < 92
    assert page.lang_chip.text() == "Mixed languages"
    assert page.model_chip.accessibleName() == "Gusan Cloud · Fast"
    assert page.where_chip.isHidden()                      # the model chip already says Gusan Cloud
    assert page.tile_live.sub.text() == "Alt + E"
    assert page.tile_live.accessibleName().startswith("Live Assistant, Alt + E")
    assert page.tile_dictate.accessibleName().startswith("Dictate, Ctrl + Shift + Space")


def test_home_shows_a_model_download(home_page):
    page, app, _ = home_page()

    class Downloads:
        def state(self, name):
            return "downloading"

        def progress_of(self, name):
            return (42, 1, 2)
    app.downloads = Downloads()
    page.on_setting_changed("whisper_model")
    assert page.status_label.text() == "Getting ready · 42%"


class FakeDownloads(QObject):
    state_changed = Signal(str, str)
    progress = Signal(str, int, int, int)
    finished = Signal(str, str, str)

    def __init__(self, state="downloading", pct=42):
        super().__init__()
        self._state, self._pct = state, pct

    def state(self, name):
        return self._state

    def progress_of(self, name):
        return (self._pct, 1, 2) if self._state == "downloading" else None


def test_service_signals_connect_once_and_go_with_the_page(qapp, pin_clock):
    """Bound slots: one connection however often the page opens, none after
    the page is deleted (the error card's Try again rebuilds a page)."""
    from PySide6.QtCore import SIGNAL, QEvent
    from ui.pages import dictation, home
    _write_store()
    app = _app("free")
    app.downloads = FakeDownloads()
    app.calendar = FakeCalendar(connected=True)
    progress = SIGNAL("progress(QString,int,int,int)")
    changed = SIGNAL("changed(QVariantList)")
    for mod in (home, dictation):
        page = mod.create(app)
        page.sync_io = True
        page.on_open()
        page.on_open()
        assert app.downloads.receivers(progress) == 1, mod.__name__
        if mod is home:                                     # the Next chip and the Coming up section: once each
            assert app.calendar.receivers(changed) == 2
            assert page.upcoming is not None
        page.deleteLater()
        QApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        assert app.downloads.receivers(progress) == 0, mod.__name__
        assert app.calendar.receivers(changed) == 0, mod.__name__
    app.downloads.progress.emit("small", 50, 1, 2)          # nothing left to call: no error
    app.calendar.changed.emit([])


def test_download_progress_updates_only_the_status_line(home_page):
    from ui.pages import home as H
    page, app, _ = home_page()
    app.downloads = FakeDownloads("downloading", 10)
    page.on_open()
    caps = page.hero_keys.findChildren(H.BigKeycap)
    app.downloads._pct = 55
    app.downloads.progress.emit("small", 55, 1, 2)
    assert page.status_label.text() == "Getting ready · 55%"
    page.on_setting_changed("whisper_model")                # same shortcut: the keycaps stay
    assert page.hero_keys.findChildren(H.BigKeycap) == caps
    app.downloads._state = "ready"
    app.downloads.finished.emit("small", "ready", "")
    assert page.status_label.text() == "Ready"


def test_home_follows_a_meeting_being_recorded(home_page):
    class Session(QObject):
        state_changed = Signal(str)

        def __init__(self):
            super().__init__()
            self.dir, self.phase = None, "idle"

        def active_dir(self):
            return self.dir

    page, app, info = home_page()
    app.meeting_session = Session()
    page.on_open()

    def meeting_row():
        return next(r for r in page.shown_rows() if r.item["kind"] == "meeting")
    s = app.meeting_session
    s.dir, s.phase = info["meeting_dir"], "recording"
    s.state_changed.emit("recording")
    assert meeting_row().main.text() == "Q4 roadmap · Recording"
    assert meeting_row().item["open"] == ("meetings", {"view": "live"})
    s.dir, s.phase = None, "done"
    s.state_changed.emit("done")
    assert meeting_row().main.text() == "Q4 roadmap · Notes ready"


def test_home_refresh_account(home_page):
    page, app, _ = home_page("free")
    app.set_tier("guest")
    page.refresh_account()
    assert page.header.title_label.text() == "Welcome"


# ── Dictation ────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("tier", ["free", "pro", "guest"])
def test_dictation_builds_for_every_plan(dict_page, tier):
    page, app, _ = dict_page(tier)
    # What the board shows; "Languages you mix" (Wave 4: taken over from the
    # retired Settings window) sits under Language, shown only for Mixed languages.
    rail_rows = [r.label.text() for r in page.rail.rows() if not r.isHidden()]
    assert rail_rows == ["Shortcut", "Language", "Clean up mistakes", "Remove filler words", "Put my clipboard back",
                         "Dictation bar look", "Hide bar in screen shares", "Gusan Cloud speech"]
    assert [r.label.text() for r in page.rail.rows()][2] == "Languages you mix"
    assert page.row_mix.isHidden()
    assert page.row_cloud.pro_tag is not None
    assert page.row_cloud.model_label.text() == "Fast"
    assert page.runs_on_text() == "Runs on Balanced · Whisper Small · on this computer"
    assert page.hero_keys.keys() == ["Alt", "R"]
    assert page.shortcut.hotkey() == "alt+r"
    assert [page.language.itemText(i) for i in range(page.language.count())] == [
        "Detect automatically", "Mixed languages", "English", "Arabic", "Armenian", "French", "German", "Russian",
        "Spanish"]
    # D7: no "Pastes" control; one quiet line points to Smart Actions' own shortcut.
    assert not hasattr(page, "mode")
    assert page.smart_line_text() == "For emails, lists and translations press Alt + S · Smart Actions"
    assert page.smart_keys.accessibleName() == "Alt + S"
    assert page.smart_link.accessibleName() == "Open Smart Actions"
    assert not page.cloud.isChecked()
    assert not _names_missing(page)


def test_languages_you_mix(dict_page):
    # The Mixed-languages picks (the retired Settings window had them): shown
    # only for Mixed languages, saved as ticked through set_setting.
    page, app, _ = dict_page(mix_languages=["ru", "hy"])
    assert page.row_mix.isHidden()
    assert [c for c, cb in page.mix_checks.items() if cb.isChecked()] == ["hy", "ru"]
    page.language.setCurrentIndex(page.language.findData("multi"))
    assert ("set_setting", "language", "multi") in app.calls
    assert not page.row_mix.isHidden() and page.row_mix.hint_label.text()
    page.mix_checks["en"].setChecked(True)
    assert app.calls[-1] == ("set_setting", "mix_languages", ["en", "hy", "ru"])
    app.cfg.update(language="multi", mix_languages=["en", "hy", "ru"])
    page.on_setting_changed("mix_languages")
    assert not page.row_mix.isHidden()
    app.cfg["language"] = "fr"                         # changed elsewhere
    page.on_setting_changed("language")
    assert page.row_mix.isHidden()
    assert all(cb.accessibleName() or cb.text() for cb in page.mix_checks.values())


def test_dictation_list_leaves_out_meetings_and_smart_actions(dict_page):
    page, _, _ = dict_page()
    texts = [r.main.text() for r in page.shown_rows()]
    assert texts[0] == D_ANNA
    assert not any("Meeting Notes" in t or "domain renewal is done" in t for t in texts)
    assert [r.lang.accessibleName() if r.lang else "" for r in page.shown_rows()] == ["EN", "ES", "FR", "EN", ""]
    page.search.setText("vendredi")
    page._render_list()
    assert [r.main.text() for r in page.shown_rows()] == ["Décalons l’appel à vendredi, 15 h."]


def test_dictation_rows_copy_and_highlight(dict_page):
    page, _, _ = dict_page()
    page.shown_rows()[1].copy_button.click()
    assert QGuiApplication.clipboard().text() == "Nos vemos mañana a las 10 en la oficina."
    page.on_open(entry="2026-10-03 18:40:00")
    row = next(r for r in page.shown_rows() if r.timestamp() == "2026-10-03 18:40:00")
    assert row._flash is True


@pytest.mark.parametrize("key,row_attr,default", [
    ("cleanup_enabled", "row_cleanup", True),
    ("cleanup_remove_fillers", "row_fillers", True),      # on out of the box
    ("restore_clipboard", "row_clipboard", True),
    ("overlay_private", "row_private", True),
])
def test_dictation_toggles_apply_at_once(dict_page, key, row_attr, default):
    page, app, _ = dict_page()
    toggle = getattr(page, row_attr).control
    assert toggle.isChecked() is default
    toggle.click()
    assert ("set_setting", key, not default) in app.calls


def test_remove_filler_words_is_on_without_a_saved_choice(dict_page):
    import main
    page, app, _ = dict_page()
    assert "cleanup_remove_fillers" not in app.cfg
    assert main.DEFAULT["cleanup_remove_fillers"] is True
    assert page.row_fillers.control.isChecked() is True
    assert page.row_fillers.control.accessibleName() == "Remove filler words"
    # A saved False stays False (an existing install that turned it off).
    off, _app_off, _ = dict_page(cleanup_remove_fillers=False)
    assert off.row_fillers.control.isChecked() is False
    off.row_fillers.control.click()
    assert ("set_setting", "cleanup_remove_fillers", True) in _app_off.calls


def test_a_refused_setting_puts_the_control_back(dict_page):
    page, app, _ = dict_page()
    app.set_setting = lambda key, value: app.calls.append(("set_setting", key, value)) and False
    page.row_fillers.control.click()
    QApplication.processEvents()
    assert page.row_fillers.control.isChecked() is True
    page.language.setCurrentIndex(page.language.findData("es"))
    QApplication.processEvents()
    assert page.language.currentData() == "auto"


def test_dictation_language_and_bar_look(dict_page):
    page, app, _ = dict_page()
    page.language.setCurrentIndex(page.language.findData("es"))
    assert ("set_setting", "language", "es") in app.calls
    page.bar_look.options["dark"].click()
    assert ("set_setting", "overlay_theme", "dark") in app.calls
    assert page.bar_preview.look() == "dark"
    assert page.bar_look.value() == "dark"


def test_dictation_follows_settings_changed_elsewhere(dict_page):
    page, app, _ = dict_page()
    app.cfg.update(hotkey="ctrl+alt+d", language="fr", cleanup_remove_fillers=False, overlay_theme="light",
                   smart_actions_hotkey="ctrl+shift+s")
    for key in ("hotkey", "language", "cleanup_remove_fillers", "overlay_theme", "smart_actions_hotkey"):
        page.on_setting_changed(key)
    assert page.hero_keys.keys() == ["Ctrl", "Alt", "D"]
    assert page.shortcut.hotkey() == "ctrl+alt+d"
    assert page.language.currentData() == "fr"
    assert not page.row_fillers.control.isChecked()
    assert page.bar_look.value() == "light"
    assert page.smart_keys.accessibleName() == "Ctrl + Shift + S"
    assert not [c for c in app.calls if c[0] == "set_setting"]   # syncing never writes back


def test_smart_actions_line(dict_page):
    """D7: Alt+R always pastes the exact words; one quiet line names the Smart
    Actions shortcut and links to its page. output_action is never written."""
    page, app, _ = dict_page("pro", output_action="smart_auto")       # a 1.9.x leftover is ignored
    assert page.smart_line_text() == "For emails, lists and translations press Alt + S · Smart Actions"
    page.smart_link.click()
    assert ("show_main", "smart_actions", {}) in app.calls
    assert not [c for c in app.calls if c[0] == "set_setting"]
    app.cfg["smart_actions_hotkey"] = ""                   # none set (it clashed at the upgrade)
    page.on_setting_changed("smart_actions_hotkey")
    assert page.smart_keys.isHidden()
    assert page.smart_line_text() == "For emails, lists and translations, use Smart Actions"


def test_shortcut_clash_with_smart_actions_says_so(dict_page):
    page, app, _ = dict_page()
    app.set_setting = lambda key, value: app.calls.append(("set_setting", key, value)) and False
    page.shortcut.captured.emit("alt+s")                   # the Smart Actions keys
    assert page.shortcut.hotkey() == "alt+r"
    assert page.shortcut_problem() == "Alt + S runs Smart Actions. Pick another."


def test_gusan_cloud_speech(dict_page):
    page, app, _ = dict_page("free")
    page.cloud.click()
    QApplication.processEvents()
    assert ("_pro_upsell", "Fast cloud transcription") in app.calls
    assert not page.cloud.isChecked()
    assert not [c for c in app.calls if c[0] == "set_settings"]

    pro, papp, _ = dict_page("pro")
    pro.cloud.click()
    assert ("set_settings", {"backend": "managed", "managed_provider": "gemini"}) in papp.calls
    papp.cfg.update(backend="managed", managed_provider="gemini")
    pro.on_setting_changed("backend")
    assert pro.cloud.isChecked()
    assert pro.runs_on_text() == "Runs on Gusan Cloud · Fast"
    pro.cloud.click()
    assert ("set_settings", {"backend": "local", "whisper_model": "small"}) in papp.calls



@pytest.fixture
def privacy_asked(monkeypatch):
    """The "Privacy mode is on" question answered without a window:
    ``privacy_asked.answer`` (default yes), each question's (title, body, yes, no)."""
    from types import SimpleNamespace
    from ui.components import mini_prompt
    rec = SimpleNamespace(calls=[], answer=True)

    def fake_ask(parent, title, body, yes, no, icon="info"):
        rec.calls.append((title, body, yes, no))
        return rec.answer
    monkeypatch.setattr(mini_prompt, "ask", fake_ask)
    return rec


def test_cloud_speech_in_privacy_mode_asks_then_turns_privacy_off(dict_page, privacy_asked):
    page, app, _ = dict_page("pro", privacy_mode=True)
    assert not page.cloud.isChecked()
    page.cloud.click()
    QApplication.processEvents()
    assert privacy_asked.calls == [("Privacy mode is on",
                                    "Gusan Cloud sends your voice to our servers. Turn off Privacy mode to use it?",
                                    "Turn off Privacy mode", "Keep it on")]
    # Privacy mode goes off through the controller in the same call as the cloud choice.
    assert ("set_settings", {"privacy_mode": False, "backend": "managed", "managed_provider": "gemini"}) in app.calls
    assert not [c for c in app.calls if c[0] == "_pro_upsell"]
    assert page.row_cloud.hint_label.isHidden()                      # no "Off while Privacy mode is on"
    app.cfg.update(privacy_mode=False, backend="managed", managed_provider="gemini")   # what the controller did
    page.on_setting_changed("backend")
    assert page.cloud.isChecked()


def test_cloud_speech_in_privacy_mode_can_keep_privacy_on(dict_page, privacy_asked):
    privacy_asked.answer = False
    page, app, _ = dict_page("pro", privacy_mode=True)
    page.cloud.click()
    QApplication.processEvents()
    assert len(privacy_asked.calls) == 1
    assert not page.cloud.isChecked()                                # back off
    assert not [c for c in app.calls if c[0] in ("set_settings", "set_setting", "_pro_upsell")]
    assert page.row_cloud.hint_label.isHidden()


@pytest.mark.parametrize("tier", ["free", "guest"])
def test_cloud_speech_for_free_in_privacy_mode_opens_the_pro_window(dict_page, privacy_asked, tier):
    page, app, _ = dict_page(tier, privacy_mode=True)
    page.cloud.click()
    QApplication.processEvents()
    assert ("_pro_upsell", "Fast cloud transcription") in app.calls
    assert not privacy_asked.calls                                   # Pro is checked before Privacy mode
    assert not page.cloud.isChecked()
    assert not [c for c in app.calls if c[0] == "set_settings"]
    assert page.row_cloud.hint_label.isHidden()


def test_shortcut_capture_applies_through_set_setting(dict_page):
    page, app, _ = dict_page()
    btn = page.shortcut
    btn.button.click()
    assert btn.is_capturing()
    QTest.keyClick(btn.button, Qt.Key_R)                   # a bare typing key is refused
    assert btn.is_capturing()
    assert page.shortcut_problem() == "Add Ctrl, Alt, Shift or Win."
    assert page.row_shortcut.hint_label.property("textRole") == "error"
    QTest.keyClick(btn.button, Qt.Key_D, Qt.ControlModifier | Qt.AltModifier)
    assert not btn.is_capturing()
    assert ("set_setting", "hotkey", "ctrl+alt+d") in app.calls
    assert page.hero_keys.keys() == ["Ctrl", "Alt", "D"]
    assert page.shortcut_problem() == ""


def test_shortcut_refused_by_the_controller(dict_page):
    page, app, _ = dict_page()
    app.set_setting = lambda key, value: app.calls.append(("set_setting", key, value)) and False
    page.shortcut.captured.emit("ctrl+alt+a")              # the Live Assistant's keys
    assert page.shortcut.hotkey() == "alt+r"
    assert page.shortcut_problem() == "Ctrl + Alt + A opens Live Assistant. Pick another."
    page.shortcut.captured.emit("alt+q")
    assert page.shortcut_problem() == "Couldn't use Alt + Q. Try another."
    assert page.hero_keys.keys() == ["Alt", "R"]


def test_shortcut_capture_pauses_global_hotkeys(dict_page):
    page, app, _ = dict_page()
    app.suspend_hotkeys = lambda on: app.calls.append(("suspend_hotkeys", on))
    page.shortcut.start_capture()
    QTest.keyClick(page.shortcut.button, Qt.Key_Escape)
    assert not page.shortcut.is_capturing()
    assert [c for c in app.calls if c[0] == "suspend_hotkeys"] == [("suspend_hotkeys", True),
                                                                  ("suspend_hotkeys", False)]


def test_export_writes_the_shown_dictations(dict_page, tmp_path, monkeypatch):
    import ui.pages.dictation as dictation
    page, app, _ = dict_page()
    target = tmp_path / "out.csv"
    monkeypatch.setattr(dictation, "_ask_save_path", lambda parent, fmt: str(target))
    page.search.setText("anna")
    page._query = "anna"
    assert page.export("csv") is True
    assert target.exists()
    body = target.read_text("utf-8-sig")
    assert "Hi Anna" in body and "Nos vemos" not in body
    assert ("track", "history_exported", {"format": "csv", "count": 1, "from": "dictation"}) in app.calls
    monkeypatch.setattr(dictation, "_ask_save_path", lambda parent, fmt: "")
    assert page.export("txt") is False                     # cancelled dialog: nothing written


def test_dictation_empty_and_privacy_states(dict_page):
    page, app, _ = dict_page(data=False)
    texts = [w.text() for w in page.list_box.findChildren(type(page.list_title))]
    assert "Nothing yet. Try it in the box above." in texts
    assert not page.export_button.isEnabled()
    app.cfg["privacy_mode"] = True
    page._render_list()
    texts = [w.text() for w in page.list_box.findChildren(type(page.list_title)) if w.isVisibleTo(page.list_box)]
    assert "Privacy mode is on: nothing new is saved." in texts


def test_practice_box_listening(dict_page):
    page, app, _ = dict_page()
    assert not page.practice.is_listening()
    page.set_listening(True)
    assert page.practice.is_listening()
    assert not page.practice.rec_label.isHidden()
    app.is_rec = True                                     # the poll needs the box focused too
    page._poll_recording()
    assert not page.practice.is_listening()
    assert page.practice.edit.accessibleName() == "Practice box"


def test_change_links_open_speech_settings(dict_page):
    page, app, _ = dict_page()
    page.runs_change.click()
    page.more_options.click()
    assert [c for c in app.calls if c[0] == "show_main"] == [("show_main", "settings", {"section": "speech"})] * 2


# ── in the window ────────────────────────────────────────────────────────────
@pytest.mark.parametrize("tier", ["free", "pro", "guest"])
def test_both_pages_open_in_the_window_without_error_cards(shell, tier):
    from ui.shell import ErrorPage
    win, app = shell(tier)
    win.resize(1280, 820)
    win.show()
    for route in ("home", "dictation"):
        page = win.open(route)
        QApplication.processEvents()
        assert not isinstance(page, ErrorPage), route
        assert not _names_missing(page), route
    win.on_setting_changed("hotkey")
    win.refresh_account()
    win.open("home")
    QApplication.processEvents()
    assert win.page("home").tile_dictate.sub.showing_keys()      # small Alt + R keycaps on the tile
    # The page fits the window: no horizontal overflow at the default and minimum widths.
    for w in (1280, 960):
        win.resize(w, 820)
        QApplication.processEvents()
        for route in ("home", "dictation"):
            holder = win._holders[route]
            win.open(route)
            QApplication.processEvents()
            assert win.page(route).width() <= holder.viewport().width(), (route, w)
        # Narrow: the Dictation settings move under the page instead of squeezing it.
        assert win.page("dictation").is_stacked() is (w == 960)


def test_narrow_window_reflows_tiles_hero_and_rail(shell):
    win, app = shell("pro", hotkey="ctrl+shift+space")
    win.show()
    QApplication.processEvents()                           # showing fits it to the small offscreen screen
    win.resize(1280, 820)
    home = win.open("home")
    QApplication.processEvents()
    assert home.tile_columns() == 5
    wide_keys = home.hero_keys.key_height()
    win.resize(960, 820)
    QApplication.processEvents()
    assert home.tile_columns() == 3                        # three + two: no "Record a…" cut short
    for t in home.tiles:
        assert t.title_label.width() >= t.title_label.sizeHint().width() - 2, t.title()
    assert home.hero_keys.key_height() < wide_keys         # the keys give the caption its one line
    assert home.caption.heightForWidth(home.caption.width()) < 40
    page = win.open("dictation")
    page.refresh(sync=True)                                # the list now (it loads on a worker)
    QApplication.processEvents()
    assert page.is_stacked()
    # The rail sits under the hero and its Runs on line, above the list (not after every dictation).
    assert page.hero_card.geometry().bottom() < page.rail.geometry().top() < page.list_box.geometry().top()
    chain = page.tab_chain()                               # and Tab follows it
    first_row = page.shown_rows()[0].copy_button
    assert chain.index(page.shortcut.button) < chain.index(page.search) < chain.index(first_row)
    win.resize(1280, 820)
    QApplication.processEvents()
    assert not page.is_stacked()
    assert page.rail.geometry().left() > page.hero_card.geometry().right()
    chain = page.tab_chain()                               # beside the list: the list's rows first
    assert chain.index(page.search) < chain.index(first_row) < chain.index(page.shortcut.button)
    win.open("home")
    QApplication.processEvents()
    assert home.tile_columns() == 5


def test_tab_order_never_reaches_into_a_combo_popup(dict_page):
    # The Language combo's popup list is its own window: a Tab order across
    # windows is refused (a Qt warning on every list render, link lost).
    from PySide6.QtCore import qInstallMessageHandler
    page, app, _ = dict_page()
    seen = []
    prev = qInstallMessageHandler(lambda _mode, _ctx, msg: seen.append(msg))
    try:
        page.refresh(sync=True)
        QApplication.processEvents()
    finally:
        qInstallMessageHandler(prev)
    assert not [m for m in seen if "setTabOrder" in m], seen
    win = page.rail.window()
    assert page._rail_focusables() and all(w.window() is win for w in page._rail_focusables())


def test_rail_cloud_row_names_the_profile_gusan_cloud_is_asked_for(dict_page):
    # (Gusan Cloud's server chooses the model: the row says "Fast", whatever 1.9's provider setting says)
    page, app, _ = dict_page("pro", backend="managed", managed_provider="mistral")
    assert page.row_cloud.model_label.text() == "Fast"
    assert page.runs_on_text() == "Runs on Gusan Cloud · Fast"


def test_cloud_toggle_goes_back_without_a_controller(dict_page):
    page, app, _ = dict_page("pro")
    del app.set_settings                                   # nothing can apply it
    page.cloud.click()
    QApplication.processEvents()
    assert not page.cloud.isChecked()


# ── the capture button on its own ────────────────────────────────────────────
def test_hotkey_capture_button(qapp, made):
    from ui.components.hotkey_capture import HotkeyCaptureButton
    btn = HotkeyCaptureButton("alt+r", accessible_name="Dictation shortcut")
    made.append(btn)
    btn.show()
    got = []
    btn.captured.connect(lambda c: got.append(("captured", c)))
    btn.refused.connect(lambda m: got.append(("refused", m)))
    btn.cancelled.connect(lambda: got.append(("cancelled",)))
    assert btn.keys.accessibleName() == "Alt + R"
    assert btn.button.text() == "Change"
    assert "Alt + R" in btn.button.accessibleName()

    QTest.keyClick(btn.button, Qt.Key_Return)                    # Enter on Change starts it (as Space does)
    assert btn.is_capturing()
    QTest.mouseClick(btn.button, Qt.LeftButton)                  # the mouse on Cancel stops it (and stays stopped)
    QApplication.processEvents()
    assert not btn.is_capturing() and got == [("cancelled",)]
    got.clear()

    btn.start_capture()
    assert btn.button.text() == "Cancel" and not btn.prompt_label.isHidden()
    QTest.keyClick(btn.button, Qt.Key_Alt, Qt.AltModifier)       # a modifier alone: keep waiting
    QTest.keyClick(btn.button, Qt.Key_Space)                     # a typing key alone: refused
    QTest.keyClick(btn.button, Qt.Key_F9)                        # an F key alone is fine
    assert got == [("refused", "Add Ctrl, Alt, Shift or Win."), ("captured", "f9")]
    assert btn.hotkey() == "f9" and btn.keys.accessibleName() == "F9"

    got.clear()
    btn.start_capture()
    QTest.keyClick(btn.button, Qt.Key_Escape)
    assert got == [("cancelled",)] and btn.hotkey() == "f9"

    got.clear()
    btn.start_capture()
    QTest.mouseClick(btn.button, Qt.MiddleButton)
    QApplication.processEvents()
    assert got == [("captured", "mouse:middle")]

    btn.set_hotkey("")
    assert not btn.empty_label.isHidden() and btn.keys.isHidden()
