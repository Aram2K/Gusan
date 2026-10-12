"""The Meetings page (ui/pages/meetings.py + meeting_views.py), offscreen.

Every view builds for Guest / Free / Pro without an error card, the Pro gate,
consent and the session calls happen in that order, settings apply through
app.set_setting, the calendar parts follow CalendarService, and a recording is
never stopped by leaving the page. One test drives a real MeetingSession from
Record to the notes."""
import json
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest
from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (QAbstractButton, QApplication, QComboBox, QLineEdit, QPlainTextEdit,
                               QTextBrowser)

from tests_qt.conftest import make_stub_app


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

    def build(tier="pro", **attrs):
        app = make_stub_app(tier)
        for k, v in attrs.items():
            setattr(app, k, v)
        win = shell_mod.MainWindow(app)
        made.append(win)
        return win, app

    yield build
    for w in made:
        w.hide()
        w.deleteLater()
    QApplication.processEvents()


NOTES = """## Summary
Live Assistant launches on 14 October.

## Decisions
- Launch on 14 October.

## Action items
- [ ] Fix speaker-label drift (Owner: David, Due: Wed)
- [ ] Confirm the date with marketing (Owner: Aram)

## Open questions
- Smart Actions on by default?
"""


def _meeting(root, name, title, notes=NOTES, transcript="Speaker 1: Hello team.", seen=False, **meta):
    d = root / name
    d.mkdir()
    started = "%s-%s-%s %s:%s:00" % (name[:4], name[4:6], name[6:8], name[9:11], name[11:13])
    data = {"title": title, "attendees": "Anna, David", "duration_sec": 2520, "started": started,
            "language": "en", "notes_engine_label": "Gusan Cloud · Mistral Small", "seen": seen}
    data.update(meta)
    (d / "meta.json").write_text(json.dumps(data), encoding="utf-8")
    if notes:
        (d / "notes.md").write_text(notes, encoding="utf-8")
    if transcript:
        (d / "transcript.txt").write_text(transcript, encoding="utf-8")
    return d


class FakeSession(QObject):
    state_changed = Signal(str)
    chunk_text = Signal(str)
    live_line = Signal(str, str)
    recap = Signal(str)
    status = Signal(str)
    levels = Signal(float, float)
    finished = Signal(str, str, str)
    error = Signal(str, str)

    def __init__(self, phase="idle", start_result=(True, "", "")):
        super().__init__()
        self.phase = phase
        self.title = "Q4 roadmap"
        self.attendees = "Anna, David"
        self.my_notes = ""
        self.transcript = ""
        self.last_error = ""
        self._live_summary_text = ""
        self.meeting_dir = None
        self.start_result = start_result
        self.calls = []

    @property
    def is_busy(self):
        return self.phase in ("recording", "paused", "processing")

    def active_dir(self):
        return self.meeting_dir if self.is_busy else None

    def elapsed_sec(self, now=None):
        return 75

    def live_lines(self):
        return []

    def snapshot(self):
        return {"phase": self.phase, "language": "en", "audio_mode": "smart_meeting"}

    def _set(self, phase):
        self.phase = phase
        self.state_changed.emit(phase)

    def start(self, **kw):
        self.calls.append(("start", kw))
        if self.start_result[0]:
            self.title = kw.get("title") or self.title
            self._set("recording")
        return self.start_result

    def pause(self):
        self.calls.append(("pause",))
        self._set("paused")
        return True

    def resume(self):
        self.calls.append(("resume",))
        self._set("recording")
        return True

    def stop(self, user_notes=None, notes_engine=None):
        self.calls.append(("stop", user_notes))
        self._set("processing")
        return True

    def update_my_notes(self, text):
        self.my_notes = text

    def reset(self):
        self.calls.append(("reset",))
        self.phase = "idle"
        return True

    def retry_notes(self, engine=None, folder=None, persist=False):
        self.calls.append(("retry_notes", engine, folder))
        return True

    def continue_meeting(self, folder, **kw):
        self.calls.append(("continue_meeting", str(folder)))
        self._set("recording")
        return True, "", ""


class FakeCalendar(QObject):
    changed = Signal(object)
    connected = Signal(str)
    disconnected = Signal(str)
    error = Signal(str)
    remind = Signal(object)
    connecting = Signal(bool)

    def __init__(self, connected=True, available=True):
        super().__init__()
        self.available = available
        self.is_connected = connected
        self.is_connecting = False
        self.email = "aram@aibuben.xyz" if connected else ""
        self.calls = []
        now = datetime.now(timezone.utc)
        self.rows = [
            {"id": "e1", "title": "Weekly product sync", "start": now + timedelta(seconds=50),
             "end": now + timedelta(minutes=31), "call_type": "Google Meet", "people": "Anna, David +2",
             "others": ["Anna", "David", "Mariam", "Lilit"], "link": "https://meet.google.com/x", "muted": False},
            {"id": "e2", "title": "Customer call", "start": now + timedelta(hours=2),
             "end": now + timedelta(hours=2, minutes=30), "call_type": "Zoom", "people": "Mariam +1",
             "others": ["Mariam", "Tom"], "link": "https://zoom.us/j/1", "muted": False},
            {"id": "e3", "title": "1:1 with David", "start": now + timedelta(hours=3),
             "end": now + timedelta(hours=3, minutes=30), "call_type": "", "people": "David",
             "others": ["David"], "link": "", "muted": True},
        ]

    def meetings(self):
        return [dict(m) for m in self.rows] if self.is_connected else []

    def current_meeting(self):
        return dict(self.rows[0]) if self.is_connected else None

    def refresh(self, force=False):
        self.calls.append(("refresh",))
        return False

    def set_muted(self, event_id, muted):
        self.calls.append(("set_muted", event_id, muted))
        return True

    def connect_calendar(self):
        self.calls.append(("connect",))
        self.is_connecting = True
        self.connecting.emit(True)
        return True

    def cancel_connect(self):
        self.calls.append(("cancel",))
        self.is_connecting = False
        self.connecting.emit(False)

    def disconnect_calendar(self, reason="user"):
        self.calls.append(("disconnect",))
        self.is_connected = False
        self.changed.emit([])
        self.disconnected.emit(reason)


def _open(win, **opts):
    page = win.open("meetings", **opts)
    QApplication.processEvents()
    return page


def _unnamed(page):
    bad = []
    for b in page.findChildren(QAbstractButton):
        if not (b.text() or "").strip() and not (b.accessibleName() or "").strip():
            bad.append("%s %r" % (type(b).__name__, b.toolTip()))
    for w in page.findChildren(QComboBox) + page.findChildren(QPlainTextEdit) + page.findChildren(QTextBrowser):
        if not (w.accessibleName() or "").strip():
            bad.append(type(w).__name__)
    for w in page.findChildren(QLineEdit):
        if isinstance(w.parent(), QComboBox):
            continue
        if not (w.accessibleName() or "").strip():
            bad.append("QLineEdit %r" % w.placeholderText())
    return bad


# ── building ─────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("tier", ["guest", "free", "pro"])
def test_the_page_builds_for_every_plan(shell, meetings_root, tier):
    from ui.pages.meetings import MeetingsPage
    from ui.shell import ErrorPage
    win, app = shell(tier)
    page = _open(win)
    assert isinstance(page, MeetingsPage) and not isinstance(page, ErrorPage)
    assert page.wants_scroll is False
    assert page.current_view() == "list"
    lv = page.list_view
    assert lv.header.title_label.text() == "Meetings"
    # No meetings yet: the one Example card, no search box.
    assert lv.meetings.example.isVisibleTo(page) and not lv.meetings.search.isVisibleTo(page)
    # No calendar service in this app: every calendar part is hidden.
    assert not lv.connect_card.isVisibleTo(page) and not lv.coming_up.isVisibleTo(page)
    assert not lv.rail.cal_row.isVisibleTo(page)
    # Calm Pro: the PRO tag on Record a meeting for everyone; the default name.
    assert lv.start_card.record_btn.pro_tag.text() == "PRO"
    assert lv.start_card.title_edit.text().startswith("Meeting · ")
    assert lv.rail.title_label.text() == "Meeting settings"
    assert "this computer" in lv.rail.runs_label.text()


def test_every_view_has_named_controls(shell, meetings_root):
    folder = _meeting(meetings_root, "20260929_110000", "Q4 roadmap")
    session = FakeSession("recording")
    win, _app = shell("pro", meeting_session=session, calendar=FakeCalendar(True))
    page = _open(win)
    assert page.current_view() == "live"                    # a recording opens the live view
    page.show_list(refresh=True)
    page.open_notes(str(folder))
    assert page.current_view() == "notes"
    assert _unnamed(page) == []


# ── Pro gate, consent, the session ───────────────────────────────────────────
def test_free_record_shows_the_pro_window(shell, meetings_root):
    win, app = shell("free")
    page = _open(win)
    page.list_view.start_card.record_btn.click()
    assert ("_pro_upsell", "Meeting recording") in app.calls
    assert getattr(app, "meeting_session", None) is None
    assert page.current_view() == "list"


def test_consent_refused_never_starts(shell, meetings_root, monkeypatch):
    from ui.pages import meetings as page_mod
    monkeypatch.setattr(page_mod, "_ensure_consent", lambda parent, app: False)
    session = FakeSession()
    win, _app = shell("pro", meeting_session=session)
    page = _open(win)
    page.list_view.start_card.record_btn.click()
    assert session.calls == []
    assert page.current_view() == "list"


def test_a_refused_start_shows_why_on_the_card(shell, meetings_root):
    session = FakeSession(start_result=(False, "file_job", "A file is being converted right now."))
    win, _app = shell("pro", meeting_session=session)
    page = _open(win)
    card = page.list_view.start_card
    card.record_btn.click()
    assert card.error_label.text() == "A file is being converted right now."
    assert card.error_label.isVisibleTo(page) and page.current_view() == "list"


def test_record_from_is_for_one_meeting_then_both_sides_again(shell, meetings_root, monkeypatch):
    import meeting_session as msess
    from ui.pages import meetings as mp
    monkeypatch.setattr(msess, "has_loopback", lambda: True)     # this system hears the call
    session = FakeSession()
    win, app = shell("pro", meeting_session=session)
    app.cfg["meeting_audio_mode"] = "default_mic"                # an old 1.x pick
    page = _open(win)
    card = page.list_view.start_card
    card.sync_settings()
    assert mp._default_mode() == "smart_meeting"
    assert card.audio_mode() == "smart_meeting"                  # a saved pick is ignored
    i = card.mode_combo.findData("default_mic")
    card.mode_combo.setCurrentIndex(i)
    card.mode_combo.activated.emit(i)
    card.record_btn.click()
    name, kw = session.calls[0]
    assert name == "start" and kw["audio_mode"] == "default_mic"    # this meeting only
    assert not [c for c in app.calls if c[:2] == ("set_setting", "meeting_audio_mode")]
    assert card.audio_mode() == "smart_meeting"                  # the next one: Both sides


def test_record_follow_and_never_stop_on_leaving(shell, meetings_root):
    session = FakeSession()
    win, app = shell("pro", meeting_session=session)
    win.show()
    page = _open(win)
    card = page.list_view.start_card
    card.title_edit.setText("Q4 roadmap")
    card.people.input.setText("Anna, David")
    card.people.commit()
    assert card.people.names() == ["Anna", "David"]
    card.record_btn.click()
    name, kw = session.calls[0]
    assert name == "start"
    assert kw["title"] == "Q4 roadmap" and kw["attendees"] == "Anna, David"
    assert kw["source"] == "page" and kw["calendar_event_id"] is None
    assert kw["audio_mode"] == card.mode_combo.currentData()
    assert page.current_view() == "live"
    live = page.live_view
    assert live.header.title_label.text() == "Q4 roadmap"
    assert live.header.sub_label.text() == "Anna, David · English"
    assert live.state_label.text() == "RECORDING"
    # The session's signals reach the view.
    session.live_line.emit("Hello team.", "You")
    assert live.transcript.lines()[-1] == ("You", "Hello team.")
    session.recap.emit("- We ship Friday.")
    assert live.so_far.text() == "- We ship Friday."
    session.levels.emit(0.8, 0.25)
    assert live.mic_meter.level() == pytest.approx(0.8) and live.call_meter.level() == pytest.approx(0.25)
    live.my_notes.setPlainText("Ask Anna")
    assert session.my_notes == "Ask Anna"
    # Pause / Resume
    live.pause_btn.click()
    assert session.phase == "paused" and live.pause_btn.text() == "Resume"
    assert live.state_label.text() == "PAUSED"
    live.pause_btn.click()
    assert session.phase == "recording" and live.pause_btn.text() == "Pause"
    # Leaving the page or closing the window never stops it.
    win.open("home")
    win.close()
    QApplication.processEvents()
    assert not any(c[0] in ("stop", "reset") for c in session.calls)
    win.show()
    assert win.open("meetings") is page and page.current_view() == "live"
    # Live Assistant: Pro -> the card.
    live.assist_btn.click()
    assert ("toggle_live_assist",) in app.calls
    # Stop and write notes -> processing -> the notes.
    live.stop_btn.click()
    assert ("stop", "Ask Anna") in session.calls
    assert live.status_box.isVisibleTo(page) and not live.stop_btn.isVisibleTo(page)
    assert live.state_label.text() == "WRITING NOTES"
    session.status.emit("Transcribing…")
    assert live.status_label.text() == "Transcribing…"
    folder = _meeting(meetings_root, "20261007_143000", "Q4 roadmap")
    session.phase = "done"
    session.finished.emit(str(folder), NOTES, "")
    assert page.current_view() == "notes" and page.notes_view.folder == str(folder)


def test_open_options_prefill_and_record(shell, meetings_root):
    session = FakeSession()
    win, _app = shell("pro", meeting_session=session)
    page = _open(win, record=True, title="Standup", attendees=["Anna"], calendar_event_id="e9")
    kw = session.calls[0][1]
    assert kw["title"] == "Standup" and kw["attendees"] == "Anna"
    assert kw["source"] == "calendar" and kw["calendar_event_id"] == "e9"
    assert page.current_view() == "live"
    # After a start the card is fresh again.
    assert page.list_view.start_card.calendar_event_id == ""
    assert page.list_view.start_card.people.names() == []


def test_a_real_session_from_record_to_notes(shell, meetings_root, monkeypatch):
    import actions
    import meeting_session
    monkeypatch.setattr(meeting_session.telemetry, "track", lambda *a, **k: None)
    monkeypatch.setattr(meeting_session.actions, "process",
                        lambda text, mode, **kw: "## Summary\n- We ship Friday.")

    class Rec:
        _full_audio_truncated = False
        _full_audio_max = 16000 * 60 * 120
        _loopback_peak = 1.0
        on_chunk_complete = on_levels = on_partial = on_lang_detected = on_source_levels = None
        fast_live_chunks = False
        partial_text = ""
        paused = False

        def start_recording(self, **kw):
            self.started = kw

        def stop_recording(self):
            pass

        def transcribe(self):
            return ("Hello team. We ship Friday.", "en")

        def get_full_audio(self):
            return None

        def set_paused(self, on):
            self.paused = bool(on)

        def chunk_source_energy(self, idx):
            return None

    win, app = shell("pro")
    app.cfg.update(meeting_consent_ack=True, save_history=False, meeting_audio_mode="default_mic",
                   action_model=actions.API_MANAGED_ID)
    app.recorder = Rec()
    app.live_assist = None
    app.save_config = lambda: None
    app.release_models_after_session = lambda: None
    app._resolve_action_engine = lambda action_model=None: (
        action_model or app.cfg["action_model"], {**app.cfg, "_managed_token": "tok"})
    page = _open(win)
    page.list_view.start_card.title_edit.setText("Weekly sync")
    page.list_view.start_card.record_btn.click()
    s = app.meeting_session
    assert isinstance(s, meeting_session.MeetingSession)
    assert s.phase == "recording" and page.current_view() == "live"
    t = threading.Thread(target=app.recorder.on_chunk_complete, args=(0, "Hello team.", "en"))
    t.start()
    t.join(5)
    assert _wait_for(lambda: page.live_view.transcript.lines() == [("", "Hello team.")])
    page.live_view.pause_btn.click()
    assert s.phase == "paused" and app.recorder.paused is True
    page.live_view.pause_btn.click()
    assert s.phase == "recording"
    page.live_view.my_notes.setPlainText("Ask Anna")
    page.live_view.stop_btn.click()
    assert _wait_for(lambda: page.current_view() == "notes", timeout=10)
    nv = page.notes_view
    assert nv.data["title"] == "Weekly sync"
    assert "We ship Friday" in nv.data["notes"] and nv.data["my_notes"] == "Ask Anna"
    assert nv.title_label.text() == "Weekly sync"
    page.show_list(refresh=True)
    rows = page.list_view.meetings.rows
    assert [r.meeting["title"] for r in rows] == ["Weekly sync"]
    s.deleteLater()


# ── settings ─────────────────────────────────────────────────────────────────
def test_settings_apply_through_set_setting(shell, meetings_root):
    win, app = shell("pro")
    page = _open(win)
    rail, card = page.list_view.rail, page.list_view.start_card
    # "Record from" is not a saved setting: no rail row, and a start-card pick
    # is for that meeting only.
    assert not hasattr(rail, "mode_combo")
    assert "Record from" not in [r.label.text() for r in rail.rows()]
    last = card.mode_combo.count() - 1
    card.mode_combo.setCurrentIndex(last)
    card.mode_combo.activated.emit(last)
    assert not [c for c in app.calls if c[:2] == ("set_setting", "meeting_audio_mode")]
    assert card.audio_mode() == card.mode_combo.itemData(last)
    card.sync_settings()                                   # another setting changed: pick kept
    assert card.audio_mode() == card.mode_combo.itemData(last)
    i = card.lang_combo.findData("en")
    card.lang_combo.setCurrentIndex(i)
    card.lang_combo.activated.emit(i)
    assert ("set_setting", "meeting_language", "en") in app.calls
    rail.opt_local.radio.click()
    assert ("set_setting", "meeting_notes_model", "qwen_tiny") in app.calls
    rail.opt_cloud.radio.click()
    assert ("set_setting", "meeting_notes_model", "managed") in app.calls
    rail.consent_toggle.click()
    assert ("set_setting", "meeting_consent_reminder", False) in app.calls
    # Another page (or the tray) changed them: everything follows.
    app.cfg.update(meeting_consent_reminder=False, meeting_language="fr", meeting_notes_model="qwen_tiny")
    page.on_setting_changed("meeting_consent_reminder")
    assert card.consent_row.isHidden() and card.consent_divider.isHidden()
    assert card.lang_combo.currentData() == "fr" and rail.lang_combo.currentData() == "fr"
    assert rail.opt_local.radio.isChecked()
    app.cfg["whisper_model"] = "small"
    page.on_setting_changed("whisper_model")
    assert rail.runs_label.text() == "Runs on Balanced · this computer"
    assert rail.runs_model.text() == "Whisper Small"
    rail.change_btn.click()
    assert win.current_route() == "settings"


def test_free_users_keep_notes_on_this_computer(shell, meetings_root):
    win, app = shell("free")
    page = _open(win)
    rail = page.list_view.rail
    assert rail.opt_local.radio.isChecked()
    rail.opt_cloud.radio.click()
    assert ("_pro_upsell", "Meeting recording") in app.calls
    assert rail.opt_local.radio.isChecked()
    assert not any(c[:2] == ("set_setting", "meeting_notes_model") for c in app.calls)


def test_copy_notice(shell, meetings_root):
    from ui.pages.meeting_views import NOTICE_TEXT
    win, _app = shell("pro")
    page = _open(win)
    page.list_view.start_card.copy_notice_btn.click()
    assert QGuiApplication.clipboard().text() == NOTICE_TEXT == \
        "Heads up: I'm using Gusan to take notes on this call."


# ── Google Calendar ──────────────────────────────────────────────────────────
def test_calendar_not_connected(shell, meetings_root):
    cal = FakeCalendar(connected=False)
    win, app = shell("free", calendar=cal)
    page = _open(win)
    lv = page.list_view
    assert lv.connect_card.isVisibleTo(page) and not lv.coming_up.isVisibleTo(page)
    assert lv.rail.cal_row.isVisibleTo(page) and lv.rail.cal_connect.isVisibleTo(page)
    assert not lv.rail.ask_row.isVisibleTo(page)
    assert ("refresh",) in cal.calls
    lv.connect_card.connect_btn.click()
    assert ("connect",) in cal.calls
    assert lv.connect_card.cancel_btn.isVisibleTo(page)
    assert lv.rail.cal_row.hint_label.text() == "Waiting for Google…"
    lv.connect_card.cancel_btn.click()
    assert ("cancel",) in cal.calls and lv.connect_card.connect_btn.isVisibleTo(page)
    cal.error.emit("Couldn't connect Google Calendar - try again.")
    assert lv.connect_card.sub.text() == "Couldn't connect Google Calendar - try again."
    # Connected later: Coming up appears, the card goes.
    cal.is_connected, cal.email = True, "aram@aibuben.xyz"
    cal.connected.emit("aram@aibuben.xyz")
    assert lv.coming_up.isVisibleTo(page) and not lv.connect_card.isVisibleTo(page)
    assert len(lv.coming_up.rows) == 3


def test_calendar_connected(shell, meetings_root):
    cal = FakeCalendar(connected=True)
    win, app = shell("pro", calendar=cal)
    page = _open(win)
    lv = page.list_view
    rows = lv.coming_up.rows
    assert [r.meeting["id"] for r in rows] == ["e1", "e2", "e3"]
    assert rows[0].property("next") is True and rows[1].property("next") is False
    assert rows[0].rel_label.text() == "in 1 min"
    assert [r.bell.isChecked() for r in rows] == [True, True, False]
    rows[0].bell.click()
    assert ("set_muted", "e1", True) in cal.calls
    rows[2].bell.click()
    assert ("set_muted", "e3", False) in cal.calls
    # The start card is filled from the meeting on now.
    card = lv.start_card
    assert card.title_edit.text() == "Weekly product sync"
    assert card.calendar_event_id == "e1" and card.cal_sub.isVisibleTo(page)
    assert card.people.names() == ["Anna", "David", "Mariam", "Lilit"]
    assert card.people.more.text() == "+2"
    # The rail: account, Ask me to record, Only meetings with a call link.
    rail = lv.rail
    assert rail.cal_row.hint_label.text() == "aram@aibuben.xyz"
    assert rail.ask_row.isVisibleTo(page) and rail.link_row.isVisibleTo(page)
    rail.ask_toggle.click()
    assert ("set_setting", "calendar_ask_to_record", False) in app.calls
    rail.link_toggle.click()
    assert ("set_setting", "calendar_only_with_link", False) in app.calls
    rail.cal_disconnect.click()
    assert ("disconnect",) in cal.calls
    assert not lv.coming_up.isVisibleTo(page) and lv.connect_card.isVisibleTo(page)
    assert card.title_edit.text().startswith("Meeting · ") and card.calendar_event_id == ""


def test_a_bell_change_keeps_the_rows(shell, meetings_root, monkeypatch):
    from ui.pages import meetings as page_mod
    fixed = datetime.now(timezone.utc)
    monkeypatch.setattr(page_mod, "_utcnow", lambda: fixed)     # no minute rollover mid-test
    cal = FakeCalendar(connected=True)
    win, _app = shell("pro", calendar=cal)
    page = _open(win)
    rows = list(page.list_view.coming_up.rows)
    cal.rows[0]["muted"] = True                  # the service re-emits the list after set_muted
    cal.changed.emit(cal.meetings())
    assert page.list_view.coming_up.rows == rows   # same widgets: the toggled bell keeps focus
    assert [r.bell.isChecked() for r in rows] == [False, True, False]


def test_calendar_hidden_without_a_google_client(shell, meetings_root):
    cal = FakeCalendar(connected=False, available=False)
    win, _app = shell("pro", calendar=cal)
    page = _open(win)
    lv = page.list_view
    assert not lv.connect_card.isVisibleTo(page) and not lv.coming_up.isVisibleTo(page)
    assert not lv.rail.cal_row.isVisibleTo(page)


# ── the list and the notes view ──────────────────────────────────────────────
def test_list_rows_search_and_notes(shell, meetings_root):
    first = _meeting(meetings_root, "20260929_110000", "Q4 roadmap", seen=False)
    _meeting(meetings_root, "20260928_100000", "Weekly product sync", seen=True)
    win, app = shell("free")
    page = _open(win)
    ml = page.list_view.meetings
    assert [r.meeting["title"] for r in ml.rows] == ["Q4 roadmap", "Weekly product sync"]
    assert not ml.example.isVisibleTo(page) and ml.search.isVisibleTo(page)
    assert ml.rows[0].accessibleName() == "Q4 roadmap, Tue 29 Sep · 11:00, 42 min, Notes ready"
    assert "Notes ready" not in ml.rows[1].accessibleName()
    ml.search.setText("weekly")
    ml._run_search()
    assert [r.meeting["title"] for r in ml.rows] == ["Weekly product sync"]
    ml.search.setText("")
    ml._run_search()
    ml.rows[0].click()
    assert page.current_view() == "notes"
    nv = page.notes_view
    assert nv.folder == str(first) and nv.title_label.text() == "Q4 roadmap"
    assert [t.owner_shown() for t in nv.doc.tasks] == ["David", "You"]
    assert nv.doc.tasks[0].due == "by Wed"
    assert json.loads((first / "meta.json").read_text(encoding="utf-8"))["seen"] is True
    # An action item ticked rewrites only its line.
    nv.doc.tasks[1].box.click()
    text = (first / "notes.md").read_text(encoding="utf-8")
    assert "- [x] Confirm the date with marketing (Owner: Aram)" in text
    assert "- [ ] Fix speaker-label drift" in text
    nv.copy_notes()
    # D9: nice plain text (no ## or - [ ]) + HTML with real headings and lists.
    copied = QGuiApplication.clipboard().text()
    assert copied.startswith("Summary\nLive Assistant launches on 14 October.")
    assert "☐ Fix speaker-label drift (David · by Wed)" in copied and "##" not in copied
    mime = QGuiApplication.clipboard().mimeData()
    assert mime.hasHtml() and "<h3>Summary</h3>" in mime.html()
    exports = [a.text().split("\t")[0] for a in nv.export_menu.actions()]
    assert exports == ["Word", "Markdown", "Notion", "Audio"]
    more = [a.text() for a in nv.more_menu.actions() if not a.isSeparator()]
    assert more == ["Rewrite notes", "Rename", "Show in folder", "Delete"]
    nv.tabs.set_index(1, emit=True)
    assert nv.stack.currentWidget() is nv.transcript and "Hello team." in nv.transcript.toPlainText()
    # Free: Continue recording is a Pro action.
    nv.continue_btn.click()
    assert ("_pro_upsell", "Meeting recording") in app.calls
    nv.back_link.click()
    assert page.current_view() == "list"
    assert "Notes ready" not in ml.rows[0].accessibleName()


def test_continue_recording_for_pro(shell, meetings_root):
    folder = _meeting(meetings_root, "20260929_110000", "Q4 roadmap")
    session = FakeSession()
    win, _app = shell("pro", meeting_session=session)
    page = _open(win, folder=str(folder))
    assert page.current_view() == "notes"
    page.notes_view.continue_btn.click()
    assert ("continue_meeting", str(folder)) in session.calls
    assert page.current_view() == "live"


def test_failed_notes_banner_writes_them_again(shell, meetings_root):
    folder = _meeting(meetings_root, "20260930_090000", "Design review", notes="",
                      notes_error="Gusan Cloud didn't answer.")
    session = FakeSession()
    win, _app = shell("pro", meeting_session=session)
    page = _open(win)
    row = page.list_view.meetings.rows[0]
    assert "No notes" in row.accessibleName()
    row.click()
    nv = page.notes_view
    banner = nv.banner
    assert banner.isVisibleTo(page) and banner.accessibleName() == "Notes couldn't be written."
    assert not nv.copy_btn.isEnabled()
    banner.button.click()
    assert ("retry_notes", None, str(folder)) in session.calls


def test_notes_a_quit_left_for_later_can_be_written_now(shell, meetings_root):
    # Gusan quit (or Windows logged off) mid-meeting: the recording and the
    # transcript are saved, meta notes_pending - the next launch writes the
    # notes, and until then the notes view offers it.
    folder = _meeting(meetings_root, "20261008_101500", "Late call", notes="", notes_pending=True)
    session = FakeSession()
    win, _app = shell("pro", meeting_session=session)
    page = _open(win, folder=str(folder))
    nv = page.notes_view
    banner = nv.banner
    assert banner.isVisibleTo(page) and banner.accessibleName() == "Notes not written yet."
    assert banner.button.text() == "Write notes"
    banner.button.click()
    assert ("retry_notes", None, str(folder)) in session.calls
    # A continued meeting with earlier notes: those stay, the new part is pending.
    other = _meeting(meetings_root, "20261008_120000", "Board", notes_pending=True)
    page = _open(win, folder=str(other))
    assert page.notes_view.banner.accessibleName() == "Notes not updated yet."


LONG_TALK = " ".join("Sentence number %d is about the launch plan and who does what." % i for i in range(40))


def test_no_notes_and_no_error_is_a_calm_line(shell, meetings_root):
    # D11: e.g. Gusan quit while it was transcribing - nothing failed, so no
    # amber "Notes couldn't be written", just "No notes yet" + Write notes.
    folder = _meeting(meetings_root, "20261008_101500", "Late call", notes="",
                      transcript="Speaker 1: " + LONG_TALK)
    session = FakeSession()
    win, _app = shell("pro", meeting_session=session)
    page = _open(win, folder=str(folder))
    nv = page.notes_view
    banner = nv.banner
    assert banner.isVisibleTo(page) and banner.accessibleName() == "No notes yet"
    assert banner.property("tone") == "neutral"
    assert banner.button.text() == "Write notes"
    banner.button.click()
    assert ("retry_notes", None, str(folder)) in session.calls
    # The transcript shown instead reads in paragraphs, not one wall of text.
    from PySide6.QtWidgets import QLabel
    shown = [w.text() for w in nv.doc.findChildren(QLabel) if "Sentence number" in w.text()]
    assert len(shown) >= 3 and "<b" in shown[0] and "Speaker 1" in shown[0]
    assert all(len(t) < 900 for t in shown)
    # No transcript either: a calm line too.
    other = _meeting(meetings_root, "20261008_120000", "Empty", notes="", transcript="")
    page = _open(win, folder=str(other))
    assert page.notes_view.banner.accessibleName() == "Nothing to show yet."
    assert page.notes_view.banner.property("tone") == "neutral"


def test_the_transcript_reads_in_paragraphs(shell, meetings_root):
    # D10: a meeting saved as one long line (old ones too) shows readable
    # paragraphs; speaker labels and "— marker —" lines stay.
    text = "Speaker 1: " + LONG_TALK + "\n— Resumed at 11:20 —\nSpeaker 2: Short answer."
    folder = _meeting(meetings_root, "20260929_110000", "Q4 roadmap", transcript=text)
    win, _app = shell("pro")
    page = _open(win, folder=str(folder))
    nv = page.notes_view
    doc = nv.transcript.document()
    blocks = [doc.findBlockByNumber(i).text() for i in range(doc.blockCount())]
    blocks = [b for b in blocks if b.strip()]
    assert len(blocks) >= 5
    assert blocks[0].startswith("Speaker 1") and not blocks[1].startswith("Speaker")
    assert "— Resumed at 11:20 —" in blocks and blocks[-1].startswith("Speaker 2")
    assert all(len(b) <= 600 for b in blocks)
    from ui.pages import meeting_views as v
    rows = v.transcript_rows(text)
    assert rows[0][0] == "Speaker 1" and rows[1][0] == ""
    assert ("", "— Resumed at 11:20 —", True) in rows and rows[-1] == ("Speaker 2", "Short answer.", False)
    # Every word is kept, in order.
    assert " ".join(r[1] for r in rows if r[0] == "Speaker 1" or (r[0] == "" and not r[2]))[:40] == LONG_TALK[:40]


def test_word_export_has_real_headings_and_items(shell, meetings_root, tmp_path, monkeypatch):
    import zipfile
    from ui.pages import meeting_views as v
    folder = _meeting(meetings_root, "20260929_110000", "Q4 roadmap",
                      transcript="Speaker 1: Hello team.\n— Resumed at 11:20 —")
    win, _app = shell("pro")
    page = _open(win, folder=str(folder))
    out = tmp_path / "notes.docx"
    monkeypatch.setattr(v.QFileDialog, "getSaveFileName", staticmethod(lambda *a, **k: (str(out), "")))
    page.notes_view.export("word")
    assert _wait_for(out.exists)
    with zipfile.ZipFile(out) as z:
        xml = z.read("word/document.xml").decode("utf-8")
    assert "##" not in xml and "- [ ]" not in xml
    # "Summary" is a bold 14 pt heading, the tasks are indented ☐ items with the owner muted.
    assert '<w:sz w:val="28"/><w:szCs w:val="28"/><w:b/></w:rPr><w:t xml:space="preserve">Summary<' in xml
    assert ">☐<" in xml and "w:hanging=" in xml and "(David · by Wed)" in xml
    assert ">Speaker 1:  <" in xml and ">Hello team.<" in xml
    assert "<w:i/>" in xml and "— Resumed at 11:20 —" in xml
    paras = v.word_paragraphs(NOTES, "Speaker 1: Hello team.")
    assert [p.get("style") for p in paras][:4] == ["heading", "body", "heading", "bullet"]
    assert paras[-2:] == [{"style": "heading", "text": "Transcript"},
                          {"start": 0, "speaker": None, "label": "Speaker 1", "text": "Hello team."}]


def test_my_notes_rename_and_delete(shell, meetings_root):
    folder = _meeting(meetings_root, "20260929_110000", "Q4 roadmap")
    win, _app = shell("pro")
    page = _open(win, folder=str(folder))
    nv = page.notes_view
    nv.my_notes.setPlainText("Call the printer")
    nv._save_my_notes()
    assert (folder / "my_notes.md").read_text(encoding="utf-8") == "Call the printer"
    nv.apply_rename("Roadmap  review")
    assert nv.title_label.text() == "Roadmap review"
    assert json.loads((folder / "meta.json").read_text(encoding="utf-8"))["title"] == "Roadmap review"
    nv.apply_delete()
    assert not folder.exists()
    assert page.current_view() == "list" and page.list_view.meetings.rows == []


def test_a_reload_keeps_my_notes_typed_a_moment_ago(shell, meetings_root):
    folder = _meeting(meetings_root, "20260929_110000", "Q4 roadmap")
    win, _app = shell("pro")
    page = _open(win, folder=str(folder))
    nv = page.notes_view
    nv.my_notes.setPlainText("Call the printer")
    assert nv._notes_save.isActive()            # still waiting on the debounce
    page.on_open()                               # e.g. the sidebar: the notes reload from disk
    assert (folder / "my_notes.md").read_text(encoding="utf-8") == "Call the printer"
    assert nv.my_notes.toPlainText() == "Call the printer"


def test_a_late_search_never_replaces_a_newer_one(shell, meetings_root):
    _meeting(meetings_root, "20260929_110000", "Q4 roadmap")
    _meeting(meetings_root, "20260928_100000", "Weekly product sync")
    win, _app = shell("pro")
    page = _open(win)
    ml = page.list_view.meetings
    held = []
    ml._bg.run = lambda fn, cb=None: held.append((fn, cb))
    ml.search.setText("roadmap")
    ml._run_search()
    ml.search.setText("weekly")
    ml._run_search()
    for fn, cb in reversed(held):                # the newer one lands first, the older one late
        cb(fn(), None)
    assert [r.meeting["title"] for r in ml.rows] == ["Weekly product sync"]


def test_a_notes_rewrite_stays_on_the_notes(shell, meetings_root):
    folder = _meeting(meetings_root, "20260929_110000", "Q4 roadmap")
    session = FakeSession("processing")
    session.meeting_dir = str(folder)
    session._rewrite_only = True                 # "⋯ Rewrite notes" of a saved meeting
    win, _app = shell("pro", meeting_session=session)
    page = _open(win)
    assert page.current_view() == "list"         # nothing records: no live view
    strip = page.list_view.live_strip
    assert strip.isVisibleTo(page)
    strip.open_btn.click()
    assert page.current_view() == "notes" and page.notes_view.folder == str(folder)
    assert page.notes_view.banner.accessibleName() == "Writing notes…"
    assert all(not t.box.isEnabled() for t in page.notes_view.doc.tasks)
    page.show_list()
    page.list_view.meetings.rows[0].click()
    assert page.current_view() == "notes"
    # A just-recorded meeting's notes being written: the live view, as before.
    session._rewrite_only = False
    page.open_notes(str(folder))
    assert page.current_view() == "live"


class FakeDownloads(QObject):
    progress = Signal(str, int, object, object)
    state_changed = Signal(str, str)
    finished = Signal(str, str, str)

    def __init__(self):
        super().__init__()
        self.states, self.pct, self.started = {}, {}, []

    def state(self, name):
        return self.states.get(name, "missing")

    def progress_of(self, name):
        return (self.pct.get(name, 0), 0, 0)

    def start(self, name):
        self.started.append(name)
        self.states[name] = "downloading"
        return True


def test_the_local_notes_model_downloads_in_view(shell, meetings_root):
    dl = FakeDownloads()
    win, _app = shell("free", downloads=dl)
    page = _open(win)
    rail = page.list_view.rail
    rail.opt_local.radio.click()
    assert dl.started == ["qwen_tiny"]
    dl.pct["qwen_tiny"] = 42
    dl.progress.emit("qwen_tiny", 42, 1, 2)
    assert rail.opt_local.model_label.text() == "Qwen 1.5B · downloading 42%"
    dl.states["qwen_tiny"] = "failed"
    dl.finished.emit("qwen_tiny", "failed", "x")
    assert rail.opt_local.model_label.text() == "Qwen 1.5B · download failed"
    rail.opt_local.mousePressEvent(_left_press())  # clicking the option again retries
    assert dl.started == ["qwen_tiny", "qwen_tiny"]
    dl.states["qwen_tiny"] = "ready"
    dl.finished.emit("qwen_tiny", "ready", "")
    assert rail.opt_local.model_label.text() == "Qwen 1.5B"


def _left_press():
    from PySide6.QtCore import QEvent, QPointF, Qt
    from PySide6.QtGui import QMouseEvent
    return QMouseEvent(QEvent.MouseButtonPress, QPointF(5, 5), QPointF(5, 5), Qt.LeftButton, Qt.LeftButton,
                       Qt.NoModifier)


def test_open_live_assistant_never_hides_the_card(shell, meetings_root):
    session = FakeSession("recording")
    win, app = shell("pro", meeting_session=session)
    app.show_live_assist = lambda: app.calls.append(("show_live_assist",))
    page = _open(win)
    page.live_view.assist_btn.click()
    assert ("show_live_assist",) in app.calls and ("toggle_live_assist",) not in app.calls


# ── pure helpers ─────────────────────────────────────────────────────────────
def test_an_older_write_never_lands_over_a_newer_one(tmp_path):
    from ui.pages import meeting_views as v
    path = str(tmp_path / "notes.md")
    old, new = v.next_version(), v.next_version()
    assert v.write_latest(path, "- [x] b", new)
    assert not v.write_latest(path, "- [ ] b", old)
    assert (tmp_path / "notes.md").read_text(encoding="utf-8") == "- [x] b"


def test_note_helpers():
    from ui.pages import meeting_views as v
    assert v.parse_task("`Fix drift` (Owner: David, Due: Wednesday)") == ("Fix drift", "David", "by Wednesday")
    assert v.parse_task("Send copy (Owner: -)") == ("Send copy", "", "")
    assert v.parse_task("Ship it (Due: Fri)") == ("Ship it", "", "by Fri")
    blocks = v.parse_notes("## Summary\nOne.\nTwo.\n\n**Decisions:**\n- A\n- [x] B (Owner: Anna)\n> note")
    assert blocks == [("head", "Summary"), ("para", "One. Two."), ("head", "Decisions"), ("bullet", "A"),
                      ("task", (True, "B (Owner: Anna)", 6)), ("quote", "note")]
    assert v.toggle_task_line("- [ ] a\n- [ ] b", 1, True) == "- [ ] a\n- [x] b"
    assert v.notes_plain("## S\n- [ ] Do (Owner: Al)") == ["S", "☐ Do (Al)"]
    assert (v.fmt_duration(25), v.fmt_duration(2530), v.fmt_duration(3900)) == ("25 s", "42 min", "1 h 05 min")
    assert v.fmt_when("2026-09-29 11:00:00") == "Tue 29 Sep · 11:00"
    assert v.fmt_clock(754) == "12:34" and v.fmt_clock(3723) == "1:02:03"
    assert v.lang_name("en") == "English" and v.lang_name("auto") == ""
    assert v.md_inline("**a** <b>") == "<b>a</b> &lt;b&gt;"
