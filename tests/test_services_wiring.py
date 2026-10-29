"""Gusan 2.0 Wave 4: the services AppController owns, and how main.py uses them
(config migration, the speech model through the download manager, the
updater's calm prompts, the meeting session, history / recording signals).

Pure logic: controllers are SimpleNamespace / __new__ doubles with the real
methods bound, under test_core's stand-ins (no Qt, no audio, no network)."""
import json
import os
import sys
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest import mock

try:
    from tests import test_core  # noqa: F401  (importing installs the stubs)
except ImportError:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import test_core  # noqa: F401

import main
import model_manager
import updater


def bind(app, *names):
    for name in names:
        setattr(app, name, getattr(main.AppController, name).__get__(app))
    return app


class _Inline:
    """threading.Thread that runs its target at start()."""

    def __init__(self, target=None, args=(), kwargs=None, daemon=None, name=None):
        self.target, self.args = target, args

    def start(self):
        self.target(*self.args)


# ── config ────────────────────────────────────────────────────────────────────

class TestConfigMigration(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = main.CONFIG_PATH
        main.CONFIG_PATH = os.path.join(self._tmp.name, "config.json")

    def tearDown(self):
        main.CONFIG_PATH = self._orig
        self._tmp.cleanup()

    def _load(self, disk):
        with open(main.CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(disk, f)
        return main.load_config()

    def test_a_1_9_config_keeps_its_language_for_files(self):
        c = self._load({"language": "ru", "config_schema_version": 1, "whisper_model": "small",
                        "live_assist_language": "en", "live_assist_output_language": "en",
                        "some_1_9_key": 7})
        self.assertEqual(c["config_schema_version"], 3)
        self.assertEqual(c["file_language"], "ru")            # 1.9 file jobs used it
        self.assertEqual(c["live_assist_language"], "en")     # a saved choice stays
        self.assertEqual(c["some_1_9_key"], 7)                # nothing is dropped
        self.assertEqual(c["whisper_model"], "small")

    def test_a_config_without_a_version_is_a_1_9_one(self):
        c = self._load({"language": "multi"})
        self.assertEqual(c["file_language"], "multi")
        self.assertEqual(c["config_schema_version"], 3)
        self.assertEqual(c["smart_actions_hotkey"], "alt+s")      # the Smart Actions shortcut

    def test_a_2_0_config_is_left_alone(self):
        c = self._load({"language": "ru", "config_schema_version": 2})
        self.assertEqual(c["file_language"], "auto")          # its own choice (the default)
        c = self._load({"language": "ru", "file_language": "de", "config_schema_version": 1})
        self.assertEqual(c["file_language"], "de")            # never overwritten

    def test_only_1_x_upgraders_get_the_rename_line_once(self):
        for disk, due in (({"config_schema_version": 1, "previous_version": ""}, True),
                          ({"language": "en"}, True),                          # 1.x without a version
                          ({"config_schema_version": 2}, False),
                          ({"config_schema_version": 2, "rename_note_pending": False}, False)):
            self.assertIs(self._load(disk)["rename_note_pending"], due, disk)
        # Shown (or closed) once: the saved 2.0 config keeps it off.
        c = self._load({"config_schema_version": 1})
        c["rename_note_pending"] = False
        main.save_config(c)
        self.assertIs(main.load_config()["rename_note_pending"], False)
        # A fresh install reads the defaults: no line.
        for path in (main.CONFIG_PATH, main.CONFIG_PATH + ".bak"):
            if os.path.exists(path):
                os.remove(path)
        self.assertIs(main.load_config()["rename_note_pending"], False)

    def test_new_defaults(self):
        d = main.DEFAULT
        for key, value in {"file_model": "auto", "file_language": "auto", "file_speakers": True,
                           "file_timestamps": True, "file_save_word": True,
                           "notify_when_ready": True, "calendar_ask_to_record": True,
                           "calendar_only_with_link": True, "meeting_language": "",
                           "meeting_notes_model": "", "meeting_consent_reminder": True,
                           "live_assist_language": "", "live_assist_output_language": "",
                           "live_assist_writer": "", "install_updates_on_quit": True}.items():
            self.assertEqual(d[key], value, key)
        self.assertNotIn("launch_at_login", d)                # autostart decides it


# ── the speech model ──────────────────────────────────────────────────────────

class _Info:
    def __init__(self, language):
        self.language = language


class _Seg:
    def __init__(self, start, end, text):
        self.start, self.end, self.text = start, end, text


class TestSegmentsLanguage(unittest.TestCase):
    def _rec(self):
        rec = main.AudioRecorder.__new__(main.AudioRecorder)
        rec._infer_lock = threading.Lock()
        rec.language_override = None
        return rec

    def _model(self, calls, old_wheel=False, detected="hy"):
        def transcribe(audio, **kw):
            if old_wheel and "multilingual" in kw:
                raise TypeError("transcribe() got an unexpected keyword argument 'multilingual'")
            calls.append(kw)
            return iter([_Seg(0.0, 1.0, " Barev ")]), _Info(detected)
        return SimpleNamespace(transcribe=transcribe)

    def _run(self, model, language):
        with mock.patch.dict(main.cfg, {"language": "en"}):
            return main.AudioRecorder._segments_with(
                self._rec_obj, model, [0.0] * 16000, 16000, language, None, None)

    def test_mixed_languages_decode_per_window_and_keep_the_detected_one(self):
        self._rec_obj, calls = self._rec(), []
        out = self._run(self._model(calls), "multi")
        self.assertTrue(calls[0]["multilingual"])
        self.assertIsNone(calls[0]["language"])
        self.assertEqual(out[0]["text"], "Barev")
        self.assertEqual(self._rec_obj._last_segments_language, "hy")

    def test_an_older_faster_whisper_still_works(self):
        self._rec_obj, calls = self._rec(), []
        out = self._run(self._model(calls, old_wheel=True), "multi")
        self.assertNotIn("multilingual", calls[0])
        self.assertEqual(len(out), 1)

    def test_an_explicit_language_wins_over_the_session(self):
        self._rec_obj, calls = self._rec(), []
        self._rec_obj.language_override = "ru"          # the last meeting's
        self._run(self._model(calls, detected="fr"), "fr")
        self.assertEqual(calls[0]["language"], "fr")
        self.assertNotIn("multilingual", calls[0])
        calls.clear()
        self._run(self._model(calls), "auto")           # a file job's "Detect"
        self.assertIsNone(calls[0]["language"])


class TestModelFetcherHook(unittest.TestCase):
    def _rec(self, fetch):
        rec = main.AudioRecorder.__new__(main.AudioRecorder)
        rec._model, rec._model_name = None, None
        rec.model_fetcher = fetch
        built = []
        rec._build_model_locked = lambda name: built.append((name, rec._fetching_model))
        return rec, built

    def test_a_missing_model_is_fetched_before_the_build(self):
        seen = []
        rec, built = self._rec(lambda name: seen.append((name, rec._fetching_model)))
        main.AudioRecorder._load_locked(rec, "small")
        self.assertEqual(seen, [("small", True)])        # transcribe() waits meanwhile
        self.assertEqual(built, [("small", False)])
        self.assertFalse(rec._building_model)

    def test_a_failed_download_fails_the_load(self):
        def fetch(name):
            raise model_manager.DownloadError("Not enough free disk space for this model.")
        rec, built = self._rec(fetch)
        with self.assertRaises(model_manager.DownloadError):
            main.AudioRecorder._load_locked(rec, "small")
        self.assertEqual(built, [])
        self.assertFalse(rec._fetching_model)
        self.assertFalse(rec._building_model)


class _Downloads:
    def __init__(self, fail=None):
        self.calls = []
        self.fail = fail

    def kind(self, name):
        return "whisper" if name in ("small", "base") else ("llm" if name == "qwen_tiny" else None)

    def fetch(self, name, on_progress=None, timeout=None):
        self.calls.append((name, timeout))
        if on_progress is not None:
            on_progress(42, 1, 2)
        if self.fail:
            raise model_manager.DownloadError(self.fail)
        return name


class TestFetchSpeechModel(unittest.TestCase):
    def _app(self, dl):
        posted = []
        app = SimpleNamespace(downloads=dl, _model_wait=None, _busy=False,
                              overlay=SimpleNamespace(call_soon=lambda fn, *a: posted.append(fn)))
        app.SPEECH_MODEL_WAIT_S = main.AppController.SPEECH_MODEL_WAIT_S
        bind(app, "_fetch_speech_model", "_run_on_gui", "_on_model_wait_progress",
             "_show_model_wait", "_end_model_wait")
        app.posted = posted
        return app

    def _on_worker(self, fn, *args):
        box = {}

        def run():
            try:
                box["result"] = fn(*args)
            except BaseException as e:      # noqa: B902
                box["error"] = e
        t = threading.Thread(target=run)
        t.start()
        t.join(5)
        return box

    def test_a_missing_model_joins_the_shared_download(self):
        dl = _Downloads()
        app = self._app(dl)
        waits = []
        dl.fetch = (lambda f: lambda name, on_progress=None, timeout=None: (
            waits.append(dict(app._model_wait)), f(name, on_progress, timeout)))(dl.fetch)
        with mock.patch.object(main, "model_downloaded", return_value=False):
            box = self._on_worker(app._fetch_speech_model, "small")
        self.assertNotIn("error", box)
        self.assertEqual(dl.calls, [("small", main.AppController.SPEECH_MODEL_WAIT_S)])
        self.assertEqual(waits[0]["name"], "small")
        self.assertIsNone(app._model_wait)              # over: the pill goes back
        self.assertIn(app._end_model_wait, app.posted)

    def test_on_disk_custom_or_gui_thread_never_waits(self):
        dl = _Downloads()
        app = self._app(dl)
        with mock.patch.object(main, "model_downloaded", return_value=True):
            self._on_worker(app._fetch_speech_model, "small")
        with mock.patch.object(main, "model_downloaded", return_value=False):
            self._on_worker(app._fetch_speech_model, "my-finetune")   # not a catalog id
            app._fetch_speech_model("small")                         # GUI (main) thread
        self.assertEqual(dl.calls, [])

    def test_one_failure_answers_every_waiting_piece(self):
        dl = _Downloads(fail="Not enough free disk space for this model.")
        app = self._app(dl)
        with mock.patch.object(main, "model_downloaded", return_value=False):
            first = self._on_worker(app._fetch_speech_model, "small")
            second = self._on_worker(app._fetch_speech_model, "small")
        self.assertIsInstance(first["error"], model_manager.DownloadError)
        self.assertEqual(str(second["error"]), "Not enough free disk space for this model.")
        self.assertEqual(len(dl.calls), 1)               # not one download per piece

    def test_the_pill_says_getting_ready_only_after_recording(self):
        from ui import overlay as ov_mod
        dl = _Downloads()
        app = self._app(dl)
        states = []
        app.overlay = SimpleNamespace(overlay_state=ov_mod.TRANSCRIBING,
                                      set_state=lambda s: (states.append(s),
                                                           setattr(app.overlay, "overlay_state", s)),
                                      set_model_progress=lambda pct: states.append(("pct", pct)))
        app._model_wait = {"name": "small", "pct": 42}
        app._busy = False                                # still recording: no change
        app._show_model_wait()
        self.assertEqual(states, [])
        app._busy = True                                 # stopped, waiting for the model
        app._show_model_wait()
        self.assertEqual(states, [("pct", 42), ov_mod.GETTING_READY])
        app._end_model_wait()
        self.assertEqual(states[-1], ov_mod.TRANSCRIBING)


# ── updates ───────────────────────────────────────────────────────────────────

class _Sig:
    def __init__(self):
        self.sent = []

    def emit(self, *a):
        self.sent.append(a)


class TestCalmUpdates(unittest.TestCase):
    def _app(self, **cfg):
        app = SimpleNamespace(cfg=dict(cfg), is_rec=False, _update_notice_shown=None,
                              _update_ready=None, _update_fetching=None, _update_installing=False,
                              hints=[], tracked=[], saves=0,
                              sig_setting_changed=_Sig(), sig_update_available=_Sig())
        app.show_tray_hint = lambda t, m, on_click=None: app.hints.append((t, m, on_click))
        app.track = lambda e, p=None: app.tracked.append((e, p))
        app.save_config = lambda: None
        app.schedule_config_save = lambda: setattr(app, "saves", app.saves + 1)
        app._prepare_update_on_quit = lambda tag: setattr(app, "prepared", tag)
        bind(app, "_prompt_update", "note_update_check", "_nudge_update_after_dictation",
             "_run_on_gui")
        app._is_newer = main.AppController._is_newer
        return app

    def test_one_quiet_notice_a_run_never_a_modal(self):
        app = self._app()
        # (the real ui.components needs Qt's SVG module, which these tests' stand-ins of Qt do not
        # have: a stand-in dialog module catches any attempt to open a dialog all the same)
        import types
        dialogs = types.ModuleType("ui.components.mini_prompt")
        dialogs.confirm = mock.MagicMock()
        package = types.ModuleType("ui.components")
        package.__path__ = []
        package.mini_prompt = dialogs
        modal = dialogs.confirm
        with mock.patch.object(main, "APP_VERSION", "2.0.0"), \
                mock.patch.dict(sys.modules, {"ui.components": package,
                                              "ui.components.mini_prompt": dialogs}):
            app._prompt_update("v2.0.1")
            app._prompt_update("v2.0.1")                 # after the next dictation
        modal.assert_not_called()
        self.assertEqual(len(app.hints), 1)
        title, text, on_click = app.hints[0]
        self.assertEqual(title, "Update available")
        self.assertIn("v2.0.1", text)
        self.assertTrue(callable(on_click))

    def test_not_now_and_recording_keep_it_quiet(self):
        app = self._app(dismissed_update_version="v2.0.1")
        with mock.patch.object(main, "APP_VERSION", "2.0.0"):
            app._prompt_update("v2.0.1")
            app.cfg["dismissed_update_version"] = ""
            app.is_rec = True
            app._prompt_update("v2.0.1")
        self.assertEqual(app.hints, [])

    def test_a_check_stores_pending_and_asks_only_when_due(self):
        app = self._app()
        with mock.patch.object(main, "APP_VERSION", "2.0.0"):
            app.note_update_check(updater.AVAILABLE, "v2.0.1", manual=False)
        self.assertEqual(app.cfg["pending_update_version"], "v2.0.1")
        self.assertEqual(app.saves, 1)
        self.assertEqual(app.sig_setting_changed.sent, [("pending_update_version",)])
        self.assertEqual(app.sig_update_available.sent, [("v2.0.1",)])
        self.assertEqual(app.prepared, "v2.0.1")                 # fetched ahead for Quit

        app = self._app()
        with mock.patch.object(main, "APP_VERSION", "2.0.0"):
            app.note_update_check(updater.DISMISSED, "v2.0.1", manual=False)
            app.note_update_check(updater.AVAILABLE, "v2.0.1")   # Settings' Check now
        self.assertEqual(app.cfg["pending_update_version"], "v2.0.1")  # still says it
        self.assertEqual(app.sig_update_available.sent, [])

        app = self._app(pending_update_version="v2.0.1")
        app.note_update_check(updater.LATEST, "v2.0.1", manual=False)
        self.assertEqual(app.cfg["pending_update_version"], "")

    def test_the_dictation_nudge_never_waits_and_shows_once(self):
        app = self._app(pending_update_version="v2.0.1")
        scheduled = []
        with mock.patch.object(main, "APP_VERSION", "2.0.0"), \
                mock.patch.object(main.updater, "APP_VERSION", "2.0.0", create=True), \
                mock.patch.object(main, "QTimer", SimpleNamespace(
                    singleShot=lambda ms, fn: scheduled.append(ms))):
            app._nudge_update_after_dictation()
            app._update_notice_shown = "v2.0.1"
            app._nudge_update_after_dictation()
        self.assertEqual(scheduled, [2500])


def _update_state():
    """The controller's in-app update state (main.AppController.__init__), at rest."""
    return dict(_update_ready=None, _update_fetching=None, _update_fetch_user=False, _update_installing=False,
                _update_closing=False, _update_window=None, _update_cancel=False, _update_gen=0,
                _update_queued=None, _update_tag="", _update_manual=True, _update_on_done=None,
                _update_key_asked=False, _update_force=False, _update_asking=False,
                _update_failed_notice=False, _update_progress_at=0.0)


_UPDATE_METHODS = ("install_update", "_update_blocker", "_update_stale", "_update_window_for", "_update_done",
                   "_update_failed", "_update_handover", "_update_start_installer", "_on_update_launched",
                   "_start_update_fetch", "_on_update_fetched", "_update_poll", "_update_unsaved_edits",
                   "_cancel_update", "_update_install_now", "_update_retry", "_update_open_page", "_run_on_gui")


class TestInstallUpdate(unittest.TestCase):
    """install_update's contract with its callers (Settings' row, the tray notice); the whole flow, its
    window and every edge are in tests/test_update_flow.py."""

    def _app(self, **cfg):
        app = SimpleNamespace(cfg={"pending_update_version": "v2.0.1", **cfg}, hints=[], tracked=[],
                              flushed=0, is_rec=False, _file_job_running=False, **_update_state())
        app.show_tray_hint = lambda t, m, on_click=None: app.hints.append((t, m))
        app.track = lambda e, p=None: app.tracked.append((e, p))
        app.flush_config_save = lambda: setattr(app, "flushed", app.flushed + 1)
        app.quit_app = lambda: None
        app._bring_to_front = lambda w: None
        app._is_newer = main.AppController._is_newer
        bind(app, *_UPDATE_METHODS)
        return app

    def _install(self, app, fetched, installable=True, **kw):
        """install_update with the download answering ``fetched`` ((ok, path or message))."""
        import types
        timers, window = [], mock.MagicMock()
        module = types.ModuleType("ui.update_window")
        module.UpdateWindow = mock.MagicMock(return_value=window)
        with mock.patch.object(main, "APP_VERSION", "2.0.0"), \
                mock.patch.object(main.sys, "platform", "win32"), \
                mock.patch.object(main.threading, "Thread", _Inline), \
                mock.patch.object(main.updater, "cached_download", return_value=fetched), \
                mock.patch.object(main.updater, "launch_installer", return_value=(True, "C:/t/setup.exe")) as launch, \
                mock.patch.object(main.updater, "spawn_relauncher", return_value=object()), \
                mock.patch.object(main.updater, "can_self_install", return_value=installable), \
                mock.patch.object(main.updater, "is_all_users_install", return_value=False), \
                mock.patch.object(main.updater, "new_log_path", return_value="C:/t/u.log"), \
                mock.patch.object(main, "QTimer", SimpleNamespace(
                    singleShot=lambda ms, fn: timers.append(fn))), \
                mock.patch.dict(sys.modules, {"ui.update_window": module}), \
                mock.patch.object(main.webbrowser, "open") as browser:
            app.install_update("v2.0.1", **kw)
        return timers, browser, window, launch

    def test_an_installed_update_keeps_pending_records_the_attempt_and_quits_properly(self):
        app = self._app()
        with tempfile.NamedTemporaryFile(suffix=".exe", delete=False) as f:
            path = f.name
        try:
            timers, _, window, launch = self._install(app, (True, path), manual=False)
        finally:
            os.unlink(path)
        launch.assert_called_once_with(path, all_users=False, log_path="C:/t/u.log", relaunch=True)
        self.assertEqual(app.cfg["pending_update_version"], "v2.0.1")        # until the new version actually runs
        self.assertEqual(app.cfg["update_attempt"], {"to": "v2.0.1", "log": "C:/t/u.log", "from": "2.0.0"})
        self.assertEqual(app.flushed, 1)
        self.assertEqual(timers, [app.quit_app])                              # the one proper quit, after a moment
        self.assertIn(("update_install_started", {"manual": False, "to": "v2.0.1"}), app.tracked)
        self.assertIn(("update_install_result", {"manual": False, "ok": True}), app.tracked)

    def test_a_failure_goes_to_the_page_and_the_window_not_a_notification(self):
        app = self._app()
        got = []
        timers, _, window, launch = self._install(app, (False, "Couldn't download the update."),
                                                  on_done=lambda ok, info: got.append((ok, info)))
        self.assertEqual(got, [(False, "Couldn't download the update.")])
        window.show_failed.assert_called_once_with("Couldn't download the update.", "")
        self.assertEqual(app.hints, [])
        self.assertEqual(timers, [])
        launch.assert_not_called()
        self.assertEqual(app.cfg["pending_update_version"], "v2.0.1")
        app = self._app()
        _, _, window, _ = self._install(app, (False, "Couldn't download the update."))      # the notice's click
        window.show_failed.assert_called_once()                    # the window says it: no second voice
        self.assertEqual(app.hints, [])

    def test_on_a_mac_or_a_portable_copy_the_release_page_opens(self):
        app = self._app()
        got = []
        _, browser, window, launch = self._install(app, (True, "x"), installable=False,
                                                   on_done=lambda ok, info: got.append((ok, info)))
        browser.assert_called_once_with(updater.release_page("v2.0.1"))
        self.assertEqual(got, [(False, "")])                       # the row resets with no red text
        self.assertEqual(app.hints[0][1], "Opening the download page in your browser.")
        launch.assert_not_called()
        window.show_downloading.assert_not_called()

    def test_install_on_quit_runs_the_installer_fetched_ahead_quietly_for_this_user(self):
        app = SimpleNamespace(cfg={"install_updates_on_quit": True, "pending_update_version": "v2.0.1",
                                   "update_attempt": {}},
                              tracked=[], saves=0, _update_closing=False)
        app.schedule_config_save = lambda: setattr(app, "saves", app.saves + 1)
        app.track = lambda e, p=None: app.tracked.append((e, p))
        with tempfile.NamedTemporaryFile(delete=False) as f:
            path = f.name
        try:
            app._update_ready = ("v2.0.1", path)
            with mock.patch.object(main, "APP_VERSION", "2.0.0"), \
                    mock.patch.object(main.updater, "can_self_install", return_value=True), \
                    mock.patch.object(main.updater, "is_all_users_install", return_value=False), \
                    mock.patch.object(main.updater, "new_log_path", return_value="C:/t/u.log"), \
                    mock.patch.object(main.updater, "launch_installer", return_value=(True, path)) as launch:
                main.AppController._run_update_on_quit(app)
            launch.assert_called_once_with(path, all_users=False, log_path="C:/t/u.log", relaunch=False)
            self.assertEqual(app.cfg["pending_update_version"], "v2.0.1")
            self.assertEqual(app.cfg["update_attempt"], {"to": "v2.0.1", "log": "C:/t/u.log", "from": "2.0.0",
                                                         "quit": True})
            self.assertEqual(app.saves, 1)                         # quit_app flushes it right after
            app._update_ready = ("v2.0.1", path)
            app.cfg["install_updates_on_quit"] = False
            with mock.patch.object(main.updater, "launch_installer") as launch:
                main.AppController._run_update_on_quit(app)
            launch.assert_not_called()
        finally:
            os.unlink(path)


# ── meetings, calendar, hotkeys, signals ──────────────────────────────────────

class TestSessionAndBusy(unittest.TestCase):
    def test_the_meeting_session_decides_busy(self):
        for state, busy in (("recording", True), ("processing", True), ("idle", False), ("done", False)):
            app = SimpleNamespace(meeting_session=SimpleNamespace(is_busy=busy, state=state))
            self.assertIs(main.AppController._is_meeting_busy(app), busy)
        self.assertFalse(main.AppController._is_meeting_busy(SimpleNamespace()))

    def test_no_reminder_while_busy_or_while_one_shows(self):
        def app(**kw):
            base = dict(is_rec=False, _busy=False, _file_job_running=False,
                        meeting_session=SimpleNamespace(is_busy=False), _reminder=None)
            base.update(kw)
            return SimpleNamespace(**base)
        self.assertFalse(main.AppController._calendar_busy(app()))
        for kw in ({"is_rec": True}, {"_busy": True}, {"_file_job_running": True},
                   {"meeting_session": SimpleNamespace(is_busy=True)},
                   {"_reminder": SimpleNamespace(is_showing=lambda: True)}):
            self.assertTrue(main.AppController._calendar_busy(app(**kw)), kw)

    def test_suspend_hotkeys_unhooks_and_rehooks(self):
        app = SimpleNamespace(_hotkeys_suspended=False, calls=[])
        app._unregister_kbd_hotkey = lambda: app.calls.append("unhook")
        app._unregister_mouse_listener = lambda: app.calls.append("unmouse")
        app._setup_assist_hotkey = lambda hk: app.calls.append(("assist", hk))
        app._release_smart_hotkey = lambda: app.calls.append("unsmart")   # the Smart Actions shortcut too
        app.apply_tray_bindings = lambda: app.calls.append("rehook")
        cfg_before = dict(main.cfg)
        self.assertTrue(main.AppController.suspend_hotkeys(app, True))
        main.AppController.suspend_hotkeys(app, True)            # once
        self.assertEqual(app.calls, ["unhook", "unmouse", ("assist", ""), "unsmart"])
        main.AppController.suspend_hotkeys(app, False)
        self.assertEqual(app.calls[-1], "rehook")
        self.assertEqual(main.cfg, cfg_before)                    # no config change

    def test_a_late_hotkey_while_capturing_does_nothing(self):
        app = mock.MagicMock()
        app._hotkeys_suspended = True
        app.is_rec = False
        main.AppController._on_hotkey(app)
        app._start.assert_not_called()


class TestSignals(unittest.TestCase):
    def test_recording_changes_are_announced_once(self):
        c = main.AppController.__new__(main.AppController)
        sent, queued = [], []
        c.sig_recording_changed = SimpleNamespace(emit=sent.append)
        c._run_on_gui = lambda fn, *a: fn(*a)            # the GUI thread
        c.is_rec = True
        c.is_rec = True
        c.is_rec = False
        self.assertEqual(sent, [True, False])
        self.assertFalse(c.is_rec)
        # A worker's change is announced on the GUI thread as the state is
        # THEN: changed back meanwhile = nothing stale goes out.
        c._run_on_gui = lambda fn, *a: queued.append(fn)
        c.is_rec = True
        c._is_rec = False                                # the GUI set it back first
        for fn in queued:
            fn()
        self.assertEqual(sent, [True, False])

    def test_workers_hand_signals_to_the_gui_thread(self):
        posted, ran = [], []
        app = SimpleNamespace(overlay=SimpleNamespace(call_soon=lambda fn, *a: posted.append((fn, a))))
        bind(app, "_run_on_gui")
        app._run_on_gui(ran.append, 1)                           # GUI (main) thread: now
        t = threading.Thread(target=app._run_on_gui, args=(ran.append, 2))
        t.start()
        t.join()
        self.assertEqual(ran, [1])
        self.assertEqual(posted, [(ran.append, (2,))])

    def test_transcript_ready_notification(self):
        def app(notify=True, route="home", visible=True):
            a = SimpleNamespace(cfg={"notify_when_ready": notify}, hints=[], history=0, opened=[])
            a.notify_history_changed = lambda: setattr(a, "history", a.history + 1)
            a.show_tray_hint = lambda t, m, on_click=None: a.hints.append((t, m, on_click))
            a.show_main = lambda route=None, **o: a.opened.append((route, o))
            a.main_win = SimpleNamespace(isVisible=lambda: visible, isMinimized=lambda: False,
                                         current_route=lambda: route)
            return a
        item = {"name": "interview.m4a", "transcript_id": "20261007_112000"}
        a = app()
        main.AppController._on_file_job_finished(a, item)
        self.assertEqual(a.history, 1)
        title, text, on_click = a.hints[0]
        self.assertEqual((title, text), ("Transcript ready", "interview.m4a"))
        on_click()
        self.assertEqual(a.opened, [("audio_to_text", {"transcript_id": "20261007_112000"})])
        a = app(route="audio_to_text")                            # on screen: no notice
        main.AppController._on_file_job_finished(a, item)
        self.assertEqual((a.history, a.hints), (1, []))
        a = app(notify=False)
        main.AppController._on_file_job_finished(a, item)
        self.assertEqual(a.hints, [])

    def test_notes_ready_notification(self):
        def app(notify=True, route="home"):
            a = SimpleNamespace(cfg={"notify_when_ready": notify}, hints=[], history=0, opened=[],
                                meeting_session=SimpleNamespace(_meeting_title="Weekly sync"))
            a.notify_history_changed = lambda: setattr(a, "history", a.history + 1)
            a.show_tray_hint = lambda t, m, on_click=None: a.hints.append((t, m, on_click))
            a.show_main = lambda route=None, **o: a.opened.append((route, o))
            a.main_win = SimpleNamespace(isVisible=lambda: True, isMinimized=lambda: False,
                                         current_route=lambda: route)
            return a
        a = app()
        main.AppController._on_meeting_finished(a, "C:/m/20261008_101500", "## Notes", "")
        self.assertEqual(a.history, 1)
        title, text, on_click = a.hints[0]
        self.assertEqual((title, text), ("Notes ready", "Weekly sync"))
        on_click()
        self.assertEqual(a.opened, [("meetings", {"meeting": "C:/m/20261008_101500"})])
        for a, args in ((app(route="meetings"), ("C:/m/1", "## Notes", "")),   # on screen
                        (app(notify=False), ("C:/m/1", "## Notes", "")),
                        (app(route="meetings"), ("C:/m/1", "", "The AI engine didn't answer.")),
                        (app(), ("", "", "Nothing was recorded."))):       # no meeting to open
            main.AppController._on_meeting_finished(a, *args)
            self.assertEqual((a.history, a.hints), (1, []))

    def test_notes_that_failed_get_one_quiet_notice(self):
        for notify in (True, False):                 # whatever "Notify me when ready" says
            a = SimpleNamespace(cfg={"notify_when_ready": notify}, hints=[], history=0, opened=[],
                                meeting_session=SimpleNamespace(_meeting_title="Weekly sync"))
            a.notify_history_changed = lambda a=a: setattr(a, "history", a.history + 1)
            a.show_tray_hint = lambda t, m, on_click=None, a=a: a.hints.append((t, m, on_click))
            a.show_main = lambda route=None, a=a, **o: a.opened.append((route, o))
            a.main_win = SimpleNamespace(isVisible=lambda: True, isMinimized=lambda: False,
                                         current_route=lambda: "dictation")
            main.AppController._on_meeting_finished(a, "C:/m/20261008_101500", "", "quota exceeded")
            self.assertEqual(len(a.hints), 1)
            title, text, on_click = a.hints[0]
            self.assertEqual((title, text), ("Notes couldn't be written", "Weekly sync · Open"))
            on_click()
            self.assertEqual(a.opened, [("meetings", {"meeting": "C:/m/20261008_101500"})])

    def test_identify_speakers_finishing_is_not_notes_news(self):
        """"Identify speakers" ends through the same finished signal: the lists
        refresh (and queued pending notes go on) but nobody is told "Notes ready"
        - or "couldn't be written" - about a run that wrote no notes."""
        def app(speakers):
            a = SimpleNamespace(cfg={"notify_when_ready": True}, hints=[], history=0, opened=[],
                                meeting_session=SimpleNamespace(_meeting_title="Weekly sync",
                                                                last_run_speakers=speakers),
                                _pending_notes_queue=["C:/m/next"], nexts=0)
            a.notify_history_changed = lambda: setattr(a, "history", a.history + 1)
            a.show_tray_hint = lambda t, m, on_click=None: a.hints.append((t, m, on_click))
            a.show_main = lambda route=None, **o: a.opened.append((route, o))
            a._next_pending_notes = lambda: setattr(a, "nexts", a.nexts + 1)
            a.main_win = SimpleNamespace(isVisible=lambda: True, isMinimized=lambda: False,
                                         current_route=lambda: "dictation")
            return a
        for notes in ("## Notes", ""):
            a = app(True)
            with mock.patch.object(main.QTimer, "singleShot", lambda ms, fn: fn()):
                main.AppController._on_meeting_finished(a, "C:/m/20261008_101500", notes, "")
            self.assertEqual((a.history, a.hints, a.nexts), (1, [], 1))
        a = app(False)                                    # a real notes run still says so
        with mock.patch.object(main.QTimer, "singleShot", lambda ms, fn: fn()):
            main.AppController._on_meeting_finished(a, "C:/m/20261008_101500", "## Notes", "")
        self.assertEqual(a.hints[0][:2], ("Notes ready", "Weekly sync"))
        # (a stand-in session that has no such attribute behaves as before, too)
        a = app(False)
        a.meeting_session = SimpleNamespace(_meeting_title="Weekly sync")
        with mock.patch.object(main.QTimer, "singleShot", lambda ms, fn: fn()):
            main.AppController._on_meeting_finished(a, "C:/m/20261008_101500", "## Notes", "")
        self.assertEqual(a.hints[0][:2], ("Notes ready", "Weekly sync"))


# ── Wave 4 review: the Smart Actions shortcut, a safe Quit, updates ───────────

class TestSmartActionsShortcut(unittest.TestCase):
    def _app(self, pro=True, rec=False):
        app = SimpleNamespace(is_rec=rec, _hotkeys_suspended=False, _smart_next=False,
                              upsells=[], presses=[], gates=[], cfg={},
                              auth=SimpleNamespace(is_pro=pro, is_authenticated=True, user_id="u1"))
        app.is_pro = lambda: pro
        app._pro_upsell = lambda feature=None: app.upsells.append(feature)
        app.show_auth_gate = lambda: app.gates.append(1)
        app._on_hotkey = lambda: app.presses.append(app._smart_next)
        return app

    def test_a_pro_press_starts_one_smart_dictation(self):
        app = self._app()
        main.AppController._on_smart_hotkey(app)
        self.assertEqual(app.presses, [True])            # _start sees it ...
        self.assertFalse(app._smart_next)                # ... and only that start
        self.assertEqual(app.upsells, [])

    def test_free_without_tries_gets_the_pro_window(self):
        app = self._app(pro=False)
        with mock.patch.object(main.entitlements, "smart_actions_used", lambda auth=None: 5):
            main.AppController._on_smart_hotkey(app)
        self.assertEqual((app.presses, app.upsells, app.gates), ([], ["Smart Actions"], []))

    def test_free_with_tries_starts(self):
        app = self._app(pro=False)
        with mock.patch.object(main.entitlements, "smart_actions_used", lambda auth=None: 2):
            main.AppController._on_smart_hotkey(app)
        self.assertEqual((app.presses, app.upsells), ([True], []))

    def test_pressed_again_it_finishes_the_dictation(self):
        app = self._app(pro=False, rec=True)               # even if Pro ended meanwhile
        main.AppController._on_smart_hotkey(app)
        self.assertEqual((app.presses, app.upsells), ([False], []))

    def test_a_late_press_while_capturing_does_nothing(self):
        app = self._app()
        app._hotkeys_suspended = True
        main.AppController._on_smart_hotkey(app)
        self.assertEqual(app.presses, [])

    def _start(self, smart_next):
        app = SimpleNamespace(
            is_rec=False, _file_job_running=False, _smart_next=smart_next, _oneshot_action="stale",
            cfg={"backend": "local", "output_action": "transcribe_only", "whisper_model": "base",
                 "action_model": "rule_based"},
            overlay=mock.MagicMock(), recorder=SimpleNamespace(start_recording=lambda: None),
            loads=[])
        app._cloud_preflight_warn = lambda: None
        app._register_transient_keys = lambda: None
        app._unregister_transient_keys = lambda: None
        app._load_for_dictation = lambda: app.loads.append(1)
        with mock.patch.object(main.threading, "Thread", _Inline):
            main.AppController._start(app)
        self.assertTrue(app.is_rec)
        return app._oneshot_action

    def test_only_the_dictation_it_started_is_a_smart_action(self):
        self.assertEqual(self._start(True), main.actions.ACTION_SMART_AUTO)
        self.assertIsNone(self._start(False))            # Alt+R: always the exact words

    def test_setup_takes_only_free_key_combos(self):
        added, removed = [], []
        kbd = SimpleNamespace(add_hotkey=lambda hk, fn, **kw: added.append(hk),
                              remove_hotkey=lambda hk: removed.append(hk))
        app = SimpleNamespace(cfg={"hotkey": "alt+r", "live_assist_hotkey": "ctrl+alt+a"},
                              _registered_smart_hotkey=None, _smart_listener=None)
        bind(app, "_setup_smart_hotkey", "_release_smart_hotkey")
        with mock.patch.dict(sys.modules, {"keyboard": kbd}), \
                mock.patch.object(main.sys, "platform", "win32"):
            for refused in ("", "Alt+R", "ctrl+alt+a", "s", "mouse:middle"):
                self.assertFalse(app._setup_smart_hotkey(refused), refused)
            self.assertEqual(added, [])
            self.assertTrue(app._setup_smart_hotkey("Ctrl + Shift + S"))
            self.assertEqual((added, app._registered_smart_hotkey), (["ctrl+shift+s"], "ctrl+shift+s"))
            app._setup_smart_hotkey("")                       # cleared: the hook goes
        self.assertEqual(removed, ["ctrl+shift+s"])
        self.assertIsNone(app._registered_smart_hotkey)


class TestSafeQuit(unittest.TestCase):
    def _app(self, state="idle", file_job=False, recording=False, confirm=True):
        order = []
        rec = SimpleNamespace(recording=recording)
        session = SimpleNamespace(state=state, is_busy=state in ("recording", "processing"))

        def save_for_quit():
            # MeetingSession.save_for_quit: recording, transcript, notes_pending;
            # the recorder stops on its own thread; the session is free.
            order.append("save meeting")
            session.state, session.is_busy, rec.recording = "idle", False, False
            return "C:/m/1"
        session.save_for_quit = save_for_quit
        rec.request_abort = lambda: order.append("abort")
        rec.stop_recording = lambda: order.append("stop")
        rec.shutdown = lambda: order.append("portaudio")
        app = SimpleNamespace(meeting_session=session, _file_job_running=file_job, recorder=rec,
                              calendar=None, mic_monitor=None, _quitting=False, order=order,
                              file_jobs=SimpleNamespace(shutdown=lambda: order.append("file jobs")),
                              qapp=SimpleNamespace(quit=lambda: order.append("quit")))
        app._confirm_quit = lambda: order.append("asked") or confirm
        app._run_update_on_quit = lambda: order.append("installer")
        app._flush_pending_save = lambda: order.append("settings")
        for name in ("_unregister_transient_keys", "_unregister_kbd_hotkey", "_unregister_mouse_listener",
                     "_release_smart_hotkey"):
            setattr(app, name, lambda: None)
        bind(app, "quit_app", "_stop_recording_for_quit", "_stop_mic_monitor_for_quit",
             "_on_quit_event", "_on_session_ending", "_on_about_to_quit")
        return app

    def test_what_quitting_would_end(self):
        q = main.AppController._quit_question
        self.assertEqual(q(self._app()), "")
        recording = q(self._app("recording"))
        self.assertIn("still recording", recording)
        self.assertIn("saved", recording)
        self.assertIn("notes are written the next time you open Gusan", recording)
        self.assertIn("notes are still being written", q(self._app("processing")))
        self.assertIn("next time you open Gusan", q(self._app("processing")))
        # A rewrite of notes the meeting has: save_for_quit keeps those.
        rewrite = self._app("processing")
        rewrite.meeting_session._rewrite_only = True
        rewrite.meeting_session.notes = "## Notes"
        self.assertIn("earlier notes are kept", q(rewrite))
        rewrite.meeting_session.notes = ""                  # none yet: written next time
        self.assertIn("next time you open Gusan", q(rewrite))
        self.assertIn("converted", q(self._app(file_job=True)))

    def test_cancel_keeps_everything_running(self):
        app = self._app("recording", recording=True, confirm=False)
        self.assertFalse(app.quit_app())
        self.assertEqual(app.order, ["asked"])
        self.assertFalse(app._quitting)

    def test_a_meeting_is_saved_before_an_installer_starts_or_portaudio_goes(self):
        app = self._app("recording", recording=True)
        self.assertTrue(app.quit_app())
        self.assertEqual(app.order, ["asked", "file jobs", "save meeting", "installer", "settings",
                                     "portaudio", "quit"])
        app.order.clear()
        self.assertTrue(app.quit_app())                     # a second Quit: nothing again
        app._on_about_to_quit()                             # Qt's aboutToQuit after it
        self.assertEqual(app.order, ["settings"])

    def test_notes_being_written_are_marked_too(self):
        app = self._app("processing")
        app.quit_app()
        self.assertEqual(app.order[:3], ["asked", "file jobs", "save meeting"])

    def test_a_dictation_stops_on_its_own_thread_before_portaudio_goes(self):
        app = self._app(recording=True)
        app.quit_app()
        self.assertEqual(app.order, ["asked", "file jobs", "abort", "stop", "installer", "settings",
                                     "portaudio", "quit"])

    def test_nothing_recording_goes_straight_to_portaudio(self):
        app = self._app()
        app.quit_app()
        self.assertEqual(app.order, ["asked", "file jobs", "installer", "settings", "portaudio", "quit"])

    def test_the_mic_check_closes_before_portaudio_goes(self):
        app = self._app()
        app.mic_monitor = SimpleNamespace(stop_now=lambda: app.order.append("mic check"),
                                          stop=lambda: app.order.append("mic check later"))
        app.quit_app()
        self.assertEqual(app.order, ["asked", "file jobs", "mic check", "installer", "settings",
                                     "portaudio", "quit"])

    def test_a_log_off_saves_the_meeting_without_asking(self):
        app = self._app("recording", recording=True)
        app._confirm_quit = lambda: self.fail("no question at a log-off")
        app._on_session_ending(object())                    # commitDataRequest(manager)
        self.assertEqual(app.order, ["save meeting", "settings"])
        self.assertTrue(app._session_ending)
        self.assertTrue(app._on_quit_event())               # Qt's own quit then goes ahead
        app.order.clear()
        app._on_about_to_quit()                             # WM_ENDSESSION: nothing left but settings
        self.assertEqual(app.order, ["settings"])

    def test_a_log_off_that_is_cancelled_leaves_the_speaker_names_working(self):
        # Windows asks every app before a log-off / shutdown (commitDataRequest), and
        # the user can still cancel it. The background speaker pass stops - but the next
        # meeting's names must still come, without restarting the app.
        import diarization_proc
        import meeting_session
        app = self._app()
        session = meeting_session.MeetingEngine(None)
        spawned = []
        session._spawn = lambda target, *args: spawned.append(target)   # (no worker thread here)
        app.meeting_session = session
        self.assertTrue(session._followup.enqueue("C:/m/1"))           # queued after a meeting's notes
        with mock.patch.object(diarization_proc, "_cancel_all", threading.Event()) as refuse_all:
            app._on_session_ending(object())
            self.assertEqual(session.speakers_state(), "")              # the queue was dropped...
            self.assertTrue(session._followup.enqueue("C:/m/2"))       # ...yet a later meeting's names still start
            self.assertEqual(session.speakers_state("C:/m/2"), "waiting")
            self.assertFalse(refuse_all.is_set())                       # and so does their child process
        self.assertEqual(app.order, ["settings"])

    def test_about_to_quit_alone_saves_a_meeting(self):
        app = self._app("recording", recording=True)
        app._on_about_to_quit()
        self.assertEqual(app.order, ["save meeting", "settings"])

    def test_cmd_q_goes_through_quit_app(self):
        app = self._app()
        later = []
        with mock.patch.object(main, "QTimer", SimpleNamespace(singleShot=lambda ms, fn: later.append(fn))):
            self.assertFalse(app._on_quit_event())          # Qt must not quit by itself...
        self.assertEqual(later, [app.quit_app])             # ...quit_app runs next
        later[0]()
        self.assertTrue(app._quitting)
        self.assertTrue(app._on_quit_event())               # quit_app's own qapp.quit() passes

    def test_a_second_quit_while_asking_does_nothing(self):
        app = self._app("recording", recording=True)
        nested = []
        app._confirm_quit = lambda: nested.append(app.quit_app()) or False
        self.assertFalse(app.quit_app())
        self.assertEqual(nested, [False])
        self.assertFalse(app._quit_asking)


class TestGusanApplication(unittest.TestCase):
    """The QApplication subclass main() builds: QEvent.Quit asks quit_filter."""

    class _Ev:
        def __init__(self, kind):
            self.kind, self.ignored = kind, False

        def type(self):
            return self.kind

        def ignore(self):
            self.ignored = True

    def test_quit_filter_decides(self):
        with mock.patch.object(main, "QEvent", SimpleNamespace(Quit="quit")):
            app = main.GusanApplication([])
            self.assertIsNone(app.quit_filter)
            app.quit_filter = lambda: False
            ev = self._Ev("quit")
            self.assertIs(app.event(ev), True)                # swallowed: quit_app runs instead
            self.assertTrue(ev.ignored)
            app.quit_filter = lambda: True
            ev = self._Ev("quit")
            app.event(ev)
            self.assertFalse(ev.ignored)                      # Qt quits
            app.quit_filter = lambda: False
            ev = self._Ev("other")
            app.event(ev)
            self.assertFalse(ev.ignored)                      # any other event: Qt's

    def test_main_builds_it_and_the_controller_hooks_in(self):
        import inspect
        self.assertIn("GusanApplication(sys.argv)", inspect.getsource(main.main))
        init = inspect.getsource(main.AppController.__init__)
        self.assertIn("quit_filter = self._on_quit_event", init)
        self.assertIn("commitDataRequest.connect(self._on_session_ending", init)
        self.assertIn("aboutToQuit.connect(self._on_about_to_quit", init)
        self.assertIn("sig_quit.connect(self.quit_app", init)


class TestPendingNotes(unittest.TestCase):
    def _app(self, busy=False, folders=("C:/m/2", "C:/m/1")):
        session = SimpleNamespace(is_busy=busy, started=[])
        session.write_pending_notes = lambda f: session.started.append(f) or f != "C:/m/bad"
        app = SimpleNamespace(meeting_session=session, is_rec=False, _busy=False, _file_job_running=False,
                              _quitting=False, _pending_notes_queue=[])
        bind(app, "_write_pending_notes", "_start_pending_notes", "_next_pending_notes", "_run_on_gui")
        self.folders = list(folders)
        return app

    def _run(self, app):
        later = []
        with mock.patch.object(main, "QTimer", SimpleNamespace(singleShot=lambda ms, fn: later.append(ms))), \
                mock.patch.object(main.threading, "Thread", _Inline), \
                mock.patch("meeting_session.pending_notes_folders", lambda: list(self.folders)):
            app._write_pending_notes()
        return later

    def test_written_one_at_a_time_when_nothing_records(self):
        app = self._app()
        self.assertEqual(self._run(app), [])
        self.assertEqual(app.meeting_session.started, ["C:/m/2"])
        self.assertEqual(app._pending_notes_queue, ["C:/m/1"])
        app._next_pending_notes()                           # after "finished"
        self.assertEqual(app.meeting_session.started, ["C:/m/2", "C:/m/1"])

    def test_waits_while_busy(self):
        for kw in ({"busy": True}, {}):
            app = self._app(**kw)
            if not kw:
                app.is_rec = True
            self.assertEqual(self._run(app), [main.PENDING_NOTES_RETRY_MS])
            self.assertEqual(app.meeting_session.started, [])
        # It keeps waiting through a long meeting (hours), then stops asking.
        self.assertGreaterEqual(main.PENDING_NOTES_MAX_TRIES * main.PENDING_NOTES_RETRY_MS,
                                3 * 3600 * 1000)
        app = self._app(busy=True)
        later = []
        with mock.patch.object(main, "QTimer", SimpleNamespace(singleShot=lambda ms, fn: later.append(ms))):
            app._write_pending_notes(main.PENDING_NOTES_MAX_TRIES)
        self.assertEqual(later, [])

    def test_a_folder_it_cannot_write_is_skipped(self):
        app = self._app(folders=("C:/m/bad", "C:/m/1"))
        self._run(app)
        self.assertEqual(app.meeting_session.started, ["C:/m/bad", "C:/m/1"])

    def test_the_queue_continues_after_a_meeting_finishes(self):
        app = SimpleNamespace(cfg={}, _pending_notes_queue=["C:/m/1"], _quitting=False, hints=[],
                              meeting_session=SimpleNamespace(_meeting_title="x"), main_win=None)
        app.notify_history_changed = lambda: None
        app.show_tray_hint = lambda *a, **k: app.hints.append(a)
        app._next_pending_notes = lambda: None
        later = []
        with mock.patch.object(main, "QTimer", SimpleNamespace(singleShot=lambda ms, fn: later.append(fn))):
            main.AppController._on_meeting_finished(app, "C:/m/2", "## Notes", "")
        self.assertEqual(later, [app._next_pending_notes])
        self.assertEqual(app.hints, [("Notes ready", "x")])

    def test_dictation_runs_while_notes_are_written_from_a_transcript(self):
        holds = main.AppController._meeting_holds_recorder
        self.assertFalse(holds(SimpleNamespace(meeting_session=SimpleNamespace(is_busy=True,
                                                                               holds_recorder=False))))
        self.assertTrue(holds(SimpleNamespace(meeting_session=SimpleNamespace(is_busy=True,
                                                                              holds_recorder=True))))
        self.assertTrue(holds(SimpleNamespace(meeting_session=SimpleNamespace(is_busy=True))))  # older double
        self.assertFalse(holds(SimpleNamespace()))


class TestFirstRunWizardForward(unittest.TestCase):
    def test_a_second_launch_brings_the_wizard_forward(self):
        for front, opened in ((True, []), (False, [()])):
            c = SimpleNamespace(opened=[])
            c.front_first_run_wizard = lambda f=front: f
            c.show_main = lambda *a, c=c: c.opened.append(a)
            main.dispatch_ipc_action(c, "show_settings")
            self.assertEqual(c.opened, opened)

    def test_only_a_wizard_on_screen_before_onboarding_is_done(self):
        class Wiz:
            def __init__(self, visible):
                self.visible = visible

            def isVisible(self):
                return self.visible
        fronted = []
        for wiz, done, want in ((Wiz(True), False, True), (Wiz(False), False, False),
                                (Wiz(True), True, False), (None, False, False)):
            c = SimpleNamespace(onboarding_win=wiz, cfg={"onboarding_done": done})
            c._bring_to_front = fronted.append
            self.assertIs(main.AppController.front_first_run_wizard(c), want)
        self.assertEqual(len(fronted), 1)


class TestRenameNote(unittest.TestCase):
    def test_due_until_shown_or_closed(self):
        c = SimpleNamespace(cfg={"rename_note_pending": True}, saves=0)
        c.schedule_config_save = lambda: setattr(c, "saves", c.saves + 1)
        self.assertTrue(main.AppController.rename_note_due(c))
        main.AppController.dismiss_rename_note(c)
        main.AppController.dismiss_rename_note(c)
        self.assertFalse(main.AppController.rename_note_due(c))
        self.assertEqual(c.saves, 1)
        self.assertFalse(main.DEFAULT["rename_note_pending"])     # fresh installs: never


class TestKeyringWritesOnlyChanges(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        p = mock.patch.object(main, "CONFIG_PATH", os.path.join(self._tmp.name, "config.json"))
        p.start()
        self.addCleanup(p.stop)

    def test_a_debounced_save_writes_no_unchanged_secret(self):
        writes = []
        cfg = {**main.DEFAULT, "google_api_key": "G", "action_api_key": "A", "mistral_api_key": "",
               "notion_api_key": "N", "notion_refresh_token": "R", "user_secrets": {}}
        with mock.patch.object(main.storage, "write_secret",
                               side_effect=lambda n, v: writes.append((n, v)) or True):
            main.save_config(cfg)
            self.assertEqual(len(writes), 6)                # first save: every secret once (the Notion pair too)
            writes.clear()
            main.save_config(cfg)
            main.save_config(dict(cfg, hotkey="ctrl+r"))
            self.assertEqual(writes, [])                    # nothing changed in the keyring
            main.save_config(dict(cfg, action_api_key="A2"))
            self.assertEqual(writes, [(main.storage.ACTION_API_KEY_SECRET, "A2")])
            writes.clear()
            main.save_config(dict(cfg, action_api_key="A2", notion_api_key="N2"))     # a refreshed Notion token
            self.assertEqual(writes, [(main.storage.NOTION_TOKEN_SECRET, "N2")])
        with open(main.CONFIG_PATH, encoding="utf-8") as f:
            disk = json.load(f)
        self.assertEqual((disk["google_api_key"], disk["action_api_key"]), ("", ""))
        self.assertEqual((disk["notion_api_key"], disk["notion_refresh_token"]), ("", ""))

    def test_a_keyring_that_refuses_keeps_trying_and_the_key_stays_on_disk(self):
        writes = []
        cfg = {**main.DEFAULT, "google_api_key": "G"}
        with mock.patch.object(main.storage, "write_secret",
                               side_effect=lambda n, v: writes.append(n) or False):
            main.save_config(cfg)
            main.save_config(cfg)
        self.assertEqual(writes.count(main.storage.GOOGLE_API_KEY_SECRET), 2)
        with open(main.CONFIG_PATH, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["google_api_key"], "G")

    def test_a_key_read_at_startup_is_not_written_back(self):
        with open(main.CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump({"config_schema_version": 2}, f)
        writes = []
        with mock.patch.object(main.storage, "write_secret",
                               side_effect=lambda n, v: writes.append(n) or True), \
                mock.patch.object(main.storage, "read_secret",
                                  side_effect=lambda n: "K-" + n if n != main.storage.USER_SECRETS_SECRET
                                  else ""):
            cfg = main.load_config()
            writes.clear()
            main.save_config(cfg)
        self.assertNotIn(main.storage.GOOGLE_API_KEY_SECRET, writes)
        self.assertNotIn(main.storage.ACTION_API_KEY_SECRET, writes)
        self.assertNotIn(main.storage.MISTRAL_API_KEY_SECRET, writes)
        self.assertNotIn(main.storage.NOTION_TOKEN_SECRET, writes)
        self.assertNotIn(main.storage.NOTION_REFRESH_SECRET, writes)
        self.assertEqual(cfg["notion_api_key"], "K-" + main.storage.NOTION_TOKEN_SECRET)


class TestUpdatesWaitForRecordings(unittest.TestCase):
    def _app(self, **kw):
        app = SimpleNamespace(cfg={"pending_update_version": "v2.0.1"}, hints=[], tracked=[],
                              is_rec=False, _busy=False, _file_job_running=False, _update_notice_shown=None,
                              meeting_session=SimpleNamespace(is_busy=False), **_update_state())
        for k, v in kw.items():
            setattr(app, k, v)
        app.show_tray_hint = lambda t, m, on_click=None: app.hints.append((t, m))
        app.track = lambda e, p=None: app.tracked.append((e, p))
        app.save_config = lambda: None
        bind(app, "install_update", "_prompt_update", "_prepare_update_on_quit", "_on_update_fetched",
             "_start_update_fetch", "_update_blocker", "_run_on_gui")
        app._is_newer = main.AppController._is_newer
        return app

    def test_install_waits_for_a_recording_a_dictation_or_a_file_job(self):
        for kw, words in (({"is_rec": True}, "recording"),
                          ({"meeting_session": SimpleNamespace(is_busy=True)}, "recording"),
                          ({"_busy": True}, "dictation"),
                          ({"_file_job_running": True}, "converting")):
            app, got = self._app(**kw), []
            with mock.patch.object(main, "APP_VERSION", "2.0.0"), \
                    mock.patch.object(main.sys, "platform", "win32"), \
                    mock.patch.object(main.updater, "can_self_install", return_value=True), \
                    mock.patch.object(main.threading, "Thread") as thread:
                self.assertFalse(app.install_update("v2.0.1", on_done=lambda ok, info: got.append((ok, info))))
            thread.assert_not_called()                     # nothing downloads, nothing quits
            self.assertEqual(len(got), 1)
            self.assertFalse(got[0][0])
            self.assertIn(words, got[0][1])
            self.assertFalse(app._update_installing)
            self.assertIsNone(app._update_window)          # not even a window
        app = self._app(is_rec=True)
        with mock.patch.object(main, "APP_VERSION", "2.0.0"), \
                mock.patch.object(main.sys, "platform", "win32"), \
                mock.patch.object(main.updater, "can_self_install", return_value=True):
            app.install_update("v2.0.1", manual=False)        # the notification's click
        self.assertEqual(app.hints[0][0], "Update not installed")

    def test_no_notice_during_a_meeting(self):
        app = self._app(meeting_session=SimpleNamespace(is_busy=True))
        with mock.patch.object(main, "APP_VERSION", "2.0.0"):
            app._prompt_update("v2.0.1")
        self.assertEqual(app.hints, [])
        self.assertIsNone(app._update_notice_shown)          # still shown after a later dictation

    def test_a_skipped_version_is_never_installed_on_quit(self):
        app = self._app()
        app.cfg.update(install_updates_on_quit=True, dismissed_update_version="v2.0.1")
        with mock.patch.object(main, "APP_VERSION", "2.0.0"), \
                mock.patch.object(main.sys, "platform", "win32"), \
                mock.patch.object(main.updater, "can_self_install", return_value=True), \
                mock.patch.object(main.threading, "Thread", _Inline), \
                mock.patch.object(main.updater, "cached_download", return_value=(True, "C:/t/setup.exe")) as dl:
            app._prepare_update_on_quit("v2.0.1")
            dl.assert_not_called()
            app.cfg["dismissed_update_version"] = ""
            app._prepare_update_on_quit("v2.0.1")
        dl.assert_called_once_with("v2.0.1", on_progress=mock.ANY, should_cancel=mock.ANY)   # kept per release
        self.assertEqual(app._update_ready, ("v2.0.1", "C:/t/setup.exe"))
        with tempfile.NamedTemporaryFile(delete=False) as f:
            path = f.name
        try:
            app._update_ready = ("v2.0.1", path)
            app.cfg["dismissed_update_version"] = "v2.0.1"       # skipped after it was fetched
            app.schedule_config_save = lambda: None
            with mock.patch.object(main, "APP_VERSION", "2.0.0"), \
                    mock.patch.object(main.updater, "can_self_install", return_value=True), \
                    mock.patch.object(main.updater, "is_all_users_install", return_value=False), \
                    mock.patch.object(main.updater, "launch_installer") as launch:
                main.AppController._run_update_on_quit(app)
            launch.assert_not_called()
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()
