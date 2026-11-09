"""Review fixes for the Interpreter's host (ui/interpreter_host.py) and the live card's interpreter parts
(ui/live_assist.py), offscreen with fakes - no network, no sound devices. Each section names the findings
it covers. ``deferred`` holds the worker threads back until ``drain()``, the way real threads take a moment
(a server call, a socket closing), so what happens in between can be checked."""
import threading
import time
from types import SimpleNamespace
from unittest import mock

import pytest
from PySide6.QtCore import QEvent, QObject, Signal
from PySide6.QtWidgets import QApplication

from tests_qt.conftest import make_stub_app


def _spin(n=5):
    for _ in range(n):
        QApplication.processEvents()
        time.sleep(0.002)


class FakeCtl:
    """interpreter.controller.Interpreter's surface. ``vmic`` False: Speak is refused (no virtual
    microphone). ``fail``: the start raises that code ("device": it opened, then a device failed)."""
    made = []

    def __init__(self, token_getter, on_event, settings, fail=None, vmic=True):
        self.on_event = on_event
        self.settings = settings
        self.calls = []
        self.session = SimpleNamespace(info={"seconds_left": 15120, "allowance_seconds": 18000, "own_voice": True})
        self.vmic = vmic
        self.fail = fail
        self._dirs = {}
        self._duck = None
        self.speakers_mode = False
        FakeCtl.made.append(self)

    def start(self):
        self.calls.append("start")
        if self.fail:
            from interpreter.session import InterpreterError
            if self.fail == "device":
                self.on_event({"type": "session", "state": "stopped", "reason": "failed", "seconds_left": 15000})
                raise InterpreterError("server")
            raise InterpreterError(self.fail)
        s = self.settings
        if s.speak and not self.vmic:
            self.on_event({"type": "error", "code": "no_virtual_mic", "direction": "speak"})
        self._dirs = {d: 1 for d, on in (("hear", s.hear), ("speak", s.speak and self.vmic)) if on}
        self.on_event({"type": "session", "state": "running", "seconds_left": 15120,
                       "speakers_mode": False, "directions": sorted(self._dirs)})

    def stop(self, reason="user"):
        self.calls.append(("stop", reason))
        self.on_event({"type": "session", "state": "stopped", "reason": reason, "seconds_left": 15000})

    def set_direction(self, name, on):
        self.calls.append(("direction", name, on))
        if on and name == "speak" and not self.vmic:
            self.on_event({"type": "error", "code": "no_virtual_mic", "direction": "speak"})
            return
        if on:
            self._dirs[name] = 1
        else:
            self._dirs.pop(name, None)
        self.on_event({"type": "session", "state": "running", "directions": sorted(self._dirs)})

    def latency(self):
        return 1.8

    def set_voice_choice(self, choice):
        self.calls.append(("voice", choice.mode))


class _Sigs(QObject):
    changed = Signal(str)
    auth = Signal()


def make_app(tier="pro", ctl_cls=FakeCtl, ctl_kw=None, **cfg):
    """make_stub_app + the interpreter's keys, set_setting that writes and announces (as
    AppController does), sig_auth_changed, and an InterpreterHost on fakes running its work inline."""
    from ui import interpreter_host as ih
    app = make_stub_app(tier)
    app.cfg.update({"language": "en", "interpreter_my_language": "", "interpreter_their_language": "ja",
                    "interpreter_hear": True, "interpreter_speak": True, "interpreter_hotkey": "ctrl+alt+i"})
    app.cfg.update(cfg)
    sigs = _Sigs()
    app._sigs = sigs
    app.sig_setting_changed = sigs.changed
    app.sig_auth_changed = sigs.auth
    app.auth.user_id = "u1" if tier != "guest" else None
    app.auth.entitlement_known = True

    def set_setting(key, value):
        app.calls.append(("set_setting", key, value))
        if app.cfg.get(key) != value or key not in app.cfg:
            app.cfg[key] = value
            sigs.changed.emit(key)
        return True

    app.set_setting = set_setting
    app.set_settings = lambda updates: {k: set_setting(k, v) for k, v in updates.items()}
    app.show_main = lambda *a, **k: app.calls.append(("show_main",) + a)
    app.show_tray_hint = lambda *a, **k: app.calls.append(("tray_hint",) + a)
    app.show_live_assist = lambda: app.calls.append(("show_live_assist",))
    app.schedule_config_save = lambda: None
    app.save_config = lambda: None
    kw = dict(ctl_kw or {})
    host = ih.InterpreterHost(app, factory=lambda tg, oe, st: ctl_cls(tg, oe, st, **kw), voices=object())
    host.spawn = lambda fn, name="": fn()
    app.interpreter = host
    return app


def deferred(host):
    """From now on the host's workers wait until drain() runs them (and what they start)."""
    jobs = []
    host.spawn = lambda fn, name="": jobs.append(fn)

    def drain():
        for _ in range(50):
            _spin(2)
            if not jobs:
                return
            while jobs:
                jobs.pop(0)()

    return jobs, drain


def _calls(app, name):
    return [c for c in app.calls if c[0] == name]


def _tracked(app, event):
    return [c[2] for c in app.calls if c[0] == "track" and c[1] == event]


@pytest.fixture(autouse=True)
def _fresh():
    FakeCtl.made.clear()
    yield
    _spin(2)


class _Win:
    """app.main_win on one page."""

    def __init__(self, route):
        self.route = route

    def isVisible(self):
        return True

    def isMinimized(self):
        return False

    def current_route(self):
        return self.route


# ── F32: Privacy mode on stops it ──
def test_privacy_mode_on_stops_a_running_interpreter(qapp):
    app = make_app("pro")
    host = app.interpreter
    host.toggle(source="hotkey")
    ctl = FakeCtl.made[-1]
    app.set_setting("privacy_mode", True)
    assert host.state == "idle" and ("stop", "privacy") in ctl.calls
    assert host.last_notice == ("privacy_stop", "Privacy mode is on, so the interpreter stopped.")


def test_privacy_mode_on_while_it_starts_or_restarts_keeps_it_off(qapp):
    app = make_app("pro")
    host = app.interpreter
    jobs, drain = deferred(host)
    host.toggle(source="card")
    assert host.state == "starting"
    app.set_setting("privacy_mode", True)
    assert not host.active                                  # shown as off at once
    drain()
    assert host.state == "idle" and ("stop", "privacy") in FakeCtl.made[-1].calls
    app.cfg["privacy_mode"] = False
    host.toggle(source="card")
    drain()
    assert host.state == "running"
    app.set_setting("interpreter_their_language", "fr")    # a restart: stopping first
    _spin(5)
    assert host.state == "stopping" and host.restarting
    app.set_setting("privacy_mode", True)
    assert not host.restarting
    drain()
    assert host.state == "idle" and len(FakeCtl.made) == 2


def test_privacy_mode_on_while_it_is_off_says_nothing(qapp):
    app = make_app("pro")
    app.set_setting("privacy_mode", True)
    assert app.interpreter.state == "idle" and app.interpreter.last_notice == ("", "")


# ── F33: settings changed while it starts ──
def test_a_language_picked_while_it_starts_restarts_it_with_that_language(qapp):
    app = make_app("pro")
    host = app.interpreter
    jobs, drain = deferred(host)
    host.toggle(source="card")
    app.set_setting("interpreter_their_language", "fr")
    drain()
    assert host.state == "running" and len(FakeCtl.made) == 2
    assert FakeCtl.made[0].settings.their_lang == "ja" and FakeCtl.made[-1].settings.their_lang == "fr"
    assert len(_calls(app, "show_live_assist")) == 1       # opened once, for the start


def test_speak_turned_off_while_it_starts_is_switched_off_once_it_runs(qapp):
    app = make_app("pro")
    host = app.interpreter
    jobs, drain = deferred(host)
    host.toggle(source="card")
    host.set_direction("speak", False)
    drain()
    ctl = FakeCtl.made[-1]
    assert host.state == "running" and len(FakeCtl.made) == 1
    assert ("direction", "speak", False) in ctl.calls and host.directions == ("hear",)


def test_a_voice_picked_while_it_starts_reaches_the_engine(qapp):
    app = make_app("pro")
    host = app.interpreter
    jobs, drain = deferred(host)
    host.toggle(source="card")
    app.set_setting("interpreter_voice_mode", "fixed")
    drain()
    assert host.state == "running" and ("voice", "fixed") in FakeCtl.made[-1].calls


def test_both_directions_off_while_it_starts_stops_it(qapp):
    app = make_app("pro")
    host = app.interpreter
    jobs, drain = deferred(host)
    host.toggle(source="card")
    host.set_direction("speak", False)
    host.set_direction("hear", False)
    drain()
    assert host.state == "idle"


# ── F34: the speech service gone for good ──
def test_the_speech_service_gone_for_good_starts_it_again_once_then_stops(qapp):
    app = make_app("pro")
    host = app.interpreter
    host.toggle(source="hotkey")
    ctl = FakeCtl.made[-1]
    ctl.on_event({"type": "state", "state": "stt_closed", "direction": "hear"})
    ctl.on_event({"type": "state", "state": "stt_closed", "direction": "speak"})
    _spin(10)
    assert host.state == "running" and len(FakeCtl.made) == 2 and ("stop", "restart") in ctl.calls
    FakeCtl.made[-1].on_event({"type": "state", "state": "stt_closed", "direction": "hear", "code": "network"})
    _spin(10)
    assert host.state == "idle" and len(FakeCtl.made) == 2      # gone again soon after: it stops, and says so
    assert host.last_notice == ("stt_lost", "Lost the speech service, so the interpreter stopped.")


def test_a_stop_after_a_long_sleep_and_a_lost_device_are_said_plainly(qapp):
    from ui import interpreter_host as ih
    app = make_app("pro")
    host = app.interpreter
    host.toggle(source="hotkey")
    FakeCtl.made[-1].on_event({"type": "session", "state": "stopped", "reason": "sleep", "seconds_left": 15000})
    assert host.state == "idle"
    assert host.last_notice == ("sleep", "The computer slept, so the interpreter stopped.")
    assert ih.message("device_lost").startswith("A sound device stopped working.")


def test_the_speech_service_closed_by_the_plan_stops_it_without_a_restart(qapp):
    app = make_app("pro")
    host = app.interpreter
    host.toggle(source="hotkey")
    ctl = FakeCtl.made[-1]
    ctl.on_event({"type": "error", "code": "allowance_used", "direction": "hear"})      # the engine says why
    ctl.on_event({"type": "state", "state": "stt_closed", "direction": "hear", "code": "allowance_used"})
    _spin(10)
    assert host.state == "idle" and len(FakeCtl.made) == 1 and ("stop", "stt") in ctl.calls
    assert host.last_notice == ("allowance_used", "You've used this month's interpreter time.")


class _DeafCtl(FakeCtl):
    """Its speech service closes for good while it starts (e.g. no credit left on the first connection)."""

    def start(self):
        super().start()
        self.on_event({"type": "state", "state": "stt_closed", "direction": "hear", "code": ""})


def test_the_speech_service_closed_while_it_starts_is_never_left_on(qapp):
    # F94 / F87: a run that can't hear is never kept on (and billed): once it runs, it starts
    # again with a fresh session, and when that can't hear either, it stops and says so - once
    app = make_app("pro", ctl_cls=_DeafCtl)
    host = app.interpreter
    host.toggle(source="hotkey")
    _spin(10)
    assert host.state == "idle" and len(FakeCtl.made) == 2
    assert ("stop", "restart") in FakeCtl.made[0].calls and ("stop", "stt") in FakeCtl.made[1].calls
    assert host.last_notice == ("stt_lost", "Lost the speech service, so the interpreter stopped.")
    assert [c[2] for c in _calls(app, "tray_hint")] == ["Lost the speech service, so the interpreter stopped."]


# ── F35 / F73: presses while it stops or restarts ──
def test_off_during_a_restart_stays_off(qapp):
    app = make_app("pro")
    host = app.interpreter
    host.toggle(source="card")
    jobs, drain = deferred(host)
    app.set_setting("interpreter_their_language", "fr")
    _spin(5)
    assert host.state == "stopping" and host.restarting
    assert host.active                                        # the chip, the tray and the switches show it on
    assert host.toggle(source="card") is False                # the chip or Ctrl+Alt+I: off
    assert not host.restarting and not host.active
    drain()
    assert host.state == "idle" and len(FakeCtl.made) == 1


def test_the_meeting_ending_during_a_restart_stops_it(qapp):
    app = make_app("pro", interpreter_with_meetings=True)
    host = app.interpreter
    host._on_meeting_phase("recording")
    assert host.state == "running"
    jobs, drain = deferred(host)
    app.set_setting("interpreter_their_language", "fr")
    _spin(5)
    assert host.state == "stopping"
    host._on_meeting_phase("processing")
    drain()
    assert host.state == "idle" and len(FakeCtl.made) == 1


def test_start_pressed_while_it_stops_starts_it_once_stopped(qapp):
    app = make_app("pro")
    host = app.interpreter
    host.toggle(source="card")
    jobs, drain = deferred(host)
    host.toggle(source="card")                                # off ...
    assert host.state == "stopping"
    assert host.toggle(source="card") is True                 # ... and on again before it has stopped
    assert host.restarting
    drain()
    assert host.state == "running" and len(FakeCtl.made) == 2
    assert len(_calls(app, "show_live_assist")) == 2          # asked for: the card opens as for any start


def test_on_again_while_it_is_still_starting_keeps_it_on(qapp):
    app = make_app("pro")
    host = app.interpreter
    jobs, drain = deferred(host)
    host.toggle(source="live_assistant")
    host.stop("user")                                         # off while it starts ...
    assert not host.active
    assert host.start(source="live_assistant") is True        # ... and on again
    assert host.active
    drain()
    assert host.state == "running"


# ── F36: the account ──
def test_signing_out_stops_it(qapp):
    app = make_app("pro")
    host = app.interpreter
    host.toggle(source="hotkey")
    app.set_tier("guest")
    app._sigs.auth.emit()
    assert host.state == "idle" and ("stop", "auth") in FakeCtl.made[-1].calls
    assert host.last_notice == ("auth", "Sign in again to use the interpreter.")


def test_the_end_of_pro_or_another_account_stops_it(qapp):
    app = make_app("pro")
    host = app.interpreter
    host.toggle(source="hotkey")
    app.auth.entitlement_known = False                        # not known yet (another account's first second)
    app.state.tier = "free"
    app._sigs.auth.emit()
    assert host.state == "running"
    app.auth.entitlement_known = True                         # the server says: not Pro
    app._sigs.auth.emit()
    assert host.state == "idle" and host.last_notice[0] == "pro_required"
    app.state.tier = "pro"
    host.toggle(source="hotkey")
    app.auth.user_id = "u2"                                   # another account signed in
    app._sigs.auth.emit()
    assert host.state == "idle" and host.last_notice == ("account", "The account changed, so the interpreter stopped.")


def test_the_auth_signal_from_a_worker_thread_reaches_the_gui_thread(qapp):
    app = make_app("pro")
    host = app.interpreter
    host.toggle(source="hotkey")
    app.set_tier("guest")
    t = threading.Thread(target=app._sigs.auth.emit)
    t.start()
    t.join()
    end = time.monotonic() + 2
    while host.state != "idle" and time.monotonic() < end:
        _spin(2)
    assert host.state == "idle"


# ── F38: quit gives the call its volume back first ──
@pytest.mark.parametrize("has_close", [True, False])
def test_quit_puts_the_call_volume_back_before_the_slow_stop(qapp, has_close):
    class OldDuck:
        def __init__(self):
            self.back = 0

        def restore(self):
            self.back += 1

    class Duck(OldDuck):
        def close(self):                                      # back, and never down again
            self.back += 1

    class SlowCtl(FakeCtl):
        def stop(self, reason="user"):
            time.sleep(0.4)                                   # sockets closing, threads joining
            self.stopped = True

    app = make_app("pro", ctl_cls=SlowCtl)
    host = app.interpreter
    host.toggle(source="hotkey")
    ctl = FakeCtl.made[-1]
    ctl._duck = Duck() if has_close else OldDuck()
    t0 = time.monotonic()
    host.shutdown(wait=0.05)
    assert time.monotonic() - t0 < 0.35 and ctl._duck.back >= 1
    end = time.monotonic() + 3
    while not getattr(ctl, "stopped", False) and time.monotonic() < end:
        time.sleep(0.02)


# ── F39 / F78: a notice on a page that doesn't show it ──
@pytest.mark.parametrize("route", ["live_assistant", "meetings"])
def test_a_failed_start_on_a_page_without_the_notice_reaches_the_tray(qapp, route):
    app = make_app("pro", ctl_kw={"fail": "network"})
    app.main_win = _Win(route)
    app.interpreter.start(source="live_assistant")
    assert app.interpreter.state == "idle"
    assert ("tray_hint", "Interpreter", "No connection to Gusan Cloud.") in app.calls


def test_on_the_interpreter_page_the_page_says_it(qapp):
    app = make_app("pro", ctl_kw={"fail": "network"})
    app.main_win = _Win("interpreter")
    app.interpreter.start()
    assert app.interpreter.last_notice[0] == "network" and not _calls(app, "tray_hint")


def test_the_meeting_hook_says_why_privacy_mode_kept_it_off(qapp):
    app = make_app("pro", interpreter_with_meetings=True, privacy_mode=True)
    app.main_win = _Win("meetings")
    app.interpreter._on_meeting_phase("recording")
    assert app.interpreter.state == "idle" and not _calls(app, "_pro_upsell")
    assert ("tray_hint", "Interpreter", "Privacy mode is on, so the interpreter didn't start.") in app.calls


# ── F40: never the same language on both sides ──
def test_their_language_is_never_mine():
    from ui import interpreter_host as ih
    cfg = {"language": "hy", "interpreter_my_language": "", "interpreter_their_language": "hy"}   # main.DEFAULT
    assert ih.their_language(cfg) == "en" and ih.pair_names(cfg) == "Armenian ⇄ English"
    assert ih.pair_codes(cfg) == "HY ⇄ EN"
    st = ih.settings_config(cfg)
    assert (st["interpreter_my_language"], st["interpreter_their_language"]) == ("hy", "en")
    assert ih.their_language({"language": "ja", "interpreter_their_language": "ja"}) == "hy"
    assert ih.their_language({"language": "en", "interpreter_their_language": "fr"}) == "fr"
    picked_mine = {"language": "en", "interpreter_my_language": "fr", "interpreter_their_language": "fr"}
    assert ih.their_language(picked_mine) == "hy"


# ── F41: the spoken notice (the engine says it; the host asks once per start) ──
def test_the_spoken_notice_is_asked_of_the_engine_once_per_start(qapp):
    from ui import interpreter_host as ih

    class Announcing(FakeCtl):
        def announce(self, text):
            self.calls.append(("announce", text))

    app = make_app("pro", ctl_cls=Announcing)
    host = app.interpreter
    host.toggle(source="hotkey")
    assert ("announce", ih.NOTICE_TEXT) in FakeCtl.made[-1].calls
    app.set_setting("interpreter_output", "speakers")         # a restart: not said again
    _spin(10)
    assert len(FakeCtl.made) == 2 and not [c for c in FakeCtl.made[-1].calls if c[0] == "announce"]
    host.toggle()
    app.cfg["interpreter_notice"] = False
    host.toggle()
    assert not [c for c in FakeCtl.made[-1].calls if c[0] == "announce"]
    host.toggle()
    app.cfg.update({"interpreter_notice": True, "interpreter_speak": False})   # Hear only: nobody to tell
    host.toggle()
    assert not [c for c in FakeCtl.made[-1].calls if c[0] == "announce"]


# ── F42: a second press while the Privacy question is open ──
def test_a_second_press_while_the_privacy_question_is_open_starts_it_once(qapp, monkeypatch):
    from ui import privacy_prompt
    app = make_app("pro", privacy_mode=True)
    host = app.interpreter
    asked = []

    def ask(parent, feature):
        asked.append(feature)
        if len(asked) == 1:
            host.toggle(source="hotkey")                      # the shortcut again, inside the open question
        return True

    monkeypatch.setattr(privacy_prompt, "confirm_turn_off_privacy", ask)
    host.toggle(source="hotkey")
    assert len(asked) == 2 and host.state == "running" and len(FakeCtl.made) == 1


# ── F43: a run that is stopping says nothing more ──
def test_an_engine_error_after_stop_is_not_said(qapp):
    app = make_app("pro")
    host = app.interpreter
    host.toggle(source="hotkey")
    ctl = FakeCtl.made[-1]
    jobs, drain = deferred(host)
    host.stop("user")
    assert host.state == "stopping"
    ctl.on_event({"type": "error", "code": "voice", "direction": "speak"})    # the voice socket closed mid-phrase
    drain()
    ctl.on_event({"type": "error", "code": "voice", "direction": "speak"})    # (and after it stopped)
    # only the stop's own sentence (Speak ran: F89), never the error
    assert host.last_notice == ("mic_back", "Switch your call's microphone back to your own mic.")
    assert _calls(app, "tray_hint") == [("tray_hint", "Interpreter", host.last_notice[1])]
    assert _tracked(app, "interpreter_failed") == []


# ── F89: after a stop nothing feeds the virtual microphone ──
def test_a_stop_of_a_run_with_speak_says_to_switch_the_call_mic_back(qapp):
    app = make_app("pro")
    host = app.interpreter
    host.toggle(source="card")
    assert host.directions == ("hear", "speak")
    host.toggle(source="card")                                   # the person turned it off
    assert host.state == "idle"
    assert host.last_notice == ("mic_back", "Switch your call's microphone back to your own mic.")
    host.toggle(source="card")
    assert host.last_notice == ("", "")                           # a new start clears it
    app.set_setting("interpreter_their_language", "fr")          # a restart: it comes back, nothing said
    _spin(10)
    assert host.state == "running" and host.last_notice == ("", "")
    host.stop("privacy")                                        # a stop with its own reason says that one
    assert host.last_notice == ("", "")


def test_a_hear_only_run_says_nothing_about_the_microphone(qapp):
    app = make_app("pro", interpreter_speak=False)
    host = app.interpreter
    host.toggle(source="card")
    assert host.directions == ("hear",)
    host.toggle(source="card")
    assert host.state == "idle" and host.last_notice == ("", "")


def test_a_start_that_fails_after_opening_is_counted_once(qapp):
    app = make_app("pro", ctl_kw={"fail": "device"})
    app.interpreter.start()
    assert app.interpreter.state == "idle"
    assert _tracked(app, "interpreter_failed") == [{"code": "server"}]
    assert _tracked(app, "interpreter_stopped") == []


def test_a_start_that_fails_after_it_was_turned_off_says_nothing(qapp):
    app = make_app("pro", ctl_kw={"fail": "network"})
    host = app.interpreter
    jobs, drain = deferred(host)
    host.start()
    host.stop("user")
    drain()
    assert host.state == "idle" and host.last_notice == ("", "")


# ── F46: a restart doesn't reopen the card ──
def test_a_restart_does_not_reopen_the_card(qapp):
    app = make_app("pro")
    host = app.interpreter
    host.toggle(source="hotkey")
    assert len(_calls(app, "show_live_assist")) == 1
    app.set_setting("interpreter_output", "speakers")
    _spin(10)
    assert host.state == "running" and len(FakeCtl.made) == 2
    assert len(_calls(app, "show_live_assist")) == 1


def test_another_microphone_picked_while_it_runs_restarts_it_with_that_one(qapp):
    app = make_app("pro", input_device_index=None)
    host = app.interpreter
    host.toggle(source="hotkey")
    app.set_setting("input_device_index", 3)                 # Settings › Microphone (the engine reads it at start)
    _spin(10)
    assert host.state == "running" and len(FakeCtl.made) == 2 and FakeCtl.made[-1].settings.mic == 3


# ── F72: quick taps on the directions ──
def test_two_quick_taps_turning_both_directions_off_stop_it(qapp):
    app = make_app("pro")
    host = app.interpreter
    host.toggle(source="card")
    jobs, drain = deferred(host)
    host.set_direction("speak", False)
    host.set_direction("hear", False)                         # before the first switch is done
    drain()
    assert host.state == "idle"


def test_off_then_on_quickly_ends_on(qapp):
    app = make_app("pro")
    host = app.interpreter
    host.toggle(source="card")
    jobs, drain = deferred(host)
    host.set_direction("hear", False)
    host.set_direction("hear", True)                          # a double click
    drain()
    assert host.state == "running" and host.directions == ("hear", "speak") and app.cfg["interpreter_hear"] is True


def test_nothing_left_once_the_switches_are_done_stops_it(qapp):
    app = make_app("pro", interpreter_speak=False, ctl_kw={"vmic": False})
    host = app.interpreter
    host.toggle(source="card")
    assert host.directions == ("hear",)
    jobs, drain = deferred(host)
    host.set_direction("speak", True)                         # refused by the engine: no virtual microphone
    host.set_direction("hear", False)
    drain()
    assert host.state == "idle"


# ── the live card ──
@pytest.fixture
def card(qapp):
    from ui import glass, live_assist
    made = []
    excl = mock.MagicMock(return_value=True)
    patches = [mock.patch.object(glass, "capture_exclusion_supported", return_value=True),
               mock.patch.object(glass, "is_remote_session", return_value=False),
               mock.patch.object(glass, "exclude_from_capture", excl),
               mock.patch.object(glass, "is_excluded_from_capture", return_value=True)]
    for pt in patches:
        pt.start()

    def build(tier="pro", **cfg):
        app = make_app(tier, **cfg)
        c = live_assist.LiveAssistOverlay(main_app=app)
        c.exclude_mock = excl
        made.append(c)
        return c, app

    yield build
    for c in made:
        c.hide()
        c.deleteLater()
    QApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    QApplication.processEvents()
    for pt in patches:
        pt.stop()


# F37: the row's mic and headphones
def test_the_card_row_follows_a_direction_switched_while_the_card_is_hidden(card):
    c, app = card("pro", interpreter_speak=False)
    host = app.interpreter
    host.toggle(source="hotkey")                              # (the card is not on screen: hidden or folded)
    assert host.state == "running" and host.directions == ("hear",)
    assert not c.interp_row.speak_switch.isChecked()
    jobs, drain = deferred(host)
    host.set_direction("speak", True)                         # from the Interpreter page
    drain()
    assert host.directions == ("hear", "speak") and c.interp_row.speak_switch.isChecked()


def test_a_click_on_the_card_mic_does_not_flip_back(card):
    c, app = card("pro")
    host = app.interpreter
    host.toggle(source="hotkey")
    row = c.interp_row
    jobs, drain = deferred(host)
    row.speak_switch.click()
    assert not row.speak_switch.isChecked()                   # the switch is under way: the choice shows
    drain()
    assert not row.speak_switch.isChecked() and host.directions == ("hear",)


# F44: the last direction off on the card
def test_the_last_direction_off_on_the_card_turns_it_off_and_the_chip_back_on(card):
    c, app = card("pro")
    host = app.interpreter
    c.btn_interp.click()
    c.interp_row.speak_switch.click()
    c.interp_row.hear_switch.click()                          # the last one: the interpreter goes off
    assert host.state == "idle" and not c.interp_row.isVisibleTo(c)
    assert app.cfg["interpreter_speak"] is False and app.cfg["interpreter_hear"] is True
    c.btn_interp.click()                                      # the chip turns it back on (Hear)
    assert host.state == "running" and host.directions == ("hear",)


# F45: the Privacy question asked from a card hidden from shares
def test_the_privacy_question_from_a_hidden_card_is_kept_out_of_shares(card, monkeypatch):
    from ui.components import mini_prompt
    asked = []
    monkeypatch.setattr(mini_prompt.MiniPrompt, "exec", lambda self: asked.append(self) or 0)
    c, app = card("pro", privacy_mode=True)
    c._private, c._exclusion_ok = True, True                  # the card is kept out of shares
    c.exclude_mock.reset_mock()
    c.btn_interp.click()
    assert len(asked) == 1 and app.interpreter.state == "idle"
    assert mock.call(asked[0], True) in c.exclude_mock.call_args_list
    assert c.hidden_from_share
    c._private = False                                        # Visible: asked like anywhere else
    assert not c.hidden_from_share
    c.exclude_mock.reset_mock()
    c.btn_interp.click()
    assert len(asked) == 2 and mock.call(asked[1], True) not in c.exclude_mock.call_args_list


# F48: the "+ Add context" line
def test_the_context_line_stays_hidden_under_the_invite_editor(card):
    c, app = card("pro")
    c.set_meeting_active(True, "Call with Anna", "Aram Adamyan, Anna Sato")
    c.set_context("We talk about the price.")
    c.set_invite("Agenda: pricing", ["Anna Sato"])
    c._open_invite_editor()
    assert c.inv_panel.isVisibleTo(c) and not c.ctx_chip.isVisibleTo(c)
    c.btn_interp.click()                                      # the interpreter on, then off, under the editor
    assert not c.ctx_chip.isVisibleTo(c)
    c.btn_interp.click()
    assert not c.ctx_chip.isVisibleTo(c)
    c._finish_invite_edit()
    assert c.ctx_chip.isVisibleTo(c)


def test_the_empty_context_line_stays_hidden_after_the_editor_while_the_interpreter_is_on(card):
    c, app = card("pro")
    assert c.ctx_chip.isVisibleTo(c)
    c.btn_interp.click()
    assert not c.ctx_chip.isVisibleTo(c)                      # empty: the directions take its place
    c._open_context_editor()
    c._finish_context_edit()
    assert not c.ctx_chip.isVisibleTo(c)
    c._open_invite_editor()                                   # (no invite: the editor still opens and closes)
    c._finish_invite_edit()
    assert not c.ctx_chip.isVisibleTo(c)
    c.btn_interp.click()                                      # off: the line is back
    assert c.ctx_chip.isVisibleTo(c)
