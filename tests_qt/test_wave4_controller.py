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
    import main
    from PySide6.QtWidgets import QMessageBox
    asked = []

    class Box(QMessageBox):
        def exec(self):                                   # closed with Esc / Cancel
            asked.append((self.windowTitle(), self.text()))
            return 0
    monkeypatch.setattr(main, "QMessageBox", Box)
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
    import main
    saved = []
    monkeypatch.setattr(main, "QMessageBox", None)            # any dialog would fail here
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
