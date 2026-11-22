"""The hourly "Still recording" reminder and the meeting's device notices (Gusan 2.1.11).

Session side (MeetingEngine, a fake clock through elapsed_sec's ``now``): every full hour of recording -
pauses not counted - asks once ("Still recording · 1 h", "2 h", ...); Keep recording = nothing until the
next hour. Device events from the recorder become short notices ("Switched to ..."), and a call that
stays silent while the microphone hears someone one hint. App side (main.AppController, unbound with a
stand-in): the card and a system notification - the tray's on Windows, Notification Center on a Mac
(through the tray, or AppleScript when the menu bar can't show one) - all mocked."""
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from unittest.mock import MagicMock, patch

import meeting_session as msess
import meeting_store as ms


class _Rec:
    def __init__(self):
        self.events, self.quiet = [], 0.0
        self.fast_live_chunks = False

    def start_recording(self, **kw):
        pass

    def stop_recording(self):
        pass

    def device_events(self):
        out, self.events = self.events, []
        return out

    def call_quiet_seconds(self):
        return self.quiet


class _App:
    def __init__(self):
        self.cfg = {"meeting_consent_ack": True, "save_history": False, "meeting_audio_mode": "default_mic"}
        self.recorder = _Rec()
        self.live_assist = None
        self.still = []
        self.notices = []
        self.hints = []
        self.notice_shows = True

    def save_config(self):
        pass

    def track(self, *a, **k):
        pass

    def is_pro(self):
        return True

    def show_still_recording(self, hours, title=""):
        self.still.append((hours, title))

    def show_meeting_notice(self, title, text="", icon="info"):
        self.notices.append((title, text, icon))
        return self.notice_shows

    def show_tray_hint(self, title, message, on_click=None):
        self.hints.append((title, message))


class _Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name) / "meetings"
        root.mkdir()
        for p in (patch.object(ms.storage, "path_for", return_value=root),
                  patch.object(msess.storage, "path_for", return_value=root),
                  patch.object(msess, "has_loopback", return_value=True),
                  patch.object(msess.telemetry, "track")):
            p.start()
            self.addCleanup(p.stop)
        self.app = _App()
        self.s = msess.MeetingEngine(self.app)
        self.s._spawn = lambda target, *args: None
        self.assertTrue(self.s.start("Weekly sync")[0])
        self.addCleanup(self.s._drop_spool)
        self.t0 = self.s._record_started_at


class TestEveryFullHour(_Base):
    def test_it_asks_once_at_every_full_hour_of_recording(self):
        s, t0 = self.s, self.t0
        for dt in (60, 1800, 3599):
            self.assertFalse(s._watch_hours(t0 + dt))
        self.assertTrue(s._watch_hours(t0 + 3600))
        self.assertEqual(self.app.still, [(1, "Weekly sync")])
        for dt in (3601, 3700, 7199):                       # Keep recording: nothing until the next hour
            self.assertFalse(s._watch_hours(t0 + dt))
        self.assertTrue(s._watch_hours(t0 + 7200))
        self.assertEqual(self.app.still, [(1, "Weekly sync"), (2, "Weekly sync")])

    def test_a_pause_is_not_recording_time(self):
        s, t0 = self.s, self.t0
        with patch.object(msess.time, "time", return_value=t0 + 1000):
            s.pause()
        self.assertFalse(s._watch_hours(t0 + 3700))         # paused: never asked
        with patch.object(msess.time, "time", return_value=t0 + 1600):
            s.resume()                                      # 600 s of pause
        self.assertFalse(s._watch_hours(t0 + 4100))          # 3500 s recorded
        self.assertTrue(s._watch_hours(t0 + 4200))           # 3600 s recorded
        self.assertEqual(self.app.still, [(1, "Weekly sync")])

    def test_not_after_stop_and_never_raises_without_the_app_hook(self):
        s, t0 = self.s, self.t0
        self.app.show_still_recording = None                 # (an app without the hook)
        self.assertTrue(s._watch_hours(t0 + 3600))           # (asked, nobody to show it: no error)
        s.state = msess.STATE_PROCESSING
        self.assertFalse(s._watch_hours(t0 + 7200))

    def test_the_recording_tick_asks(self):
        s = self.s
        s._record_started_at = s._record_started_at - 3600
        s._maybe_recap()
        self.assertEqual(self.app.still, [(1, "Weekly sync")])

    def test_a_new_meeting_starts_counting_again(self):
        s, t0 = self.s, self.t0
        s._watch_hours(t0 + 3600)
        s.abort()
        self.assertTrue(s.start("Next one")[0])
        self.assertFalse(s._watch_hours(s._record_started_at + 60))
        self.assertTrue(s._watch_hours(s._record_started_at + 3600))
        self.assertEqual(self.app.still[-1], (1, "Next one"))


class TestDeviceNotices(_Base):
    def test_a_switch_is_a_short_notice(self):
        self.app.recorder.events = [("call", "Headphones (WH-1000XM4 Stereo)"), ("mic", "Headset (AirPods)")]
        self.s._watch_devices()
        self.assertEqual(self.app.notices, [
            ("Switched to Headphones (WH-1000XM4 Stereo)", "The call's sound for this meeting", "speaker"),
            ("Switched to Headset (AirPods)", "Your microphone for this meeting", "mic")])
        self.app.recorder.events = [("mic_lost", "USB mic")]
        self.s._watch_devices()
        self.assertEqual(self.app.notices[-1][0], "Microphone disconnected")
        self.assertEqual(self.app.hints, [])

    def test_a_silent_call_is_said_once_and_again_after_a_switch(self):
        rec = self.app.recorder
        for q in (60.0, 179.0):
            rec.quiet = q
            self.s._watch_devices()
        self.assertEqual(self.app.notices, [])
        for q in (180.0, 240.0, 600.0):
            rec.quiet = q
            self.s._watch_devices()
        self.assertEqual([n[0] for n in self.app.notices], ["No sound from the call"])
        rec.events = [("call", "Speakers")]                 # another device: worth saying again
        rec.quiet = 0.0
        self.s._watch_devices()
        rec.quiet = 200.0
        self.s._watch_devices()
        self.assertEqual([n[0] for n in self.app.notices][-1], "No sound from the call")
        self.assertEqual(len(self.app.notices), 3)

    def test_with_the_window_hidden_the_silent_call_is_a_tray_hint(self):
        self.app.notice_shows = False
        self.app.recorder.quiet = 200.0
        self.s._watch_devices()
        self.assertEqual(self.app.hints, [msess.CALL_QUIET_NOTICE[:2]])

    def test_never_raises(self):
        self.app.recorder.device_events = MagicMock(side_effect=RuntimeError("x"))
        self.s._watch_devices()
        self.app.recorder = SimpleNamespace()                # a recorder without the feature
        self.s._watch_devices()


def _main(case):
    try:
        import main
    except Exception as e:                          # no PySide6 / audio libs here
        case.skipTest("main can't be imported here: %s" % e)
    if not isinstance(main.AppController, type):
        case.skipTest("PySide6 stubbed")
    return main


class TestTheAppSaysIt(unittest.TestCase):
    """main.AppController, called unbound on a stand-in (no window, no tray)."""

    def setUp(self):
        self.main = _main(self)
        self.card = MagicMock()
        self.app = SimpleNamespace(
            tray_icon=MagicMock(), show_tray_hint=MagicMock(), _ensure_still_card=lambda: self.card,
            _front_still_card=MagicMock(), meeting_session=None, show_main=MagicMock())
        self.app.system_notify = lambda *a, **k: self.main.AppController.system_notify(self.app, *a, **k)

    def test_windows_shows_the_card_and_a_tray_notification(self):
        AC = self.main.AppController
        with patch.object(self.main.sys, "platform", "win32"):
            AC.show_still_recording(self.app, 2, "Weekly sync")
        self.card.show_hours.assert_called_once()
        self.assertEqual(self.card.show_hours.call_args[0][:2], (2, "Weekly sync"))
        title, body = self.app.show_tray_hint.call_args[0][:2]
        self.assertEqual(title, "Still recording · 2 h")
        self.assertIn("Weekly sync", body)
        self.assertIs(self.app.show_tray_hint.call_args[1]["on_click"], self.app._front_still_card)

    def test_a_mac_uses_notification_center_through_the_menu_bar_icon(self):
        AC = self.main.AppController
        with patch.object(self.main.sys, "platform", "darwin"), \
                patch.object(self.main.QSystemTrayIcon, "isSystemTrayAvailable", return_value=True), \
                patch.object(self.main.QSystemTrayIcon, "supportsMessages", return_value=True), \
                patch.object(self.main, "_mac_notification") as script:
            AC.show_still_recording(self.app, 1, "")
        self.assertEqual(self.app.show_tray_hint.call_args[0][0], "Still recording · 1 h")
        script.assert_not_called()

    def test_a_mac_without_the_menu_bar_icon_asks_applescript(self):
        AC = self.main.AppController
        done = []
        # (both asked of a stand-in: the real ones need a QApplication)
        with patch.object(self.main.sys, "platform", "darwin"), \
                patch.object(self.main.QSystemTrayIcon, "isSystemTrayAvailable", return_value=True), \
                patch.object(self.main.QSystemTrayIcon, "supportsMessages", return_value=False), \
                patch.object(self.main, "_mac_notification", side_effect=lambda t, m: done.append((t, m))), \
                patch.object(self.main.threading, "Thread",
                             side_effect=lambda target, args, **kw: SimpleNamespace(start=lambda: target(*args))):
            self.assertTrue(AC.system_notify(self.app, "Still recording · 1 h", "Your meeting is still being recorded."))
        self.assertEqual(done, [("Still recording · 1 h", "Your meeting is still being recorded.")])
        self.app.show_tray_hint.assert_not_called()

    def test_the_applescript_is_quoted(self):
        run = MagicMock()
        self.assertTrue(self.main._mac_notification('Still "1 h"', 'a \\ b', run=run))
        cmd = run.call_args[0][0]
        self.assertEqual(cmd[:2], ["osascript", "-e"])
        self.assertEqual(cmd[2], 'display notification "a \\\\ b" with title "Still \\"1 h\\""')
        self.assertFalse(self.main._mac_notification("x", "y", run=MagicMock(side_effect=OSError("no"))))

    def test_stop_stops_the_meeting_and_keep_changes_nothing(self):
        AC = self.main.AppController
        session = SimpleNamespace(state="recording", stop=MagicMock())
        self.app.meeting_session = session
        AC._on_still_keep(self.app)
        session.stop.assert_not_called()
        AC._on_still_stop(self.app)
        session.stop.assert_called_once_with()
        session.state = "processing"
        AC._on_still_stop(self.app)
        self.assertEqual(session.stop.call_count, 1)

    def test_the_card_goes_when_the_meeting_stops(self):
        AC = self.main.AppController
        self.app._still_card = self.card
        AC._on_session_state_for_still(self.app, "paused")
        self.card.hide_card.assert_not_called()
        AC._on_session_state_for_still(self.app, "processing")
        self.card.hide_card.assert_called_once()
