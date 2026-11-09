"""The Interpreter's wiring in main.py and the Live Assistant page (review fixes), offscreen and with
fakes - no network, no sound devices:

* a meeting never records the Interpreter's own voice (AudioRecorder._keep_out_own_voice, the real
  _record_loop on scripted audio, AppController._interpreter_voice_playing on the real controller);
* Ctrl+Alt+I never opens the Pro window over another app for Free / guests, and the Pro window
  never stacks;
* the Live Assistant row says "in my voice" only for a voice that exists;
* the real config (main.DEFAULT through load_config) gives Armenian -> English for someone who
  dictates in Armenian; the app's auth signal stops a running interpreter.
"""
import json
import os
import sys
import tempfile
import threading
import time
import types
from types import SimpleNamespace
from unittest import mock

import numpy as np
from PySide6.QtCore import QObject
from PySide6.QtWidgets import QApplication

from tests_qt.test_interpreter_ui import FakeCtl, make_app

FRAME = 1024                                    # cfg chunk_size at 16 kHz: 64 ms a read


def _spin(n=10):
    for _ in range(n):
        QApplication.processEvents()
        time.sleep(0.002)


def _frame(level):
    t = np.arange(FRAME, dtype=np.float32) / 16000.0
    return (level * np.sin(2 * np.pi * 220 * t)).astype(np.float32)


# ── a meeting never records the Interpreter's own voice ──
def test_keep_out_own_voice_silences_the_call_and_from_speakers_a_quiet_mic():
    import main
    mic, loud, call = _frame(0.02), _frame(0.3), _frame(0.2)
    rec = SimpleNamespace(own_voice_playing=lambda: "")
    m, c = main.AudioRecorder._keep_out_own_voice(rec, mic, call)
    assert m is mic and c is call                                    # nothing plays: untouched
    rec.own_voice_playing = lambda: "headphones"
    m, c = main.AudioRecorder._keep_out_own_voice(rec, mic, call)
    assert m is mic and len(c) == len(call) and not c.any()          # the call: silent, same length
    rec.own_voice_playing = lambda: "speakers"
    m, c = main.AudioRecorder._keep_out_own_voice(rec, mic, call)
    assert not m.any() and not c.any()                               # the room's echo of it too
    m, _c = main.AudioRecorder._keep_out_own_voice(rec, loud, call)
    assert m is loud                                                 # talking over it: kept
    for odd in (lambda: 1 / 0, lambda: mock.MagicMock(), lambda: "yes"):
        rec.own_voice_playing = odd                                  # never breaks or empties a recording
        m, c = main.AudioRecorder._keep_out_own_voice(rec, mic, call)
        assert m is mic and c is call
    assert main.AudioRecorder._keep_out_own_voice(SimpleNamespace(), mic, None) == (mic, None)


def _loop(capture_mode, mic_level, call_level, own, reads_total=60):
    """The real _record_loop on scripted reads. ``own(n)`` says where the interpreter's voice
    plays during read n. Returns [(mic_rms, call_rms)] per read (meetings) and the rec."""
    import main
    reads, levels = [], []
    has_mic = capture_mode in ("smart_meeting", "default_mic", None)
    has_call = capture_mode in ("smart_meeting", "system_only")

    def tick():
        n = len(reads)
        reads.append(n)
        if n == reads_total:
            rec.stop_requested = True
        return n

    def read_mic(stream, sr):
        return _frame(mic_level(tick())).tobytes()

    def read_loop(stream):
        n = tick() if not has_mic else len(reads) - 1        # the mic read of this pass is the clock
        return _frame(call_level(n))

    rec = SimpleNamespace(
        recording=True, stop_requested=False, _abort=False, _capture_mode=capture_mode,
        fast_live_chunks=False, on_levels=None, _chunk_lock=threading.Lock(),
        _chunk_frames=[], _full_audio_truncated=False, _full_audio_frames=[],
        _full_audio_len=0, _full_audio_max=10 ** 9, _chunk_idx=0,
        _chunk_silence_before={}, _chunk_threads=[], _level_peak=0.05,
        _record_error="", _read_input_draining=read_mic, _read_available_loopback=read_loop,
        paused=False, _chunk_sources={}, on_source_levels=lambda m, c: levels.append((m, c)),
        _transcribe_chunk=lambda audio, idx: None,
        # 2.1.2+ recorder state (real-time transcription off here)
        _live_session=None, _live_fed_s=0.0, _got_audio=False, _tail_speech_sec=0.0,
        own_voice_playing=lambda: own(len(reads) - 1))
    with mock.patch.dict(main.cfg, {"sample_rate": 16000, "chunk_size": FRAME}):
        main.AudioRecorder._record_loop(rec, has_call, object(), object() if (has_call and has_mic) else None)
    for t in rec._chunk_threads:
        t.join(5)
    return levels, rec


def _window(n):
    return 20 <= n < 40


def test_a_meeting_with_the_call_never_keeps_the_translation_played_in_headphones():
    levels, _rec = _loop("smart_meeting", lambda n: 0.3, lambda n: 0.2,
                         lambda n: "headphones" if _window(n) else "")
    for n in (5, 45):
        assert levels[n][0] > 0.1 and levels[n][1] > 0.1           # you and the call, as before
    for n in (21, 30, 39):
        assert levels[n][0] > 0.1 and levels[n][1] == 0.0          # the call track is silent meanwhile


def test_from_speakers_the_mic_drops_the_translation_unless_you_talk_over_it():
    levels, _rec = _loop("smart_meeting", lambda n: 0.3 if n < 30 else 0.02, lambda n: 0.2,
                         lambda n: "speakers" if _window(n) else "")
    assert levels[25][0] > 0.1 and levels[25][1] == 0.0              # talking over it: your words stay
    assert levels[35] == (0.0, 0.0)                                  # only the room's echo: nothing kept
    assert levels[45][0] > 0.01 and levels[45][1] > 0.1              # quiet again: both back


def test_system_sound_only_and_mic_only_meetings_and_dictation():
    levels, _rec = _loop("system_only", lambda n: 0.0, lambda n: 0.2,
                         lambda n: "headphones" if _window(n) else "")
    assert levels[5][1] > 0.1 and levels[30] == (0.0, 0.0) and levels[45][1] > 0.1
    levels, _rec = _loop("default_mic", lambda n: 0.02, lambda n: 0.0,
                         lambda n: "speakers" if _window(n) else "")
    assert levels[5][0] > 0.01 and levels[30] == (0.0, 0.0) and levels[45][0] > 0.01
    levels, _rec = _loop("default_mic", lambda n: 0.02, lambda n: 0.0, lambda n: "headphones")
    assert all(m > 0.01 for m, _c in levels[:50])                    # headphones: the mic never hears it
    # Dictation is never touched, whatever plays.
    _levels, rec = _loop(None, lambda n: 0.02, lambda n: 0.0, lambda n: "speakers", reads_total=10)
    assert rec._full_audio_frames and all(
        np.frombuffer(f, dtype=np.float32).any() for f in rec._full_audio_frames)


def test_the_app_asks_the_running_interpreter_where_its_voice_plays():
    import main
    from interpreter import controller
    playing = {"on": False}
    sink = SimpleNamespace(is_playing=lambda tail_s=0.3: playing["on"])
    # The real engine object (not started: no devices), with a Hear direction playing on that sink.
    it = controller.Interpreter(lambda: "jwt", lambda e: None, controller.Settings(my_lang="en", their_lang="ja"),
                                audio=object(), session=object())
    app = SimpleNamespace(interpreter=SimpleNamespace(controller=None))
    assert main.AppController._interpreter_voice_playing(app) == ""          # not running
    app.interpreter.controller = it
    assert main.AppController._interpreter_voice_playing(app) == ""          # no Hear direction
    it._dirs["hear"] = SimpleNamespace(name="hear", sink=sink)
    assert main.AppController._interpreter_voice_playing(app) == ""          # quiet
    playing["on"] = True
    assert main.AppController._interpreter_voice_playing(app) == "headphones"
    it.speakers_mode = True
    assert main.AppController._interpreter_voice_playing(app) == "speakers"
    sink.is_playing = lambda tail_s=0.3: 1 / 0
    assert main.AppController._interpreter_voice_playing(app) == ""          # never breaks a recording
    assert main.AppController._interpreter_voice_playing(SimpleNamespace(interpreter=None)) == ""
    # An engine without its own helper: the Hear direction's sink is asked directly.
    sink.is_playing = lambda tail_s=0.3: True
    app.interpreter.controller = SimpleNamespace(_dirs={"hear": SimpleNamespace(sink=sink)}, speakers_mode=False)
    assert main.AppController._interpreter_voice_playing(app) == "headphones"
    # ...and AppController hands that to the recorder it builds.
    import inspect
    assert "self.recorder.own_voice_playing = self._interpreter_voice_playing" in inspect.getsource(
        main.AppController.__init__)


# ── Ctrl+Alt+I and the Pro window ──
def _hotkey_app(tier, active=False):
    calls = []
    host = SimpleNamespace(active=active, restarting=False,
                           toggle=lambda source="page", parent=None: calls.append(("toggle", source)))
    app = SimpleNamespace(
        interpreter=host, _hotkeys_suspended=False, calls=calls, is_pro=lambda: tier == "pro",
        show_tray_hint=lambda title, text, on_click=None: calls.append(("hint", title, text, on_click)),
        _pro_upsell=lambda feature=None: calls.append(("upsell", feature)),
        show_main=lambda *a, **k: calls.append(("show_main",) + a), _dialog_parent=lambda: None)
    return app


def test_the_shortcut_never_opens_the_pro_window_over_another_app():
    import main
    app = _hotkey_app("free")
    for _ in range(3):                                   # Ctrl+Alt+I is IntelliJ's / Word's too
        main.AppController.toggle_interpreter(app, "hotkey")
    hints = [c for c in app.calls if c[0] == "hint"]
    assert [c[:3] for c in app.calls] == [("hint", "Interpreter", "The interpreter is part of Gusan Pro.")]
    hints[0][3]()                                        # a click on it opens the Interpreter page
    assert ("show_main", "interpreter") in app.calls
    assert not [c for c in app.calls if c[0] in ("toggle", "upsell")]
    # The tray is a deliberate click: it still goes through the host (its Pro window).
    main.AppController.toggle_interpreter(app, "tray")
    assert app.calls[-1] == ("toggle", "tray")
    # Running (Pro just ended): the shortcut still turns it off.
    app = _hotkey_app("free", active=True)
    main.AppController.toggle_interpreter(app, "hotkey")
    assert app.calls == [("toggle", "hotkey")]
    # Pro: on and off as before, no hint.
    app = _hotkey_app("pro")
    main.AppController.toggle_interpreter(app, "hotkey")
    assert app.calls == [("toggle", "hotkey")]


def test_the_pro_window_never_stacks():
    import main
    made = []

    class FakeProDialog:
        def __init__(self, main_app=None, feature=None, parent=None):
            self.app, self.visible, self.raised = main_app, False, 0
            made.append(self)

        def isVisible(self):
            return self.visible

        def raise_(self):
            self.raised += 1

        def activateWindow(self):
            pass

        def exec(self):
            self.visible = True
            try:                                         # asked twice more while it is open
                self.app._pro_upsell("Interpreter")
                self.app._pro_upsell("Interpreter")
            finally:
                self.visible = False
            return 0

    app = SimpleNamespace(cfg={}, main_win=None)
    for name in ("_pro_upsell", "_dialog_parent"):
        setattr(app, name, getattr(main.AppController, name).__get__(app))
    fake = types.ModuleType("ui.pro_dialog")
    fake.ProDialog = FakeProDialog
    with mock.patch.dict(sys.modules, {"ui.pro_dialog": fake}), \
            mock.patch.object(main.telemetry, "track") as track:
        app._pro_upsell("Interpreter")
        assert len(made) == 1 and made[0].raised == 2 and track.call_count == 1
        made[0].exec = lambda: 0
        app._pro_upsell("Interpreter")                   # closed: the next ask opens a new one
        assert len(made) == 2 and track.call_count == 2


# ── the Live Assistant row: "in my voice" only for a voice that exists ──
def test_the_live_assistant_row_says_in_my_voice_only_for_a_voice_that_exists(qapp, monkeypatch):
    import live_context
    from ui.pages import live_assistant
    monkeypatch.setattr(live_assistant, "load_sessions", lambda: [])
    real = live_context.hide_status
    monkeypatch.setattr(live_context, "hide_status",
                        lambda wanted, **kw: real(wanted, **dict(dict(supported=True, remote=False, mac=False), **kw)))
    app = make_app("pro")
    app.live_assist = None
    p = live_assistant.create(app)
    try:
        app._sig.changed.connect(p.on_setting_changed)
        host = app.interpreter

        def hint():
            p._refresh_interpreter()
            return p.interp_hint.text()
        assert hint() == "They hear me · I hear them"                       # not known yet
        host.own_voice = True                                                # the last run's start said so
        assert hint() == "They hear me in my voice · I hear them"
        host.own_voice = False
        assert hint() == "They hear me · I hear them"
        # What the Interpreter page read from the server wins over the last run.
        page = SimpleNamespace(voice_tab=SimpleNamespace(voice_known=True, voice=None))
        app.main_win = SimpleNamespace(page=lambda route: page if route == "interpreter" else None)
        host.own_voice = True
        assert hint() == "They hear me · I hear them"                       # deleted since
        page.voice_tab.voice = {"voice_id": "v1"}
        host.own_voice = False
        assert hint() == "They hear me in my voice · I hear them"           # made since
        app.set_setting("interpreter_voice_on", False)                       # "a standard voice"
        assert p.interp_hint.text() == "They hear me · I hear them"
        app.set_setting("interpreter_voice_on", True)
        app.set_tier("free")                                                 # no Pro, no cloned voice
        assert hint() == "They hear me · I hear them"
    finally:
        p.deleteLater()


# ── the real config and the app's auth signal ──
def test_the_real_config_gives_armenian_to_english_for_someone_who_dictates_in_armenian(qapp):
    import main
    from interpreter import controller
    from ui import interpreter_host as ih
    tmp = tempfile.mkdtemp(prefix="gusan-review-cfg-")
    orig = main.CONFIG_PATH
    main.CONFIG_PATH = os.path.join(tmp, "config.json")
    try:
        for dictation, pair in (("hy", "HY ⇄ EN"), ("en", "EN ⇄ HY"), ("auto", "EN ⇄ HY")):
            with open(main.CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump({"language": dictation, "config_schema_version": 3}, f)
            cfg = main.load_config()                     # main.DEFAULT merged, as the app holds it
            assert ih.pair_codes(cfg) == pair, dictation
        s = controller.Settings.from_config(ih.settings_config({**main.DEFAULT, "language": "hy"}))
        assert (s.my_lang, s.their_lang) == ("hy", "en")                     # never Armenian -> Armenian
    finally:
        main.CONFIG_PATH = orig


def test_signing_out_stops_the_interpreter_through_the_apps_auth_signal(qapp):
    import main
    from ui import interpreter_host as ih
    app = main.AppController.__new__(main.AppController)
    QObject.__init__(app)
    hints = []
    app.cfg = {"language": "en", "interpreter_their_language": "ja", "privacy_mode": False}
    app.auth = SimpleNamespace(is_authenticated=True, is_pro=True, entitlement_known=True, user_id="u1",
                               get_access_token=lambda: "jwt")
    app.is_pro = lambda: bool(app.auth.is_pro)
    app.track = lambda *a, **k: None
    app.show_tray_hint = lambda *a, **k: hints.append(a)
    app.show_live_assist = lambda: None
    host = ih.InterpreterHost(app, factory=lambda tg, oe, st: FakeCtl(tg, oe, st))
    host.spawn = lambda fn, name="": fn()
    try:
        host.start(source="hotkey")
        assert host.state == "running"
        app.auth.is_authenticated, app.auth.user_id, app.auth.is_pro = False, None, False
        t = threading.Thread(target=app.sig_auth_changed.emit)          # auth's worker thread
        t.start()
        t.join()
        end = time.monotonic() + 2
        while host.state != "idle" and time.monotonic() < end:
            _spin(2)
        assert host.state == "idle"
    finally:
        host.deleteLater()
