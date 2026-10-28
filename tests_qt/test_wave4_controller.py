"""Gusan 2.0 Wave 4: the REAL AppController owns the services and wires them
(offscreen, a throwaway profile, no global hotkey hooks, no network, no
analytics). Named to run last: it imports main and builds the whole app."""
import threading
from unittest import mock

import pytest
from PySide6.QtWidgets import QApplication


def _spin(ms=50):
    import time
    end = time.monotonic() + ms / 1000.0
    while time.monotonic() < end:
        QApplication.processEvents()


# ── the dictation pill's "Getting ready" ──
def test_the_pill_says_getting_ready_with_the_download(qapp):
    from types import SimpleNamespace
    import ui.overlay as ov
    hud = ov.Overlay(main_app=SimpleNamespace(cfg={"backend": "local"}, save_config=lambda: None))
    try:
        hud.show_overlay(ov.TRANSCRIBING)
        hud.set_partial("half a sentence")
        hud.set_state(ov.GETTING_READY)
        hud.set_model_progress(42)
        assert hud._busy_label("transcribing") == ("Getting ready", "speech model · 42%")
        assert hud._partial_line() == ""                  # the label, not words
        hud.set_model_progress(None)
        assert hud._busy_label("transcribing") == ("Getting ready", "speech model")
        hud.set_state(ov.TRANSCRIBING)
        assert hud._busy_label("transcribing")[0] == "Transcribing"
        assert hud._partial_line() == "half a sentence"
    finally:
        hud.timer.stop()
        hud.hide()
        hud.deleteLater()


# ── the mic check at Quit ──
def test_the_mic_check_closes_on_this_thread_at_quit(qapp):
    from mic_monitor import MicMonitor
    closed = []

    class Stream:
        def stop_stream(self):
            closed.append(("stop", threading.current_thread() is threading.main_thread()))

        def close(self):
            closed.append(("close", threading.current_thread() is threading.main_thread()))
    mm = MicMonitor(pa=object(), spawn=lambda fn, *a: None)      # no worker ever runs
    try:
        mm._stream, mm._running = Stream(), True
        assert mm.stop_now() is True
        assert closed == [("stop", True), ("close", True)]       # here, not on a worker
        assert not mm.is_running() and mm._stream is None
        # A worker holding PyAudio (an open in flight): give up after the timeout.
        held, release = threading.Event(), threading.Event()

        def hold():
            with mm._pa_lock:
                held.set()
                release.wait(2)
        t = threading.Thread(target=hold)
        t.start()
        assert held.wait(1)
        try:
            assert mm.stop_now(timeout=0.05) is False
        finally:
            release.set()
            t.join(2)
    finally:
        mm.deleteLater()


# ── the controller ──
@pytest.fixture(scope="module")
def ctl(qapp):
    import main
    import updater
    main.cfg.update(analytics_enabled=False, onboarding_done=True, account_gate_seen=True,
                    install_updates_on_quit=False)
    patches = [
        mock.patch.object(main.AppController, "_setup_hotkey", lambda self, hk, remove_old=None: True),
        mock.patch.object(main.AppController, "_setup_assist_hotkey", lambda self, hk: False),
        mock.patch.object(main.AppController, "_setup_smart_hotkey", lambda self, hk: False),
        mock.patch.object(main.AppController, "_register_transient_keys", lambda self: None),
        mock.patch.object(main.AppController, "_preload_speech_model", lambda self: None),
        mock.patch.object(updater, "check_latest", lambda *a, **k: (updater.ERROR, "")),
    ]
    for p in patches:
        p.start()
    c = main.AppController(qapp)
    hints = []
    c.show_tray_hint = lambda title, text, on_click=None: hints.append((title, text, on_click))
    c.hints = hints
    # Let the startup timers fire while the patches hold (PySide calls a
    # bound-method slot by name, so a restored _preload_speech_model would
    # load a real Whisper model - and a model alive at exit crashes it).
    _spin(2800)
    yield c
    for t in (c._idle_timer, c._save_timer):
        t.stop()
    c.file_jobs.shutdown()
    if c.mic_monitor is not None:
        c.mic_monitor.stop()
    if c.live_assist is not None:
        c.live_assist.hide()
    c.tray_icon.hide()
    c.recorder.shutdown()
    # Destroy the app's Qt objects now, while the QApplication lives: left
    # to interpreter exit they would outlive it (an access violation there).
    import gc
    import shiboken6
    tray = getattr(c, "tray_menu", None)
    for obj in (getattr(c, "_reminder", None), getattr(tray, "menu", None),
                c.live_assist, c.main_win,
                c.onboarding_win, c.overlay, c.tray_icon, c.mic_monitor, c.file_jobs,
                getattr(c.meeting_session, "proc_signals", None), c.meeting_session,
                c.downloads, c):
        if obj is not None and shiboken6.isValid(obj):
            if hasattr(obj, "hide"):
                obj.hide()
            shiboken6.delete(obj)
    _spin(50)
    gc.collect()
    for p in patches:
        p.stop()


def test_the_controller_owns_the_services(ctl):
    import sys
    import main
    from meeting_session import MeetingSession
    # Startup built the tray and the dictation bar only: the Gusan window
    # waits for its first open, and no 1.x window module was even imported.
    assert ctl.main_win is None
    assert ctl.tray_icon.contextMenu() is ctl.tray_menu.menu
    assert ctl.overlay is not None
    retired = ["ui." + m for m in ("settings", "history", "meetings", "meeting_detail", "onboarding",
                                   "shiny_tabs", "mode_combo", "file_transcribe")]
    assert [m for m in retired if m in sys.modules] == []
    assert ctl.cfg is main.cfg                                   # mutated in place, never rebound
    assert type(ctl.downloads).__name__ == "ModelDownloads"
    assert ctl.recorder.model_fetcher == ctl._fetch_speech_model
    assert type(ctl.file_jobs).__name__ == "FileJobController"
    assert ctl._file_job_running is False
    assert isinstance(ctl.meeting_session, MeetingSession)
    for classic in ("settings_win", "history_win", "meetings_win"):  # the 1.x windows retired
        assert not hasattr(ctl, classic), classic
    assert ctl._is_meeting_busy() is False
    assert ctl.calendar is None                                  # no Google client in this build
    assert type(ctl.mic_monitor).__name__ == "MicMonitor"
    assert ctl.cfg["config_schema_version"] == 3
    # A Smart Action's paste reaches the rich clipboard from its worker thread.
    from ui.components import clipboard as rich_clipboard
    assert rich_clipboard.installed() is True
    items, msg = ctl.file_jobs.enqueue(["Z:/no/such/file.m4a"])
    assert items == [] and msg


def test_history_and_recording_signals_reach_the_gui_thread(ctl):
    got = {"history": 0, "rec": []}
    on_history = lambda: got.__setitem__("history", got["history"] + 1)  # noqa: E731
    ctl.sig_history_changed.connect(on_history)
    ctl.sig_recording_changed.connect(got["rec"].append)
    try:
        t = threading.Thread(target=ctl.notify_history_changed)
        t.start()
        t.join()
        _spin(100)
        assert got["history"] == 1
        ctl.is_rec = True
        ctl.is_rec = False
        t = threading.Thread(target=setattr, args=(ctl, "is_rec", True))   # a worker sets it
        t.start()
        t.join()
        _spin(100)
        ctl.is_rec = False
        assert got["rec"] == [True, False, True, False]
        # A worker's change that arrives after the GUI changed it back is
        # never announced stale: listeners end with the real state.
        t = threading.Thread(target=setattr, args=(ctl, "is_rec", True))
        t.start()
        t.join()
        ctl.is_rec = False
        _spin(100)
        assert got["rec"][-1] is False and ctl.is_rec is False
    finally:
        ctl.sig_history_changed.disconnect(on_history)
        ctl.sig_recording_changed.disconnect(got["rec"].append)


def test_a_finished_meeting_refreshes_history(ctl):
    seen = []

    def on_history():
        seen.append(1)
    ctl.sig_history_changed.connect(on_history)
    try:
        ctl.meeting_session.finished.emit("C:/m/1", "notes", "")
        _spin(50)
        assert seen == [1]
    finally:
        ctl.sig_history_changed.disconnect(on_history)


def test_live_assistant_is_fed_by_the_meeting_session(ctl):
    ctl.toggle_live_assist()
    card = ctl.live_assist
    assert card is not None and card.isVisible()
    ctl.show_live_assist()                                       # never hides it
    assert card.isVisible()
    card.set_meeting_active(True, "Live session 10:00", "")
    ctl.meeting_session.sig_chunk.emit("How much does it cost")
    _spin(100)
    assert "How much does it cost" in card._live_text
    card.set_meeting_active(False)
    ctl.toggle_live_assist()
    assert not card.isVisible()


def test_the_update_answer_is_one_quiet_notice(ctl, monkeypatch):
    import main
    monkeypatch.setattr(main, "APP_VERSION", "2.0.0")
    ctl.hints.clear()
    seen = []
    ctl.sig_setting_changed.connect(seen.append)
    try:
        ctl._on_update_checked("available", "v2.0.1")
        _spin(100)
        assert ctl.cfg["pending_update_version"] == "v2.0.1"
        assert "pending_update_version" in seen
        ctl.sig_update_available.emit("v2.0.1")                  # e.g. after a dictation
        _spin(100)
        assert [h[0] for h in ctl.hints] == ["Update available"]
    finally:
        ctl.sig_setting_changed.disconnect(seen.append)
        ctl.cfg["pending_update_version"] = ""


def test_quit_asks_before_it_ends_a_meeting(ctl, monkeypatch):
    from ui.components import mini_prompt
    asked = []

    def exec_(self):                                      # closed with Esc / Cancel
        asked.append((self.windowTitle(), self.body_label.text()))
        assert self.tone == "danger" and self.no_button.isDefault()      # red Quit; Enter = Cancel
        return 0
    monkeypatch.setattr(mini_prompt.MiniPrompt, "exec", exec_)
    session = ctl.meeting_session
    assert session.state == "idle"
    session.state = "recording"
    try:
        assert ctl.quit_app() is False                    # still running
        assert asked == [("Quit Gusan?", "A meeting is still recording. If you quit, it stops and "
                                          "is saved. Its notes are written the next time you open Gusan.")]
        assert ctl._quitting is False and ctl.tray_icon.isVisible()
    finally:
        session.state = "idle"


def test_cmd_q_and_the_dock_quit_go_through_quit_app(ctl, monkeypatch):
    import main
    from PySide6.QtCore import QEvent
    asked = []
    monkeypatch.setattr(ctl, "quit_app", lambda: asked.append("quit_app") or False)
    qapp = QApplication.instance()
    # The QApplication main() builds: its Quit event asks the controller first.
    monkeypatch.setattr(qapp, "quit_filter", ctl._on_quit_event, raising=False)
    ev = QEvent(QEvent.Quit)
    assert main.GusanApplication.event(qapp, ev) is True     # swallowed: Qt doesn't just hide windows
    assert not ev.isAccepted()
    _spin(50)
    assert asked == ["quit_app"]                              # ...quit_app runs (it may ask first)
    assert ctl._quitting is False and ctl.tray_icon.isVisible()


def test_a_log_off_mid_meeting_saves_without_a_question(ctl, monkeypatch):
    from ui.components import mini_prompt
    saved = []

    def no_card(*_a, **_k):
        raise AssertionError("a question was asked at a log-off")
    monkeypatch.setattr(mini_prompt.MiniPrompt, "exec", no_card)       # any card would fail here
    monkeypatch.setattr(ctl.meeting_session, "save_for_quit", lambda: saved.append("saved") or "")
    session = ctl.meeting_session
    session.state = "recording"
    try:
        ctl._on_session_ending(None)                          # commitDataRequest
        assert saved == ["saved"] and ctl._session_ending is True
        assert ctl._on_quit_event() is True                   # Windows' own quit then goes ahead
    finally:
        session.state = "idle"
        ctl._session_ending = False


def test_notes_a_quit_left_are_written_at_the_next_launch(ctl, monkeypatch):
    import json
    import time
    import actions
    import meeting_store
    import meeting_session
    folder = meeting_store.meetings_dir() / "20261008_101500"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "transcript.txt").write_text("Thanks for staying late. Pricing next week.", encoding="utf-8")
    meeting_store.update_meta(folder, title="Late call", duration_sec=1860, seen=False,
                              **{meeting_session.META_NOTES_PENDING: True})
    monkeypatch.setattr(actions, "process", lambda *a, **k: "## Summary\n- Pricing next week")
    ctl.hints.clear()
    ctl._write_pending_notes()                                 # what startup schedules (+20 s)
    end = time.monotonic() + 10
    while time.monotonic() < end and not ctl.hints:
        _spin(50)
    assert (folder / "notes.md").read_text(encoding="utf-8") == "## Summary\n- Pricing next week"
    meta = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
    assert meeting_session.META_NOTES_PENDING not in meta
    assert [h[:2] for h in ctl.hints] == [("Notes ready", "Late call")]
    assert ctl.meeting_session.is_busy is False


def test_notes_that_failed_say_so_once(ctl, monkeypatch):
    import time
    import actions
    import meeting_store
    folder = meeting_store.meetings_dir() / "20261008_111500"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "transcript.txt").write_text("We ship Friday.", encoding="utf-8")
    meeting_store.update_meta(folder, title="Ship review")

    def fail(*a, **k):
        raise RuntimeError("The AI engine didn't answer.")
    monkeypatch.setattr(actions, "process", fail)
    ctl.hints.clear()
    assert ctl.meeting_session.retry_notes(folder=str(folder))
    end = time.monotonic() + 10
    while time.monotonic() < end and not ctl.hints:
        _spin(50)
    title, text, on_click = ctl.hints[0]
    assert (title, text) == ("Notes couldn't be written", "Ship review · Open")
    on_click()
    _spin(30)
    try:
        assert ctl.main_win.current_route() == "meetings"
    finally:
        ctl.main_win.hide()


def test_suspend_hotkeys_round_trip(ctl):
    calls = []
    with mock.patch.object(type(ctl), "apply_tray_bindings", lambda self: calls.append("rehook")):
        assert ctl.suspend_hotkeys(True) is True
        assert ctl._hotkeys_suspended is True
        assert ctl.suspend_hotkeys(False) is True
    assert calls == ["rehook"] and ctl._hotkeys_suspended is False


# ── routes and surfaces ──
def test_the_tray_is_the_gusan_menu(ctl):
    from ui.tray_menu import TrayMenu
    tm = ctl.tray_menu
    assert isinstance(tm, TrayMenu)
    assert ctl.tray_icon.contextMenu() is tm.menu
    rows = tm.visible_texts()
    texts = [t for _k, t in rows]
    for row in ("Open Gusan", "Dictate", "Record a meeting…", "Live Assistant",
                "Convert audio to text…", "Recent", "Privacy mode", "Settings…", "Quit Gusan"):
        assert row in texts, row
    keys = [k for k, _t in rows]
    assert "signin" in keys and "upgrade" not in keys            # a guest: Sign in, no Upgrade
    assert not [t for t in texts if "Transcribe" in t or "Live Assistance" in t]
    ctl._build_tray_menu()                                       # e.g. a sign-in: refreshed in place
    assert ctl.tray_menu is tm and ctl.tray_icon.contextMenu() is tm.menu


def test_the_old_entry_points_open_their_pages(ctl, monkeypatch):
    upsells = []
    monkeypatch.setattr(ctl, "_pro_upsell", lambda feature=None: upsells.append(feature))
    try:
        for call, route in ((lambda: ctl.show_settings("speech"), "settings"),
                            (ctl.show_history, "home"),
                            (ctl.show_file_transcribe, "audio_to_text"),
                            (ctl.show_meeting, "meetings"),           # a guest too: no Pro window
                            (ctl.show_gpu_settings, "settings")):
            call()
            _spin(30)
            assert ctl.main_win.current_route() == route
            assert ctl.main_win.isVisible()
        assert upsells == []
        # Only the Gusan window, the dictation bar and nothing from 1.x exist.
        from PySide6.QtWidgets import QDialog
        assert not [w for w in QApplication.topLevelWidgets()
                    if isinstance(w, QDialog) and w.isVisible()]
    finally:
        ctl.main_win.hide()


def test_first_run_only_gets_the_new_wizard(ctl):
    from ui.onboarding_v2 import OnboardingWizard
    assert ctl.onboarding_win is None                            # onboarded: never even built
    ctl.show_onboarding()
    assert ctl.onboarding_win is None and ctl.main_win.isVisible()
    ctl.main_win.hide()
    ctl.cfg["onboarding_done"] = False
    try:
        wiz = ctl.show_onboarding()
        assert isinstance(wiz, OnboardingWizard) and wiz.isVisible()
        assert ctl.show_onboarding() is wiz                      # brought forward, not a second one
        wiz.reject()
        _spin(30)
        assert not wiz.isVisible()
    finally:
        ctl.cfg["onboarding_done"] = True


def test_a_tray_click_brings_the_first_run_wizard_forward(ctl):
    from PySide6.QtWidgets import QSystemTrayIcon
    ctl.cfg["onboarding_done"] = False
    try:
        wiz = ctl.show_onboarding()
        if ctl.main_win is not None:
            ctl.main_win.hide()
        ctl._on_tray_activated(QSystemTrayIcon.Trigger)
        assert wiz.isVisible()
        assert ctl.main_win is None or not ctl.main_win.isVisible()   # never the app over it
        wiz.reject()
        _spin(30)
        ctl._on_tray_activated(QSystemTrayIcon.Trigger)               # closed with X: the app opens
        assert ctl.main_win.isVisible()
    finally:
        ctl.cfg["onboarding_done"] = True
        if ctl.main_win is not None:
            ctl.main_win.hide()


def test_a_second_launch_brings_the_first_run_wizard_forward(ctl):
    import main
    ctl.cfg["onboarding_done"] = False
    try:
        wiz = ctl.show_onboarding()
        if ctl.main_win is not None:
            ctl.main_win.hide()
        main.dispatch_ipc_action(ctl, "show_settings")        # a second launch / the installer
        assert wiz.isVisible()
        assert ctl.main_win is None or not ctl.main_win.isVisible()   # never the app behind it
        wiz.reject()
        _spin(30)
        main.dispatch_ipc_action(ctl, "show_settings")        # closed with X: the app opens
        assert ctl.main_win.isVisible()
    finally:
        ctl.cfg["onboarding_done"] = True
        if ctl.main_win is not None:
            ctl.main_win.hide()


def test_sign_in_is_the_new_sheet(ctl):
    from ui.auth_sheet import AuthSheet
    sheet = ctl.show_auth_gate()
    try:
        assert isinstance(sheet, AuthSheet) and sheet.isVisible()
        assert sheet.mode == "signin"
        assert ctl.show_auth_gate() is sheet                     # one sheet, never a stack
    finally:
        sheet.reject()
        _spin(50)


def test_the_pro_window_opens_over_the_gusan_window(ctl, monkeypatch):
    from PySide6.QtWidgets import QLabel
    from ui.pro_dialog import ProDialog
    seen = []

    def fake_exec(dlg):
        lines = [lb.text() for lb in dlg.findChildren(QLabel) if lb.text().endswith("is a Pro feature.")]
        seen.append((dlg.parentWidget(), lines))
        dlg.deleteLater()
        return 0
    monkeypatch.setattr(ProDialog, "exec", fake_exec)
    ctl.show_main("home")
    _spin(30)
    try:
        ctl._pro_upsell("Live Assistance")                       # the tray / pages' feature name
        assert seen[0] == (ctl.main_win, ["Live Assistant is a Pro feature."])
    finally:
        ctl.main_win.hide()
    ctl._pro_upsell()
    assert seen[1] == (None, [])                                 # window hidden: no parent
    _spin(30)


def test_a_calendar_reminder_is_the_toast(ctl, monkeypatch):
    from datetime import datetime, timedelta, timezone
    from ui.floating.meeting_reminder import MeetingReminder
    upsells = []
    monkeypatch.setattr(ctl, "_pro_upsell", lambda feature=None: upsells.append(feature))
    start = datetime.now(timezone.utc) + timedelta(seconds=40)
    m = {"id": "ev1", "title": "Weekly product sync", "start": start, "end": start + timedelta(minutes=30),
         "attendees": ["Aram", "Anna"], "others": ["Anna"], "people": "Anna",
         "link": "https://meet.google.com/abc", "call_type": "Google Meet"}
    ctl.hints.clear()
    ctl._on_calendar_remind(m)
    toast = ctl._reminder
    try:
        assert isinstance(toast, MeetingReminder) and toast.isVisible()
        assert ctl.hints == []
        assert ctl._calendar_busy() is True                      # one toast at a time
        ctl._on_calendar_remind(dict(m, id="ev2"))
        assert ctl._reminder is toast                            # reused
        assert ctl.record_from_calendar(m) is False              # a guest: the Pro window
        assert upsells == ["Meeting recording"]
        assert not toast.isVisible()
        assert ctl.meeting_session.is_busy is False
        assert ctl._calendar_busy() is False
    finally:
        toast.hide_reminder()


# ── Google Calendar wiring (G7, G11): the controller side of the reminder, the cards and telemetry ──
CAL_AGENDA = "Q3 numbers\nHiring plan\nLaunch checklist"


def _cal_meeting(**kw):
    from datetime import datetime, timedelta, timezone
    start = datetime.now(timezone.utc) + timedelta(seconds=40)
    m = {"id": "ev1", "title": "Weekly product sync", "start": start, "end": start + timedelta(minutes=30),
         "attendees": ["Aram Adamyan", "Anna Smith", "David Lee"], "others": ["Anna Smith", "David Lee"],
         "people": "Anna, David", "agenda": CAL_AGENDA, "link": "https://meet.google.com/abc",
         "call_type": "Google Meet", "private": False, "has_others": True}
    m.update(kw)
    return m


class _FakeSession:
    """Stands in for MeetingSession: records start(), takes an agenda like the real one does."""

    def __init__(self, ok=True, with_agenda=True, log=None):
        self.is_busy = False
        self.ok = ok
        self.calls = []
        self.log = log if log is not None else []
        self.with_agenda = with_agenda
        self.agendas = []
        if with_agenda:
            self.start = self._start_with_agenda
        else:
            self.start = self._start_plain

    def _start_with_agenda(self, title="", attendees="", agenda="", source=None, calendar_event_id=None):
        self.log.append("session.start")
        self.calls.append(dict(title=title, attendees=attendees, agenda=agenda, source=source,
                               calendar_event_id=calendar_event_id))
        return (True, "", "") if self.ok else (False, "busy", "A meeting is already being recorded.")

    def _start_plain(self, title="", attendees="", source=None, calendar_event_id=None):
        self.log.append("session.start")
        self.calls.append(dict(title=title, attendees=attendees, source=source,
                               calendar_event_id=calendar_event_id))
        return (True, "", "") if self.ok else (False, "busy", "A meeting is already being recorded.")

    def set_agenda(self, text):
        self.agendas.append(text)


class _FakeCard:
    def __init__(self):
        self.invites = []

    def set_invite(self, agenda="", others=None):
        self.invites.append((agenda, list(others) if others is not None else None))


@pytest.fixture
def cal_flow(ctl, monkeypatch):
    """record_from_calendar with everything outside the controller faked, in one call log."""
    log = []
    events = {"log": log, "main": [], "tracked": [], "marked": [], "hints": ctl.hints}
    monkeypatch.setattr(ctl, "is_pro", lambda: True)
    monkeypatch.setattr(ctl, "show_main", lambda page=None, **kw: events["main"].append((page, kw)))
    monkeypatch.setattr(ctl, "show_live_assist", lambda: log.append("show_live_assist"))
    monkeypatch.setattr(ctl, "track", lambda event, props=None: events["tracked"].append((event, props)))
    card = _FakeCard()

    def ensure():
        log.append("ensure_live_assist")
        return card
    monkeypatch.setattr(ctl, "_ensure_live_assist", ensure)
    monkeypatch.setattr(ctl, "live_assist", card)
    cal = type("Cal", (), {"mark_prompted": lambda self, i: events["marked"].append(i) or True})()
    monkeypatch.setattr(ctl, "calendar", cal)
    import ui.recording_prompts as rp
    monkeypatch.setattr(rp, "ensure_recording_consent", lambda parent, app: True)
    events["card"] = card
    events["session"] = lambda **kw: _install_session(ctl, monkeypatch, log, **kw)
    ctl.hints.clear()
    return events


def _install_session(ctl, monkeypatch, log, **kw):
    s = _FakeSession(log=log, **kw)
    monkeypatch.setattr(ctl, "meeting_session", s)
    return s


def test_calendar_start_context_helper(ctl, monkeypatch):
    # No calendar (dormant) or not connected: nothing - for the default meeting.
    monkeypatch.setattr(ctl, "calendar", None)
    assert ctl.calendar_start_context() is None
    m = _cal_meeting()

    class Cal:
        available, is_connected = True, True

        def __init__(self, row):
            self.row = row

        def current_meeting(self, only_meetings=False):
            return self.row
    cal = Cal(m)
    monkeypatch.setattr(ctl, "calendar", cal)
    ctx = ctl.calendar_start_context()
    assert ctx == {"title": "Weekly product sync", "attendees": ["Aram Adamyan", "Anna Smith", "David Lee"],
                   "others": ["Anna Smith", "David Lee"], "agenda": CAL_AGENDA, "event_id": "ev1"}
    assert ctl.calendar_start_context(with_agenda=False)["agenda"] == ""      # title and names only
    cal.is_connected = False
    assert ctl.calendar_start_context() is None
    cal.is_connected = True
    cal.row = None                                             # nothing on now
    assert ctl.calendar_start_context() is None
    cal.current_meeting = lambda only_meetings=False: 1 / 0    # a service that breaks is not the start's problem
    assert ctl.calendar_start_context() is None
    # A meeting handed in is used as it is (the reminder's), connected or not.
    monkeypatch.setattr(ctl, "calendar", None)
    assert ctl.calendar_start_context(m)["event_id"] == "ev1"
    # Nothing that identifies anyone beyond their name goes in; a private event has no agenda.
    ctx = ctl.calendar_start_context(_cal_meeting(private=True, attendees=["Aram <a@x.example>"],
                                                  others=["Anna <anna@secret.example>"]))
    assert ctx["agenda"] == "" and "@" not in repr(ctx) and "secret.example" not in repr(ctx)


def test_the_live_card_start_never_takes_a_solo_block(ctl, monkeypatch, tmp_path):
    """calendar_start_context() with no meeting is the Live card's Start: its title, people
    and agenda go to the AI with every answer, so a solo block ("Doctor appointment") is
    never picked - while the start card's pre-fill still takes any event (spec A9)."""
    from datetime import datetime, timedelta, timezone
    from calendar_service import CalendarCore

    class Api:
        def configured(self, cfg=None):
            return True

        def is_connected(self):
            return True

    now = datetime.now(timezone.utc)
    solo = _cal_meeting(id="doc", title="Doctor appointment", start=now - timedelta(minutes=2),
                        end=now + timedelta(minutes=58), attendees=["Aram Adamyan"], others=[],
                        has_others=False, agenda="Bring the results")
    real = _cal_meeting(id="cust", title="Customer call", start=now + timedelta(minutes=4),
                        end=now + timedelta(minutes=34))

    class Cal:
        available, is_connected = True, True

        def __init__(self):
            self.core = CalendarCore({}, api=Api(), state_path=tmp_path / "cal.json")

        def current_meeting(self, only_meetings=False):
            return self.core.current_meeting(only_meetings=only_meetings)
    cal = Cal()
    monkeypatch.setattr(ctl, "calendar", cal)
    cal.core.apply_fetch([solo])
    assert cal.current_meeting()["id"] == "doc"                           # the start card: any event
    assert ctl.calendar_start_context() is None                           # the Live card: nothing on
    cal.core.apply_fetch([solo, real])
    assert cal.current_meeting()["id"] == "doc"                           # it started closer to now...
    ctx = ctl.calendar_start_context()
    assert ctx["title"] == "Customer call" and ctx["event_id"] == "cust"  # ...but the real meeting wins here
    assert "Doctor" not in repr(ctx) and "results" not in repr(ctx)


def test_record_from_calendar_with_assist_orders_card_then_session_and_passes_agenda(ctl, cal_flow):
    s = cal_flow["session"]()
    assert ctl.record_from_calendar(_cal_meeting(), with_assist=True) is True
    assert cal_flow["log"] == ["ensure_live_assist", "session.start", "show_live_assist"]
    kw = s.calls[0]
    assert kw == {"title": "Weekly product sync", "attendees": "Aram Adamyan, Anna Smith, David Lee",
                  "agenda": CAL_AGENDA, "source": "calendar", "calendar_event_id": "ev1"}
    assert ctl.recorder.fast_live_chunks is True              # short pieces keep its answers live
    ctl.recorder.fast_live_chunks = False
    assert cal_flow["main"] == [("meetings", {"view": "live"})]
    assert cal_flow["marked"] == ["ev1"]                       # asked: never again
    assert cal_flow["tracked"] == [("calendar_meeting_started",
                                    {"source": "toast", "with_assist": True, "with_agenda": True})]
    # The open card says whose meeting it is: who is on it, and the agenda that was sent.
    assert cal_flow["card"].invites == [(CAL_AGENDA, ["Anna Smith", "David Lee"])]


def test_record_from_calendar_plain_record_sends_no_agenda(ctl, cal_flow):
    s = cal_flow["session"]()
    assert ctl.record_from_calendar(_cal_meeting(), with_assist=False) is True
    assert cal_flow["log"] == ["session.start"]                # no card is built for a plain record
    assert s.calls[0]["agenda"] == "" and s.calls[0]["title"] == "Weekly product sync"
    assert s.calls[0]["attendees"] == "Aram Adamyan, Anna Smith, David Lee"
    assert cal_flow["tracked"] == [("calendar_meeting_started",
                                    {"source": "toast", "with_assist": False, "with_agenda": False})]
    assert cal_flow["card"].invites == [("", ["Anna Smith", "David Lee"])]    # an open card: people, no agenda
    assert cal_flow["main"] == [("meetings", {"view": "live"})]
    assert ctl.recorder.fast_live_chunks is False


def test_record_from_calendar_works_with_a_recorder_that_has_no_agenda_yet(ctl, cal_flow):
    s = cal_flow["session"](with_agenda=False)
    assert ctl.record_from_calendar(_cal_meeting(), with_assist=True) is True
    assert "agenda" not in s.calls[0] and s.agendas == [CAL_AGENDA]       # set after the start
    ctl.recorder.fast_live_chunks = False


def test_record_from_calendar_private_event_never_sends_its_agenda(ctl, cal_flow):
    s = cal_flow["session"]()
    assert ctl.record_from_calendar(_cal_meeting(private=True), with_assist=True) is True
    assert s.calls[0]["agenda"] == ""
    assert cal_flow["tracked"][0][1]["with_agenda"] is False
    ctl.recorder.fast_live_chunks = False


def test_record_from_calendar_failure_prefills_start_card(ctl, cal_flow):
    s = cal_flow["session"](ok=False)
    assert ctl.record_from_calendar(_cal_meeting(), with_assist=True) is False
    assert ctl.recorder.fast_live_chunks is False              # not left on
    assert cal_flow["main"] == [("meetings", {"title": "Weekly product sync",
                                              "attendees": ["Anna Smith", "David Lee"],
                                              "calendar_event_id": "ev1", "agenda": CAL_AGENDA})]
    assert cal_flow["hints"][-1][:2] == ("Couldn't start the recording", "A meeting is already being recorded.")
    assert cal_flow["tracked"] == [] and cal_flow["marked"] == [] and cal_flow["card"].invites == []
    # A plain Record that fails fills the card too - with the agenda the user can read there.
    cal_flow["main"].clear()
    s.ok = False
    assert ctl.record_from_calendar(_cal_meeting(), with_assist=False) is False
    assert cal_flow["main"][0][1]["agenda"] == CAL_AGENDA and s.calls[-1]["agenda"] == ""


def test_record_from_calendar_without_a_session_or_while_busy(ctl, cal_flow, monkeypatch):
    monkeypatch.setattr(ctl, "meeting_session", None)
    assert ctl.record_from_calendar(_cal_meeting()) is False
    assert cal_flow["main"][-1][1]["calendar_event_id"] == "ev1"             # the card, filled in
    cal_flow["main"].clear()
    s = _install_session(ctl, monkeypatch, cal_flow["log"])
    s.is_busy = True
    assert ctl.record_from_calendar(_cal_meeting()) is False
    assert cal_flow["main"] == [("meetings", {})] and s.calls == []         # one at a time: the live view


def test_record_from_calendar_pro_gate_and_consent_come_first(ctl, cal_flow, monkeypatch):
    s = cal_flow["session"]()
    asked = []
    monkeypatch.setattr(ctl, "is_pro", lambda: False)
    monkeypatch.setattr(ctl, "_pro_upsell", lambda feature=None: asked.append(feature))
    assert ctl.record_from_calendar(_cal_meeting(), with_assist=True) is False
    assert asked == ["Meeting recording"] and s.calls == [] and cal_flow["tracked"] == []
    monkeypatch.setattr(ctl, "is_pro", lambda: True)
    import ui.recording_prompts as rp
    monkeypatch.setattr(rp, "ensure_recording_consent", lambda parent, app: False)    # the notice was refused
    assert ctl.record_from_calendar(_cal_meeting(), with_assist=True) is False
    assert s.calls == [] and cal_flow["log"] == []


def test_note_calendar_start_marks_asked_and_shows_the_invite(ctl, cal_flow):
    card = cal_flow["card"]
    ctl.note_calendar_start({"event_id": "ev7", "agenda": "A", "others": ["Anna"]}, "start_card", False)
    assert cal_flow["marked"] == ["ev7"] and card.invites == [("A", ["Anna"])]
    assert cal_flow["tracked"] == [("calendar_meeting_started",
                                    {"source": "start_card", "with_assist": False, "with_agenda": True})]
    # The Live card's own start: it already shows the invite; counted as "with assist".
    card.invites.clear()
    ctl.note_calendar_start({"event_id": "ev8", "agenda": ""}, "live_card")
    assert card.invites == [] and cal_flow["marked"] == ["ev7", "ev8"]
    assert cal_flow["tracked"][-1] == ("calendar_meeting_started",
                                       {"source": "live_card", "with_assist": True, "with_agenda": False})
    # A recording with no calendar meeting: nothing to mark, no chip.
    ctl.note_calendar_start({}, "start_card", False)
    assert cal_flow["marked"] == ["ev7", "ev8"] and card.invites == []


def test_calendar_telemetry_has_no_text_ids_or_counts(ctl, monkeypatch):
    tracked = []
    monkeypatch.setattr(ctl, "track", lambda event, props=None: tracked.append((event, props)))
    monkeypatch.setattr(ctl, "record_from_calendar", lambda m, with_assist=False: True)
    m = _cal_meeting()
    ctl._on_reminder_record(m, False)
    ctl._on_reminder_record(m, True)
    ctl._on_reminder_join(m)
    ctl._on_reminder_dismissed(m)
    ctl._on_reminder_closed(m, "expired")
    ctl._on_reminder_closed(m, "hidden")                       # a recording began elsewhere: not counted
    ctl._on_reminder_closed(m, "record")
    ctl._on_calendar_connect_result(True, "")
    ctl._on_calendar_connect_result(False, "admin")
    ctl._on_calendar_disconnected("user")
    ctl._on_calendar_disconnected("account")
    assert tracked == [
        ("calendar_reminder_action", {"action": "record"}),
        ("calendar_reminder_action", {"action": "record_assist"}),
        ("calendar_reminder_action", {"action": "join"}),
        ("calendar_reminder_action", {"action": "dismiss"}),
        ("calendar_reminder_action", {"action": "expired"}),
        ("calendar_connect_result", {"ok": True, "reason": ""}),
        ("calendar_connect_result", {"ok": False, "reason": "admin"}),
        ("calendar_disconnected", {"reason": "user"}),
        ("calendar_disconnected", {"reason": "account"}),
    ]
    import telemetry
    for event, props in tracked:
        assert event in telemetry.ALLOWED_EVENTS
        assert "ev1" not in repr(props) and "Weekly" not in repr(props)
    # An expiry still tells the user (and counts).
    ctl.hints.clear()
    ctl._on_calendar_disconnected("expired")
    assert tracked[-1] == ("calendar_disconnected", {"reason": "expired"})
    assert ctl.hints[-1][0] == "Google Calendar disconnected"


def test_the_toast_buttons_reach_the_controller(ctl, monkeypatch):
    # The toast's own signals are what the controller listens to (one toast, built once).
    seen = []
    monkeypatch.setattr(ctl, "track", lambda event, props=None: seen.append((event, props)))
    monkeypatch.setattr(ctl, "record_from_calendar", lambda m, with_assist=False: seen.append(("rec", with_assist)))
    import ui.floating.meeting_reminder as mr
    monkeypatch.setattr(mr.QDesktopServices, "openUrl", staticmethod(lambda url: True))   # no real browser
    toast = ctl._ensure_reminder()
    m = _cal_meeting()
    try:
        toast.show_meeting(m)
        toast._on_join()
        toast.record.emit(m, True)
        toast.dismissed.emit(m)
        toast._finish("expired")
    finally:
        toast.hide_reminder()
    assert ("calendar_reminder_action", {"action": "join"}) in seen
    assert ("calendar_reminder_action", {"action": "record_assist"}) in seen and ("rec", True) in seen
    assert ("calendar_reminder_action", {"action": "dismiss"}) in seen
    assert ("calendar_reminder_action", {"action": "expired"}) in seen


def test_on_calendar_remind_tray_fallback(ctl, monkeypatch):
    # The toast can't be built: the reminder is not lost - a tray note that opens Meetings.
    monkeypatch.setattr(ctl, "_ensure_reminder", lambda: 1 / 0)
    opened = []
    monkeypatch.setattr(ctl, "show_main", lambda page=None, **kw: opened.append(page))
    ctl.hints.clear()
    ctl._on_calendar_remind(_cal_meeting())
    title, text, on_click = ctl.hints[-1]
    assert title == "Record this meeting?" and text == "Weekly product sync is starting."
    on_click()
    assert opened == ["meetings"]


def test_calendar_busy_blocks_toast_without_marking(ctl, monkeypatch, tmp_path):
    # The service asks the controller whether it may; while a dictation / meeting runs it
    # answers no, and the meeting is not marked asked (it is asked when the user is free).
    from datetime import datetime, timezone
    from calendar_service import CalendarCore
    now = datetime.now(timezone.utc)
    m = _cal_meeting()
    api = type("Api", (), {"configured": staticmethod(lambda cfg=None: True),
                           "is_connected": staticmethod(lambda: True),
                           "people_label": staticmethod(lambda names: "Anna, David")})
    core = CalendarCore({}, api=api, state_path=tmp_path / "calendar_state.json", clock=lambda: now,
                        is_busy=ctl._calendar_busy)
    core.connected = True
    core.cache = [m]
    monkeypatch.setattr(ctl, "is_rec", True)
    assert core.take_reminder(now) is None and core.state.prompted == {}
    monkeypatch.setattr(ctl, "is_rec", False)
    got = core.take_reminder(now)
    assert got is not None and got["id"] == "ev1" and "ev1" in core.state.prompted


def test_setup_calendar_with_client_builds_service_and_tray_row(ctl, monkeypatch):
    from calendar_service import CalendarService
    saved = ctl.calendar
    monkeypatch.setattr(ctl, "calendar", None)
    monkeypatch.setitem(ctl.cfg, "google_oauth_client_id", "test-client.apps.googleusercontent.com")
    attached, heard = [], []
    monkeypatch.setattr(ctl.tray_menu, "attach_calendar", lambda cal: attached.append(cal))
    monkeypatch.setattr(ctl, "_on_calendar_remind", lambda m: heard.append(("remind", m["id"])))
    monkeypatch.setattr(ctl, "_on_calendar_disconnected", lambda reason: heard.append(("gone", reason)))
    monkeypatch.setattr(CalendarService, "start", lambda self: None, raising=False)   # no timers, no reads
    ctl._setup_calendar()
    cal = ctl.calendar
    try:
        assert isinstance(cal, CalendarService) and cal.available and not cal.is_connected
        assert attached == [cal]                                   # the tray's "Next" row follows it
        cal.remind.emit(_cal_meeting())
        cal.disconnected.emit("user")
        assert heard == [("remind", "ev1"), ("gone", "user")]
        # connect_result reaches the controller's telemetry.
        tracked = []
        monkeypatch.setattr(ctl, "track", lambda event, props=None: tracked.append((event, props)))
        cal.connect_result.emit(False, "cancelled")
        assert tracked == [("calendar_connect_result", {"ok": False, "reason": "cancelled"})]
        # Privacy mode pauses its reads (nothing leaves the computer); it is the service's own gate.
        monkeypatch.setitem(ctl.cfg, "privacy_mode", True)
        assert cal.core.network_allowed() is False
        monkeypatch.setitem(ctl.cfg, "privacy_mode", False)
        assert cal.core.network_allowed() is True
    finally:
        cal.stop()
        cal.deleteLater()
        QApplication.processEvents()
        monkeypatch.setattr(ctl, "calendar", saved)


def test_dormant_without_client_id_keeps_every_calendar_call_safe(ctl, monkeypatch):
    monkeypatch.setattr(ctl, "calendar", None)
    assert ctl.calendar_start_context() is None
    ctl.note_calendar_start({"event_id": "x", "agenda": ""}, "start_card", False)   # no service: no error
    ctl._on_reminder_dismissed({"id": "x"})
    assert ctl._calendar_busy() in (True, False)


def test_a_card_opened_mid_meeting_catches_up_with_the_invite(ctl, monkeypatch):
    from PySide6.QtCore import QObject, Signal

    class Session(QObject):
        sig_chunk = Signal(str)
        sig_summary = Signal(str)
        state = "recording"
        STATE_RECORDING = "recording"
        _meeting_title = "Weekly product sync"
        _meeting_attendees = "Aram Adamyan, Anna Smith, David Lee"
        _meeting_agenda = CAL_AGENDA
        _calendar_event_id = "ev1"
        _live_text = "We just started."
        _live_summary_text = ""
    from ui import live_assist as la_mod
    s = Session()
    monkeypatch.setattr(ctl, "meeting_session", s)
    monkeypatch.setattr(ctl, "live_assist", None)
    monkeypatch.setattr(la_mod.LiveAssistOverlay, "_me_name", lambda self: "Aram Adamyan")
    try:
        card = ctl._ensure_live_assist()
        assert card.lbl_title.text() == "Weekly product sync"
        assert card.has_invite() and card.invite_text() == CAL_AGENDA
        assert card.inv_chip.text.text() == "Anna, David · Agenda"      # the others, not you
    finally:
        card = ctl.live_assist
        ctl.live_assist = None
        if card is not None:
            card.hide()
            card.deleteLater()
            QApplication.processEvents()
    # A meeting that is not a calendar's: no chip.
    s._calendar_event_id, s._meeting_agenda = "", ""
    try:
        card = ctl._ensure_live_assist()
        assert not card.has_invite() and card.lbl_title.text() == "Weekly product sync"
    finally:
        card = ctl.live_assist
        ctl.live_assist = None
        if card is not None:
            card.hide()
            card.deleteLater()
            QApplication.processEvents()


def test_a_real_session_started_with_an_invite_shows_its_agenda_on_a_card_opened_mid_call(
        ctl, monkeypatch, tmp_path):
    """The card opened with the hotkey in the middle of a call: no stand-in for the session -
    the real MeetingSession is started with the invite and the card catches up from it."""
    import meeting_session as msess
    import live_context
    from types import SimpleNamespace
    from ui import live_assist as la_mod

    monkeypatch.setattr(msess.storage, "path_for", lambda *a, **k: tmp_path / "meetings")

    class Rec:
        fast_live_chunks = False
        on_chunk_complete = on_levels = on_partial = on_lang_detected = on_source_levels = None

        def start_recording(self, **kw):
            pass

        def stop_recording(self):
            pass

        def request_abort(self):
            pass

    app = SimpleNamespace(cfg={"meeting_consent_ack": True, "meeting_audio_mode": "default_mic",
                               "save_history": False}, recorder=Rec(), live_assist=None,
                          track=lambda *a, **k: None)
    s = msess.MeetingSession(app)
    try:
        ok, _code, msg = live_context.start_session(
            s, agenda=CAL_AGENDA, title="Weekly product sync",
            attendees="Aram Adamyan, Anna Smith, David Lee", source="calendar",
            calendar_event_id="ev1")
        assert ok, msg
        assert s._meeting_agenda == CAL_AGENDA
        monkeypatch.setattr(ctl, "meeting_session", s)
        monkeypatch.setattr(ctl, "live_assist", None)
        monkeypatch.setattr(la_mod.LiveAssistOverlay, "_me_name", lambda self: "Aram Adamyan")
        card = ctl._ensure_live_assist()
        try:
            assert card.lbl_title.text() == "Weekly product sync"
            assert card.has_invite() and card.invite_text() == CAL_AGENDA
            assert card.inv_chip.text.text() == "Anna, David · Agenda"
        finally:
            ctl.live_assist = None
            card.hide()
            card.deleteLater()
            QApplication.processEvents()
        # An edit on the card is kept by the session (and so reaches the notes).
        s.set_agenda("Only this")
        assert s._meeting_agenda == "Only this"
    finally:
        s.abort()
        s.deleteLater()
        QApplication.processEvents()
