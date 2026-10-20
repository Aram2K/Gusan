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


# ── Notion: the rail, the sheet's entry point, Send to Notion ────────────────
NOTION_HUB = "99999999-9999-9999-9999-999999999999"
NOTION_OAUTH = {"notion_api_key": "tok", "notion_refresh_token": "ref", "notion_auth_mode": "oauth",
                "notion_workspace_name": "Aram's workspace", "notion_parent_page": NOTION_HUB,
                "notion_parent_title": "Gusan meetings", "notion_hub_page": NOTION_HUB}
NOTION_KEYS = ("notion_api_key", "notion_refresh_token", "notion_auth_mode", "notion_workspace_name",
               "notion_parent_page", "notion_parent_title", "notion_hub_page", "notion_auto_send",
               "notion_reconnect")


def _writable(app, **cfg):
    """A stub controller whose settings really land in cfg (the stub only records)."""
    app.cfg.update(cfg)

    def set_settings(updates):
        app.calls.append(("set_settings", dict(updates)))
        app.cfg.update(updates)
        return {k: True for k in updates}

    def set_setting(key, value):
        app.calls.append(("set_setting", key, value))
        app.cfg[key] = value
        return True
    app.set_settings = set_settings
    app.set_setting = set_setting
    app.flush_config_save = lambda: app.calls.append(("flush_config_save",))
    return app


def _sync(rail, app, **cfg):
    for k in NOTION_KEYS + ("privacy_mode",):
        app.cfg.pop(k, None)
    app.cfg.update(cfg)
    rail.sync_settings()
    QApplication.processEvents()


def test_the_notion_rows_follow_the_connection(shell, meetings_root):
    win, app = shell("pro")
    page = _open(win)
    rail = page.list_view.rail
    row = rail.notion_row
    assert row.label.text() == "Send notes to Notion"
    # not connected: one Connect, nothing else
    _sync(rail, app)
    assert rail.notion_connect.isVisibleTo(page) and rail.notion_connect.accessibleName() == "Connect Notion"
    for hidden in (rail.notion_manage, rail.notion_reconnect, rail.notion_disconnect, rail.notion_auto_row):
        assert not hidden.isVisibleTo(page)
    assert not row.hint_label.isVisibleTo(page)
    # connected with the button: the workspace, Manage + Disconnect, the switch
    _sync(rail, app, **NOTION_OAUTH)
    assert not rail.notion_connect.isVisibleTo(page) and not rail.notion_reconnect.isVisibleTo(page)
    assert rail.notion_manage.isVisibleTo(page) and rail.notion_manage.accessibleName() == "Manage Notion"
    assert rail.notion_disconnect.isVisibleTo(page) and rail.notion_disconnect.accessibleName() == "Disconnect Notion"
    assert row.hint_label.text() == "Aram's workspace"
    assert rail.notion_auto_row.isVisibleTo(page) and rail.notion_auto_row.label.text() == "Send automatically"
    assert rail.notion_auto_row.hint_label.text() == "After every meeting"
    assert not rail.notion_auto_toggle.isChecked()
    assert rail.notion_auto_toggle.accessibleName() == "Send every meeting's notes to Notion automatically"
    # the switch is on, Privacy mode is on: it says it is paused (the switch keeps its value)
    _sync(rail, app, **NOTION_OAUTH, notion_auto_send=True, privacy_mode=True)
    assert rail.notion_auto_toggle.isChecked()
    assert rail.notion_auto_row.hint_label.text() == "Paused while Privacy mode is on"
    # Notion refused the saved connection for good
    _sync(rail, app, **NOTION_OAUTH, notion_reconnect=True)
    assert row.hint_label.text() == "Reconnect Notion to keep sending."
    assert rail.notion_reconnect.isVisibleTo(page) and rail.notion_reconnect.accessibleName() == "Reconnect Notion"
    assert not rail.notion_manage.isVisibleTo(page) and rail.notion_disconnect.isVisibleTo(page)
    # a setup from before the button (a pasted secret, no workspace name)
    _sync(rail, app, notion_api_key="ntn_old", notion_parent_page=NOTION_HUB)
    assert row.hint_label.text() == "Connected" and rail.notion_manage.isVisibleTo(page)
    # a token with no page to send to is not connected
    _sync(rail, app, notion_api_key="ntn_old")
    assert rail.notion_connect.isVisibleTo(page) and not rail.notion_manage.isVisibleTo(page)


def test_the_notion_rail_actions_all_fit_beside_their_label(shell, meetings_root):
    win, app = shell("pro")
    win.show()
    page = _open(win)
    rail = page.list_view.rail
    for cfg in ({}, NOTION_OAUTH, dict(NOTION_OAUTH, notion_reconnect=True),
                dict(NOTION_OAUTH, notion_workspace_name="Institut Imagine – Direction des systèmes d'information")):
        _sync(rail, app, **cfg)
        row = rail.notion_row
        shown = [w for w in (rail.notion_connect, rail.notion_manage, rail.notion_reconnect, rail.notion_disconnect)
                 if w.isVisibleTo(page)]
        assert shown
        for w in shown:                                     # nothing clipped by the narrow rail
            assert w.width() >= w.sizeHint().width() - 1, w.text()
            assert w.mapTo(row, w.rect().bottomRight()).x() <= row.width(), w.text()
        label = row.label
        assert label.width() >= label.sizeHint().width() - 1, "the label is cut: %r" % label.text()


def test_the_automatic_switch_writes_its_key(shell, meetings_root):
    win, app = shell("pro")
    page = _open(win)
    rail = page.list_view.rail
    _sync(rail, app, **NOTION_OAUTH)
    rail.notion_auto_toggle.click()
    assert ("set_setting", "notion_auto_send", True) in app.calls
    rail.notion_auto_toggle.click()
    assert ("set_setting", "notion_auto_send", False) in app.calls


def test_connect_manage_and_reconnect_open_the_sheet(shell, meetings_root, monkeypatch):
    from ui import notion_sheet
    opened = []
    monkeypatch.setattr(notion_sheet, "show_notion_sheet",
                        lambda host, then="", reason="": opened.append((host, then, reason)) or False)
    win, app = shell("pro")
    page = _open(win)
    rail = page.list_view.rail
    _sync(rail, app)
    rail.notion_connect.click()
    _sync(rail, app, **NOTION_OAUTH)
    rail.notion_manage.click()
    _sync(rail, app, **NOTION_OAUTH, notion_reconnect=True)
    rail.notion_reconnect.click()
    assert [o[0] for o in opened] == [page, page, page] and [o[1:] for o in opened] == [("", "")] * 3
    assert page.connect_notion(then="Send this meeting") is False
    assert opened[-1][1:] == ("Send this meeting", "")


def test_the_rail_disconnect_forgets_the_connection(shell, meetings_root, monkeypatch):
    import notion_connect
    from ui import notion_sheet
    revoked = []
    monkeypatch.setattr(notion_connect, "revoke", lambda token: revoked.append(token))
    monkeypatch.setattr(notion_sheet, "_run_then", lambda fn, *a, then=None, error=None, **k: fn(*a))
    win, app = shell("pro")
    _writable(app, **NOTION_OAUTH, notion_auto_send=True)
    page = _open(win)
    rail = page.list_view.rail
    rail.sync_settings()
    assert rail.notion_disconnect.isVisibleTo(page)
    rail.notion_disconnect.click()                          # no question: it is reversible
    for key in NOTION_KEYS:
        assert app.cfg[key] in ("", False), key
    assert revoked == ["tok"]
    assert ("track", "notion_disconnected", {"mode": "oauth", "reason": "user"}) in app.calls
    assert rail.notion_connect.isVisibleTo(page) and not rail.notion_auto_row.isVisibleTo(page)


# ── Send to Notion on a meeting ──────────────────────────────────────────────
@pytest.fixture
def sync_fake(monkeypatch):
    """A stand-in notion_sync: records every send_meeting, answers ``result``."""
    import sys
    import types
    mod = types.ModuleType("notion_sync")
    mod.calls = []
    mod.result = {"ok": True, "url": "https://www.notion.so/Q4-roadmap-abc", "code": "sent", "sentence": ""}
    mod.during = None

    def send_meeting(folder, cfg, *, auto=False, persist=None, **kw):
        mod.calls.append({"folder": folder, "cfg": dict(cfg), "auto": auto, "persist": persist})
        if mod.during:
            mod.during(persist)
        return dict(mod.result)
    mod.send_meeting = send_meeting
    monkeypatch.setitem(sys.modules, "notion_sync", mod)
    return mod


@pytest.fixture
def opened_urls(monkeypatch):
    from ui.pages import meeting_views as v
    urls = []
    monkeypatch.setattr(v.QDesktopServices, "openUrl", staticmethod(lambda url: urls.append(url.toString()) or True))
    return urls


def _notes_view(shell, meetings_root, **cfg):
    folder = _meeting(meetings_root, "20260929_110000", "Q4 roadmap")
    win, app = shell("pro")
    _writable(app, **cfg)
    page = _open(win, folder=str(folder))
    return page.notes_view, app, str(folder), page


def _tracked(app, event):
    return [c[2] for c in app.calls if c[0] == "track" and c[1] == event]


def test_send_to_notion_when_connected_sends_and_opens_the_page(shell, meetings_root, sync_fake, opened_urls):
    nv, app, folder, _page = _notes_view(shell, meetings_root, **NOTION_OAUTH)
    nv.export("notion")
    assert [(c["folder"], c["auto"]) for c in sync_fake.calls] == [(folder, False)]
    assert sync_fake.calls[0]["cfg"]["notion_api_key"] == "tok"
    assert nv.flash_label.text() == "Sent to Notion"
    assert opened_urls == ["https://www.notion.so/Q4-roadmap-abc"]
    assert _tracked(app, "meeting_exported") == [{"target": "notion", "ok": True, "auto": False, "mode": "oauth"}]
    assert nv._flash.interval() == 1800


def test_send_to_notion_only_opens_https_pages(shell, meetings_root, sync_fake, opened_urls):
    sync_fake.result = dict(sync_fake.result, url="javascript:alert(1)")
    nv, _app, _folder, _page = _notes_view(shell, meetings_root, **NOTION_OAUTH)
    nv.export("notion")
    assert opened_urls == [] and nv.flash_label.text() == "Sent to Notion"


def test_send_to_notion_when_not_connected_connects_first_and_goes_on(shell, meetings_root, sync_fake, monkeypatch):
    from ui import notion_sheet
    asked = []

    def sheet(host, then="", reason=""):
        asked.append(then)
        host.cfg().update(NOTION_OAUTH)                     # the person connects, then presses "Send this meeting"
        return True
    monkeypatch.setattr(notion_sheet, "show_notion_sheet", sheet)
    nv, app, folder, _page = _notes_view(shell, meetings_root)
    nv.export("notion")
    assert asked == ["Send this meeting"]
    assert [c["folder"] for c in sync_fake.calls] == [folder]
    assert sync_fake.calls[0]["cfg"]["notion_api_key"] == "tok"
    assert _tracked(app, "meeting_exported")[0]["mode"] == "oauth"


def test_closing_the_notion_sheet_any_other_way_sends_nothing(shell, meetings_root, sync_fake, monkeypatch):
    from ui import notion_sheet
    monkeypatch.setattr(notion_sheet, "show_notion_sheet", lambda host, then="", reason="": False)
    nv, app, _folder, _page = _notes_view(shell, meetings_root)
    nv.export("notion")
    assert sync_fake.calls == [] and not nv.flash_label.isVisible()
    assert _tracked(app, "meeting_exported") == []


def test_a_connection_notion_refused_opens_the_sheet_again(shell, meetings_root, sync_fake, monkeypatch):
    from ui import notion_sheet
    asked = []
    monkeypatch.setattr(notion_sheet, "show_notion_sheet",
                        lambda host, then="", reason="": asked.append(then) or False)
    nv, _app, _folder, _page = _notes_view(shell, meetings_root, **NOTION_OAUTH, notion_reconnect=True)
    nv.export("notion")
    assert asked == ["Send this meeting"] and sync_fake.calls == []


def test_send_to_notion_asks_before_leaving_privacy_mode(shell, meetings_root, sync_fake, monkeypatch):
    from ui.components import mini_prompt
    asked = []
    answer = {"yes": False}

    def ask(parent, title, body, yes, no, icon="info"):
        asked.append((title, body, yes, no, icon))
        return answer["yes"]
    monkeypatch.setattr(mini_prompt, "ask", ask)
    nv, _app, _folder, _page = _notes_view(shell, meetings_root, **NOTION_OAUTH, privacy_mode=True)
    nv.export("notion")
    assert asked == [("Privacy mode is on", "Send this meeting to Notion anyway?", "Send", "Not now", "incognito")]
    assert sync_fake.calls == []
    answer["yes"] = True
    nv.export("notion")
    assert len(sync_fake.calls) == 1


def test_nothing_to_send_says_so_before_anything_else(shell, meetings_root, sync_fake, monkeypatch):
    from ui import notion_sheet
    monkeypatch.setattr(notion_sheet, "show_notion_sheet", lambda *a, **k: pytest.fail("no sheet for an empty meeting"))
    folder = _meeting(meetings_root, "20260929_110000", "Empty", notes="", transcript="")
    win, app = shell("pro")
    page = _open(win, folder=str(folder))
    page.notes_view.export("notion")
    assert page.notes_view.flash_label.text() == "Nothing to send yet." and sync_fake.calls == []


@pytest.mark.parametrize("code,sentence", [
    ("auth", "Couldn't send to Notion. Reconnect it in Meeting settings."),
    ("no_access", "Couldn't send to Notion. Pick another page in Meeting settings."),
    ("rate", "Notion is busy. Try again in a minute."),
    ("offline", "Couldn't reach Notion. Check your internet."),
    ("other", "Couldn't send to Notion.")])
def test_a_failed_send_shows_its_sentence_for_five_seconds(shell, meetings_root, sync_fake, opened_urls, code, sentence):
    sync_fake.result = {"ok": False, "url": "", "code": code, "sentence": sentence}
    nv, app, _folder, _page = _notes_view(shell, meetings_root, **NOTION_OAUTH)
    nv.export("notion")
    assert nv.flash_label.text() == sentence and nv._flash.interval() == 5000
    assert opened_urls == []
    assert _tracked(app, "meeting_exported") == [
        {"target": "notion", "ok": False, "auto": False, "mode": "oauth", "reason": code}]
    nv._end_flash()
    assert nv._flash.interval() == 1800                     # the next "Copied" is short again


def test_a_failure_without_a_sentence_still_says_something(shell, meetings_root, sync_fake):
    sync_fake.result = {"ok": False, "code": "weird"}
    nv, app, _folder, _page = _notes_view(shell, meetings_root, **NOTION_OAUTH)
    nv.export("notion")
    assert nv.flash_label.text() == "Couldn't send to Notion."
    assert _tracked(app, "meeting_exported")[0]["reason"] == "other"


def test_a_meeting_already_being_sent_says_so(shell, meetings_root, sync_fake):
    sync_fake.result = {"ok": False, "code": "busy", "sentence": "x"}
    nv, _app, _folder, _page = _notes_view(shell, meetings_root, **NOTION_OAUTH)
    nv.export("notion")
    assert nv.flash_label.text() == "Already sending this meeting."


def test_the_send_works_when_the_worker_itself_blows_up(shell, meetings_root, sync_fake):
    def boom(persist):
        raise RuntimeError("secret detail")
    sync_fake.during = boom
    nv, app, _folder, _page = _notes_view(shell, meetings_root, **NOTION_OAUTH)
    nv.export("notion")
    assert nv.flash_label.text() == "Couldn't send to Notion."          # never the exception's text
    assert "secret" not in nv.flash_label.text()
    assert _tracked(app, "meeting_exported")[0]["ok"] is False


def test_settings_a_send_changes_are_saved_through_the_page(shell, meetings_root, sync_fake):
    def during(persist):
        persist({"notion_api_key": "tok-new", "notion_refresh_token": "ref-new"})
    sync_fake.during = during
    nv, app, _folder, _page = _notes_view(shell, meetings_root, **NOTION_OAUTH)
    nv.export("notion")
    assert app.cfg["notion_api_key"] == "tok-new" and app.cfg["notion_refresh_token"] == "ref-new"
    assert ("set_settings", {"notion_api_key": "tok-new", "notion_refresh_token": "ref-new"}) in app.calls
    # the persist handed to the worker is a (queued) signal, not a direct call into the page
    assert sync_fake.calls[0]["persist"].__name__ == "emit"


def test_settings_changed_by_a_worker_thread_land_on_the_gui_thread(shell, meetings_root, sync_fake):
    import threading
    nv, app, _folder, _page = _notes_view(shell, meetings_root, **NOTION_OAUTH)
    seen = {}
    real = app.set_settings

    def set_settings(updates):
        seen["gui"] = threading.current_thread() is threading.main_thread()
        return real(updates)
    app.set_settings = set_settings
    worker = threading.Thread(target=lambda: nv._notion_settings.emit({"notion_reconnect": True}))
    worker.start()
    worker.join()
    assert _wait_for(lambda: "gui" in seen)
    assert seen["gui"] is True and app.cfg["notion_reconnect"] is True


def test_a_sent_meeting_gets_a_quiet_in_notion_link(shell, meetings_root, opened_urls):
    folder = _meeting(meetings_root, "20260929_110000", "Q4 roadmap", notion_url="https://www.notion.so/Q4-abc")
    win, _app = shell("pro")
    page = _open(win, folder=str(folder))
    nv = page.notes_view
    links = [b for b in nv.findChildren(QAbstractButton) if b.text() == "In Notion"]
    assert len(links) == 1 and links[0].isVisibleTo(page) and links[0].property("kind") == "link"
    assert links[0].accessibleName() == "Open this meeting in Notion"
    links[0].click()
    assert opened_urls == ["https://www.notion.so/Q4-abc"]
    other = _meeting(meetings_root, "20260930_090000", "Weekly sync")
    page.open_notes(str(other))
    QApplication.processEvents()
    assert not [b for b in page.notes_view.findChildren(QAbstractButton)
                if b.text() == "In Notion" and b.isVisibleTo(page)]
    odd = _meeting(meetings_root, "20261001_090000", "Odd link", notion_url="javascript:alert(1)")
    page.open_notes(str(odd))
    QApplication.processEvents()
    assert not [b for b in page.notes_view.findChildren(QAbstractButton)
                if b.text() == "In Notion" and b.isVisibleTo(page)]


def test_after_a_send_the_meeting_shows_the_link_without_reopening(shell, meetings_root, sync_fake, opened_urls):
    nv, _app, folder, _page = _notes_view(shell, meetings_root, **NOTION_OAUTH)

    def during(persist):                                    # what notion_sync writes into the meeting
        path = meetings_root / "20260929_110000" / "meta.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["notion_url"] = "https://www.notion.so/Q4-roadmap-abc"
        path.write_text(json.dumps(data), encoding="utf-8")
    sync_fake.during = during
    nv.export("notion")
    assert [b for b in nv.findChildren(QAbstractButton) if b.text() == "In Notion"]


# ── review fixes: plain text, contrast, the real notion_sync ─────────────────
def test_the_workspace_name_in_the_rail_is_plain_text(shell, meetings_root, tmp_path):
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QColor, QImage
    from PySide6.QtWidgets import QLabel
    img = QImage(8, 8, QImage.Format_RGB32)
    img.fill(QColor("#ff0000"))
    png = tmp_path / "x.png"
    assert img.save(str(png))
    hostile = '<img src="%s" width="40" height="40">' % png.as_uri()
    win, app = shell("pro")
    win.show()
    page = _open(win)
    rail = page.list_view.rail
    _sync(rail, app, **dict(NOTION_OAUTH, notion_workspace_name=hostile))
    label = rail.notion_row.hint_label
    assert label.textFormat() == Qt.PlainText and label.text() == hostile

    def reds(lb):
        shot = lb.grab().toImage()
        return sum(1 for x in range(shot.width()) for y in range(shot.height())
                   if QColor(shot.pixel(x, y)).red() > 200 and QColor(shot.pixel(x, y)).green() < 60)
    control = QLabel(hostile)                                # the stock label loads the picture
    control.resize(200, 60)
    assert reds(control) > 100
    assert reds(label) == 0


def _contrast(a, b):
    def lum(c):
        def ch(v):
            v /= 255.0
            return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
        return 0.2126 * ch(c.red()) + 0.7152 * ch(c.green()) + 0.0722 * ch(c.blue())
    hi, lo = sorted((lum(a), lum(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def _text_colour_in(img, widget, rail):
    """(the most text-like colour, the background) of ``widget`` in a render of ``rail``."""
    from PySide6.QtCore import QPoint, QRect
    from PySide6.QtGui import QColor
    top_left = widget.mapTo(rail, QPoint(0, 0))
    dpr = img.devicePixelRatio() or 1
    r = QRect(int(top_left.x() * dpr), int(top_left.y() * dpr), int(widget.width() * dpr), int(widget.height() * dpr))
    counts = {}
    for y in range(r.top(), min(r.bottom() + 1, img.height())):
        for x in range(r.left(), min(r.right() + 1, img.width())):
            counts[img.pixel(x, y)] = counts.get(img.pixel(x, y), 0) + 1
    bg = QColor.fromRgb(max(counts, key=counts.get))
    far = max(counts, key=lambda k: sum(abs(a - b) for a, b in zip(QColor.fromRgb(k).getRgb()[:3], bg.getRgb()[:3])))
    return QColor.fromRgb(far), bg


def test_what_the_rail_asks_for_is_readable(shell, meetings_root):
    """The reconnect hint (an instruction) and the Disconnect link (an action) are at least 4.5:1
    on the rail, measured on the real pixels (the muted grey was 4.4:1)."""
    from PySide6.QtGui import QImage
    win, app = shell("pro")
    win.resize(1180, 820)
    win.show()
    page = _open(win)
    rail = page.list_view.rail
    _sync(rail, app, **NOTION_OAUTH, notion_reconnect=True)
    QApplication.processEvents()
    img = rail.grab().toImage().convertToFormat(QImage.Format_RGB32)
    for widget in (rail.notion_row.hint_label, rail.notion_disconnect, rail.notion_reconnect):
        if widget is rail.notion_reconnect:
            continue                                         # a button with its own fill
        fg, bg = _text_colour_in(img, widget, rail)
        assert _contrast(fg, bg) >= 4.5, (widget.text(), fg.name(), bg.name(), _contrast(fg, bg))
    _sync(rail, app, **NOTION_OAUTH)
    assert rail.notion_row.hint_label.property("textRole") == "hint"         # a passive name stays quiet
    _sync(rail, app, **NOTION_OAUTH, notion_reconnect=True)
    assert rail.notion_row.hint_label.property("textRole") == "body"
    _sync(rail, app, **NOTION_OAUTH)
    assert rail.notion_row.hint_label.property("textRole") == "hint"


def test_a_muted_link_reads_dark_enough_on_the_glass(shell, meetings_root):
    from PySide6.QtGui import QImage
    win, app = shell("pro")
    win.resize(1180, 820)
    win.show()
    page = _open(win)
    rail = page.list_view.rail
    _sync(rail, app, **NOTION_OAUTH)
    QApplication.processEvents()
    img = rail.grab().toImage().convertToFormat(QImage.Format_RGB32)
    fg, bg = _text_colour_in(img, rail.notion_disconnect, rail)
    assert _contrast(fg, bg) >= 4.5, (fg.name(), bg.name())


def test_send_to_notion_runs_through_the_real_notion_sync(shell, meetings_root, opened_urls, monkeypatch):
    """No stand-in module: the real notion_sync publishes (a fake publish_meeting), writes the page into
    the meeting, and the meeting then shows its "In Notion" link - the way a missing module could not hide."""
    import notion_export
    calls = []

    def publish(token, parent, title, notes, transcript="", meta_line="", *, oauth=False):
        calls.append((token, parent, title, bool(notes), transcript, oauth))
        return "https://www.notion.so/Q4-roadmap-0123456789abcdef0123456789abcdef"
    monkeypatch.setattr(notion_export, "publish_meeting", publish)
    nv, app, folder, _page = _notes_view(shell, meetings_root, **NOTION_OAUTH)
    nv.export("notion")
    assert calls == [("tok", NOTION_HUB, "Q4 roadmap", True, "Speaker 1: Hello team.", True)]
    assert nv.flash_label.text() == "Sent to Notion"
    assert opened_urls == ["https://www.notion.so/Q4-roadmap-0123456789abcdef0123456789abcdef"]
    meta = json.loads((meetings_root / "20260929_110000" / "meta.json").read_text(encoding="utf-8"))
    assert meta["notion_url"].startswith("https://www.notion.so/Q4-roadmap")
    assert meta["notion_auto"] is False and "notion_error" not in meta
    assert [b for b in nv.findChildren(QAbstractButton) if b.text() == "In Notion"]
    assert _tracked(app, "meeting_exported") == [{"target": "notion", "ok": True, "auto": False, "mode": "oauth"}]


def test_a_refused_token_is_refreshed_and_saved_through_the_real_notion_sync(shell, meetings_root, opened_urls,
                                                                              monkeypatch):
    import notion_connect
    import notion_export
    seen = []

    def publish(token, parent, title, notes, transcript="", meta_line="", *, oauth=False):
        seen.append(token)
        if token == "tok":
            raise notion_export.NotionAuthError("401")
        return "https://www.notion.so/Q4-roadmap-0123456789abcdef0123456789abcdef"
    monkeypatch.setattr(notion_export, "publish_meeting", publish)
    monkeypatch.setattr(notion_connect, "refresh", lambda rt: {"access_token": "tok-2", "refresh_token": "ref-2"})
    nv, app, _folder, _page = _notes_view(shell, meetings_root, **NOTION_OAUTH)
    nv.export("notion")
    assert seen == ["tok", "tok-2"]
    assert nv.flash_label.text() == "Sent to Notion"
    assert _wait_for(lambda: app.cfg["notion_api_key"] == "tok-2")
    assert app.cfg["notion_refresh_token"] == "ref-2"


def test_a_deleted_hub_is_made_again_through_the_real_notion_sync(shell, meetings_root, opened_urls, monkeypatch):
    import notion_connect
    import notion_export
    new = {"id": "88888888-8888-8888-8888-888888888888", "title": "Gusan meetings", "url": ""}
    parents = []

    def publish(token, parent, title, notes, transcript="", meta_line="", *, oauth=False):
        parents.append(parent)
        if parent == NOTION_HUB:
            raise notion_export.NotionNotFoundError("gone")
        return "https://www.notion.so/Q4-roadmap-0123456789abcdef0123456789abcdef"
    monkeypatch.setattr(notion_export, "publish_meeting", publish)
    monkeypatch.setattr(notion_connect, "create_hub", lambda token, title=notion_connect.HUB_TITLE: dict(new))
    nv, app, _folder, _page = _notes_view(shell, meetings_root, **NOTION_OAUTH)
    nv.export("notion")
    assert parents == [NOTION_HUB, new["id"]]
    assert _wait_for(lambda: app.cfg["notion_parent_page"] == new["id"])
    assert app.cfg["notion_hub_page"] == new["id"]


def test_a_failed_real_send_says_why_and_the_meeting_remembers_it(shell, meetings_root, opened_urls, monkeypatch):
    import notion_export

    def publish(*a, **kw):
        raise notion_export.NotionError("net", code="offline")
    monkeypatch.setattr(notion_export, "publish_meeting", publish)
    nv, app, _folder, _page = _notes_view(shell, meetings_root, **NOTION_OAUTH)
    nv.export("notion")
    assert nv.flash_label.text() == "Couldn't reach Notion. Check your internet."
    meta = json.loads((meetings_root / "20260929_110000" / "meta.json").read_text(encoding="utf-8"))
    assert meta["notion_error"] == "offline" and "notion_url" not in meta
    assert opened_urls == []


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
