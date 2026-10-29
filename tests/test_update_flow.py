"""The in-app update, end to end, without Qt or a network: main.AppController.install_update drives the
"Updating Gusan" window (ui/update_window.py) from the first click to the restart.

What is checked here (the pieces are in tests/test_updater.py, the window itself in
tests_qt/test_update_window.py):
 * progress -> checking -> installing -> the installer is started (quietly, for exactly this copy) ->
   Gusan quits through quit_app after 1.5 s; per-user Setup reopens Gusan itself, a watcher reopens it
   in every other case; an all-users install asks Windows for permission first and a No keeps Gusan open;
 * it never interrupts work: a recording, dictation, meeting or file job is waited for, a typed API key is
   asked about once, no dictation / meeting / file job starts once the installer runs;
 * Cancel at once, install again while a cancelled download winds down (queued, never two downloads), a
   cancel that lands as the download finishes wins, a timer of a cancelled attempt never acts on the next;
 * the quiet fetch ahead for "Install updates when I quit" and Install now share ONE download; Quit installs
   for a per-user installed copy only, never asks for an administrator, never reopens Gusan;
 * a Mac, the portable zip and python main.py open the download page and download nothing;
 * the next start: it worked (said once, %TEMP% tidied) or it stopped (said once, with Try again).

Pure logic like tests/test_services_wiring.py: SimpleNamespace controllers with the real methods bound, under
test_core's stand-ins for Qt (a fake window module and a fake question card take the place of the widgets)."""
import os
import shutil
import sys
import tempfile
import types
import unittest
from types import SimpleNamespace
from unittest import mock
from unittest.mock import MagicMock

try:
    from tests import test_core  # noqa: F401  (importing installs the stubs)
except ImportError:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import test_core  # noqa: F401

import file_jobs
import main
import updater

TAG = "v2.0.1"
OLD = "2.0.0"                      # the running version in these tests
LOG = os.path.join(tempfile.gettempdir(), "gusan-update", "logs", "gusan-update-0a1b2c3d.log")
EXE = r"C:\Users\a\AppData\Local\Programs\Gusan\TranscribeApp.exe"
MB = 2 ** 20

METHODS = (
    "install_update", "_update_blocker", "_update_stale", "_update_window_for", "_update_done", "_update_failed",
    "_cancel_update", "_update_install_now", "_update_retry", "_update_open_page", "_on_update_progress",
    "_update_poll", "_update_unsaved_edits", "_ask_install_with_unsaved_key", "_update_handover",
    "_update_start_installer", "_on_update_launched", "_prepare_update_on_quit", "_start_update_fetch",
    "_on_update_fetched", "_run_update_on_quit", "_clear_update_attempt", "_after_restart_checks",
    "_show_update_failed_after_restart", "_tidy_update_files", "_prompt_update", "_nudge_update_after_dictation",
    "note_update_check", "_run_on_gui", "_start", "_quit_question",
)


def bind(app, *names):
    for name in names:
        setattr(app, name, getattr(main.AppController, name).__get__(app))
    return app


class _Signal:
    """A Qt signal as far as the controller uses one."""

    def __init__(self):
        self.slots = []

    def connect(self, fn):
        self.slots.append(fn)

    def emit(self, *args):
        for fn in list(self.slots):
            fn(*args)


class _Window:
    """ui.update_window.UpdateWindow as the controller sees it: it records every call."""

    def __init__(self, tag="", main_app=None, parent=None):
        self.tag, self.app, self.calls, self.stage, self.visible = tag, main_app, [], "downloading", False
        self.cancel_requested, self.install_requested = _Signal(), _Signal()
        self.retry_requested, self.website_requested = _Signal(), _Signal()

    def isVisible(self):
        return self.visible

    def set_tag(self, tag):
        self.tag = tag

    def front(self):
        self.calls.append(("front", (), {}))
        self.visible = True

    def hide(self):
        self.calls.append(("hide", (), {}))
        self.visible = False

    def _stage(self, name, stage, *args, **kw):
        self.calls.append((name, args, kw))
        self.stage, self.visible = stage, True

    def show_downloading(self):
        self._stage("show_downloading", "downloading")

    def show_progress(self, done, total):
        self.calls.append(("show_progress", (done, total), {}))

    def show_waiting(self, text=None, can_install=False):
        self._stage("show_waiting", "waiting", text=text, can_install=can_install)

    def show_installing(self, reopens=True, admin=False):
        self._stage("show_installing", "installing", reopens=reopens, admin=admin)

    def show_failed(self, message, detail=""):
        self._stage("show_failed", "failed", message, detail=detail)

    def names(self):
        return [c[0] for c in self.calls if c[0] != "front"]

    def last(self, name):
        found = [c for c in self.calls if c[0] == name]
        return found[-1] if found else None


class Harness(unittest.TestCase):
    """The controller double and everything around it."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.installer = os.path.join(self.tmp, "TranscribeApp-Windows-Setup.exe")
        with open(self.installer, "wb") as f:
            f.write(b"MZ installer")
        self.timers, self.pending, self.mode = [], [], "inline"
        self.fetches, self.fetch_result, self.on_fetch = [], (True, self.installer), None
        self.all_users, self.can_self, self.watcher = False, True, object()
        self.setup_alive = False             # is a Gusan Setup process running (updater.setup_running)
        self.launch = MagicMock(return_value=(True, self.installer))
        self.spawn = MagicMock(side_effect=lambda *a, **k: self.watcher)
        self.browser = MagicMock()
        self.prune = MagicMock()
        self.logged = []
        self.question = MagicMock(return_value="wait")
        prompt = types.ModuleType("ui.components.mini_prompt")
        prompt.choose = self.question
        package = types.ModuleType("ui.components")
        package.__path__ = []
        package.mini_prompt = prompt
        window_module = types.ModuleType("ui.update_window")
        window_module.UpdateWindow = _Window
        self.save_config = MagicMock()

        def worker(target=None, args=(), kwargs=None, daemon=None, name=None):
            run = (lambda: target(*args, **(kwargs or {})))
            return SimpleNamespace(start=(run if self.mode == "inline" else lambda: self.pending.append(run)),
                                   is_alive=lambda: False)

        def fetch(tag, **kw):
            self.fetches.append((tag, kw))
            if self.on_fetch:
                self.on_fetch(kw)
            if self.honour_cancel and kw.get("should_cancel") and kw["should_cancel"]():
                return False, updater.CANCELLED
            return self.fetch_result

        patches = (
            mock.patch.object(main, "APP_VERSION", OLD),
            mock.patch.object(main.sys, "platform", "win32"),
            mock.patch.object(main.sys, "executable", EXE),
            mock.patch.object(main.threading, "Thread", worker),
            mock.patch.object(main, "QTimer", SimpleNamespace(singleShot=lambda ms, fn: self.timers.append((ms, fn)))),
            mock.patch.object(main.webbrowser, "open", self.browser),
            mock.patch.object(main, "save_config", self.save_config),
            mock.patch.object(updater, "cached_download", fetch),
            mock.patch.object(updater, "launch_installer", self.launch),
            mock.patch.object(updater, "spawn_relauncher", self.spawn),
            mock.patch.object(updater, "can_self_install", lambda *a: self.can_self),
            mock.patch.object(updater, "is_all_users_install", lambda *a, **k: self.all_users),
            mock.patch.object(updater, "new_log_path", lambda *a: LOG),
            mock.patch.object(updater, "setup_running", lambda: self.setup_alive),
            mock.patch.object(updater, "prune_cache", self.prune),
            mock.patch.object(updater, "prune_logs", self.prune),
            mock.patch.dict(sys.modules, {"ui.update_window": window_module, "ui.components": package,
                                          "ui.components.mini_prompt": prompt}),
        )
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.honour_cancel = True
        self.app = self.make_app()

    def reset(self):
        """A fresh scenario inside one test (the patches stay)."""
        self.timers.clear()
        self.pending.clear()
        self.fetches.clear()
        self.mode, self.on_fetch, self.fetch_result = "inline", None, (True, self.installer)
        for mock_ in (self.launch, self.spawn, self.browser, self.prune, self.question, self.save_config):
            mock_.reset_mock(side_effect=False)
        self.launch.return_value = (True, self.installer)
        self.honour_cancel = True
        self.app = self.make_app()
        return self.app

    def make_app(self, **over):
        app = SimpleNamespace(
            cfg={"pending_update_version": TAG, "install_updates_on_quit": True, "update_attempt": {}},
            is_rec=False, _busy=False, _file_job_running=False, meeting_session=SimpleNamespace(is_busy=False),
            main_win=None, _quitting=False, hints=[], tracked=[], flushed=0, saves=0, overlay=MagicMock(),
            _update_notice_shown=None, _update_ready=None, _update_fetching=None, _update_fetch_user=False,
            _update_installing=False, _update_closing=False, _update_window=None, _update_cancel=False,
            _update_gen=0, _update_queued=None, _update_tag="", _update_manual=True, _update_on_done=None,
            _update_key_asked=False, _update_force=False, _update_asking=False, _update_failed_notice=False,
            _update_progress_at=0.0, sig_update_available=SimpleNamespace(emitted=[]),
            sig_setting_changed=SimpleNamespace(emit=lambda *a: None))
        app.sig_update_available.emit = lambda *a: app.sig_update_available.emitted.append(a)
        app.show_tray_hint = lambda t, m, on_click=None: app.hints.append((t, m, on_click))
        app.track = lambda e, p=None: app.tracked.append((e, p))
        app.flush_config_save = lambda: setattr(app, "flushed", app.flushed + 1)
        app.schedule_config_save = lambda: setattr(app, "saves", app.saves + 1)
        app.quit_app = MagicMock()
        app._bring_to_front = lambda w: w.front()
        app._is_newer = main.AppController._is_newer
        app._is_meeting_busy = lambda: bool(app.meeting_session.is_busy)
        bind(app, *METHODS)
        for k, v in over.items():
            setattr(app, k, v)
        return app

    # ── helpers ──
    @property
    def window(self):
        return self.app._update_window

    def run_next_timer(self):
        ms, fn = self.timers.pop(0)
        fn()
        return ms

    def run_worker(self):
        self.pending.pop(0)()

    def done_calls(self):
        calls = []
        return calls, (lambda ok, info: calls.append((ok, info)))

    def tracked(self, event):
        return [p for e, p in self.app.tracked if e == event]


# ═════════════════════════ the whole way through ═════════════════════════
class TestInstalling(Harness):
    def test_progress_then_installing_then_quit_after_a_moment(self):
        calls, done = self.done_calls()
        self.on_fetch = lambda kw: kw["on_progress"](50 * MB, 100 * MB)
        self.assertTrue(self.app.install_update(TAG, manual=True, on_done=done))
        win = self.window
        self.assertEqual(win.names(), ["show_downloading", "show_progress", "show_installing"])
        self.assertEqual(win.last("show_progress")[1], (50 * MB, 100 * MB))
        self.assertEqual(win.last("show_installing")[2], {"reopens": True, "admin": False})
        self.assertIn(("front", (), {}), win.calls)
        # The quiet install, for this user, with its own log; Setup reopens Gusan when it succeeds.
        self.launch.assert_called_once_with(self.installer, all_users=False, log_path=LOG, relaunch=True)
        self.spawn.assert_called_once_with(EXE, LOG)
        self.assertEqual(self.app.cfg["update_attempt"], {"to": TAG, "log": LOG, "from": OLD})
        self.assertEqual(self.app.flushed, 1)
        # Nothing is closed yet: a moment to read "Installing", then the one proper quit.
        self.app.quit_app.assert_not_called()
        self.assertEqual([ms for ms, _fn in self.timers], [1500])
        self.assertIs(self.timers[0][1], self.app.quit_app)
        self.assertEqual(calls, [])                                   # it installs: nobody is told "it did not"
        # pending_update_version stays until the new version actually runs.
        self.assertEqual(self.app.cfg["pending_update_version"], TAG)
        self.assertEqual(self.tracked("update_install_result"), [{"manual": True, "ok": True}])
        self.assertEqual(self.tracked("update_install_started"), [])      # (Settings tracks its own manual start)

    def test_a_notice_click_is_tracked_as_started(self):
        self.app.install_update(TAG, manual=False)
        self.assertEqual(self.tracked("update_install_started"), [{"manual": False, "to": TAG}])

    def test_per_user_setup_reopens_gusan_all_users_does_not_but_the_watcher_does(self):
        self.all_users = True
        self.app.install_update(TAG)
        self.launch.assert_called_once_with(self.installer, all_users=True, log_path=LOG, relaunch=False)
        self.spawn.assert_called_once_with(EXE, LOG)
        self.assertEqual(self.window.last("show_installing")[2], {"reopens": True, "admin": True})
        self.assertEqual([ms for ms, _fn in self.timers], [1500])

    def test_when_nothing_can_reopen_it_the_window_says_so_and_waits_longer(self):
        self.all_users, self.watcher = True, None                   # PowerShell blocked by policy
        self.app.install_update(TAG)
        self.assertEqual(self.window.last("show_installing")[2], {"reopens": False, "admin": True})
        self.assertEqual([ms for ms, _fn in self.timers], [5000])

    def test_a_per_user_install_still_reopens_when_the_watcher_is_blocked(self):
        self.watcher = None                                          # Setup's own relaunch remains
        self.app.install_update(TAG)
        self.assertEqual(self.window.last("show_installing")[2], {"reopens": True, "admin": False})
        self.assertEqual([ms for ms, _fn in self.timers], [1500])

    def test_the_installer_is_started_on_a_worker_never_on_the_gui_thread(self):
        self.mode = "deferred"
        self.app.install_update(TAG)
        self.run_worker()                                            # the download
        self.assertEqual(self.window.stage, "installing")
        self.assertTrue(self.app._update_closing)                    # already: no dictation during a permission prompt
        self.launch.assert_not_called()
        self.assertEqual(len(self.pending), 1)                       # the launch waits for its worker
        self.run_worker()
        self.launch.assert_called_once()

    def test_never_a_second_installer_or_download(self):
        self.mode = "deferred"
        self.assertTrue(self.app.install_update(TAG))
        self.assertFalse(self.app.install_update(TAG))               # while downloading
        self.run_worker()
        self.assertFalse(self.app.install_update(TAG))               # while the installer starts
        self.run_worker()
        self.assertFalse(self.app.install_update(TAG))               # once it runs
        self.assertEqual(len(self.fetches), 1)
        self.launch.assert_called_once()
        self.assertEqual(self.window.calls.count(("front", (), {})) >= 3, True)       # each click brings it forward

    def test_saying_no_to_the_permission_prompt_keeps_the_app_open(self):
        calls, done = self.done_calls()
        self.all_users = True
        self.launch.return_value = (False, updater.MSG_NO_PERMISSION)
        self.app.install_update(TAG, manual=False, on_done=done)
        self.assertEqual(self.window.last("show_failed")[1], (updater.MSG_NO_PERMISSION,))
        self.assertEqual(calls, [(False, updater.MSG_NO_PERMISSION)])
        self.spawn.assert_not_called()                               # nothing to reopen later
        self.assertEqual(self.timers, [])                            # no quit
        self.assertNotIn("update_attempt", [k for k, v in self.app.cfg.items() if v])
        self.assertFalse(self.app._update_closing)                   # dictation, meetings and files work again
        self.assertFalse(self.app._update_installing)
        self.assertEqual(self.tracked("update_install_result"), [{"manual": False, "ok": False}])
        # ...and a later try is a clean new attempt that finds the downloaded file again.
        self.launch.return_value = (True, self.installer)
        self.assertTrue(self.app.install_update(TAG))
        self.assertEqual(len(self.fetches), 1)
        self.assertEqual(self.launch.call_count, 2)

    def test_windows_or_an_antivirus_refusing_the_installer_is_a_failure_with_a_way_forward(self):
        self.launch.return_value = (False, updater.MSG_BLOCKED)
        calls, done = self.done_calls()
        self.app.install_update(TAG, on_done=done)
        self.assertEqual(self.window.last("show_failed")[1], (updater.MSG_BLOCKED,))
        self.assertEqual(calls, [(False, updater.MSG_BLOCKED)])
        self.spawn.assert_not_called()

    def test_an_error_while_starting_the_installer_is_a_failure_too(self):
        self.launch.side_effect = RuntimeError("boom")
        self.app.install_update(TAG)
        self.assertEqual(self.window.last("show_failed")[1], (updater.MSG_NO_START,))
        self.assertFalse(self.app._update_closing)


class TestFailures(Harness):
    def test_a_failed_download_shows_a_way_forward_and_the_app_stays(self):
        calls, done = self.done_calls()
        self.fetch_result = (False, updater.MSG_NETWORK)
        self.app.install_update(TAG, on_done=done)
        self.assertEqual(self.window.last("show_failed")[1], (updater.MSG_NETWORK,))
        self.assertEqual(calls, [(False, updater.MSG_NETWORK)])
        self.launch.assert_not_called()
        self.spawn.assert_not_called()
        self.assertEqual(self.timers, [])
        self.assertFalse(self.app._update_installing)
        self.assertEqual(self.tracked("update_install_result"), [{"manual": True, "ok": False}])
        self.assertEqual(self.app.cfg["pending_update_version"], TAG)       # still "Update available"

    def test_try_again_is_the_same_install_and_the_download_page_button_opens_the_page(self):
        self.fetch_result = (False, updater.MSG_NETWORK)
        calls, done = self.done_calls()
        self.app.install_update(TAG, on_done=done)
        self.assertEqual(len(self.fetches), 1)
        self.fetch_result = (True, self.installer)
        self.window.retry_requested.emit()
        self.assertEqual(len(self.fetches), 2)
        self.launch.assert_called_once()
        self.window.website_requested.emit()
        self.browser.assert_called_once_with(updater.release_page(TAG))

    def test_the_window_is_wired_once_to_the_controller(self):
        self.app.install_update(TAG)
        win = self.window
        for sig in ("cancel_requested", "install_requested", "retry_requested", "website_requested"):
            self.assertEqual(len(getattr(win, sig).slots), 1, sig)
        self.fetch_result = (False, updater.MSG_NETWORK)
        self.app._update_installing = False
        self.app._update_closing = False
        self.app.install_update(TAG)
        self.assertIs(self.window, win)                              # reused, not built again

    def test_an_unexpected_error_in_the_worker_is_a_failure_too(self):
        def boom(kw):
            raise ValueError("bug")
        self.on_fetch = boom
        self.app.install_update(TAG)
        self.assertEqual(self.window.last("show_failed")[1], (updater.MSG_NETWORK,))
        self.launch.assert_not_called()

    def test_a_downloaded_file_that_vanished_is_a_failure_not_a_crash(self):
        # (an antivirus removing it between the download and the hand-over)
        self.fetch_result = (True, os.path.join(self.tmp, "gone.exe"))
        self.app.install_update(TAG)
        self.assertEqual(self.window.last("show_failed")[1], (updater.MSG_FILE_MISSING,))
        self.assertIsNone(self.app._update_ready)
        self.launch.assert_not_called()
        # a kept file that is gone by the time of a new click is simply downloaded again
        app = self.make_app(_update_ready=(TAG, os.path.join(self.tmp, "gone.exe")))
        self.fetch_result = (True, self.installer)
        app.install_update(TAG)
        self.assertEqual(self.launch.call_count, 1)

    def test_a_tag_that_is_not_a_newer_release_is_refused(self):
        for bad in ("v2.0.0", "v1.9.9", "", None, "v2.0.1/../../x", "garbage"):
            calls, done = self.done_calls()
            self.assertFalse(self.app.install_update(bad, on_done=done), bad)
            self.assertEqual(calls, [(False, updater.MSG_NOT_A_RELEASE)])
        self.assertEqual(self.fetches, [])
        self.assertIsNone(self.window)


class TestNeverInterrupting(Harness):
    def test_never_in_the_middle_of_work(self):
        cases = ((dict(is_rec=True), "recording"), (dict(_busy=True), "dictation"),
                 (dict(_file_job_running=True), "converting"),
                 (dict(meeting_session=SimpleNamespace(is_busy=True)), "recording"))
        for over, words in cases:
            app = self.make_app(**over)
            calls, done = self.done_calls()
            self.assertFalse(app.install_update(TAG, on_done=done))
            self.assertEqual(len(calls), 1, over)
            self.assertFalse(calls[0][0])
            self.assertIn(words, calls[0][1])
            self.assertIsNone(app._update_window)                    # no window, no download, nothing counted
            self.assertFalse(app._update_installing)
            self.assertEqual([e for e, _p in app.tracked], [])
            app = self.make_app(**over)
            app.install_update(TAG, manual=False)                    # the notice's click: a notification says it
            self.assertEqual(app.hints[0][:2], ("Update not installed", calls[0][1]))
        self.assertEqual(self.fetches, [])

    def test_a_recording_started_during_the_download_is_waited_for(self):
        def alt_r(kw):
            self.app.is_rec = True
        self.on_fetch = alt_r
        self.app.install_update(TAG)
        self.assertEqual(self.window.last("show_waiting")[2], {"text": None, "can_install": False})
        self.launch.assert_not_called()
        self.assertEqual([ms for ms, _fn in self.timers], [2000])
        self.run_next_timer()                                        # still recording
        self.assertEqual(self.window.names().count("show_waiting"), 2)
        self.launch.assert_not_called()
        self.app.is_rec = False
        self.run_next_timer()
        self.launch.assert_called_once()
        self.assertEqual(self.window.stage, "installing")
        self.app.quit_app.assert_not_called()
        self.assertEqual([ms for ms, _fn in self.timers], [1500])

    def test_a_meeting_or_file_job_started_during_the_download_is_waited_for_too(self):
        for attr, value in (("meeting_session", SimpleNamespace(is_busy=True)), ("_file_job_running", True),
                            ("_busy", True)):
            self.reset()
            self.on_fetch = lambda kw, a=attr, v=value: setattr(self.app, a, v)
            self.app.install_update(TAG)
            self.launch.assert_not_called()
            self.assertEqual(self.window.stage, "waiting", attr)

    def test_install_again_while_waiting_shows_the_same_window_and_downloads_nothing(self):
        self.on_fetch = lambda kw: setattr(self.app, "is_rec", True)
        self.app.install_update(TAG)
        win = self.window
        calls, done = self.done_calls()
        self.app.is_rec = False                                       # (so only "already running" can refuse)
        self.assertFalse(self.app.install_update(TAG, on_done=done))
        self.assertIs(self.window, win)
        self.assertEqual(win.calls[-1], ("front", (), {}))
        self.assertEqual(len(self.fetches), 1)
        self.assertIs(self.app._update_on_done, done)                 # the newest page's row hears the result
        self.launch.assert_not_called()
        self.app._cancel_update()
        self.assertEqual(calls, [(False, "")])

    def test_cancel_while_waiting_never_installs(self):
        calls, done = self.done_calls()
        self.on_fetch = lambda kw: setattr(self.app, "is_rec", True)
        self.app.install_update(TAG, on_done=done)
        self.app._cancel_update()
        self.assertEqual(self.window.calls[-1][0], "hide")
        self.assertEqual(calls, [(False, "")])                        # a plain Cancel: nothing red on the page
        self.app.is_rec = False
        self.run_next_timer()
        self.assertEqual(self.timers, [])                             # nothing re-armed
        self.launch.assert_not_called()
        self.assertFalse(self.app._update_installing)
        self.assertIsNotNone(self.app._update_ready)                  # the file is kept for Quit

    def test_a_timer_from_a_cancelled_attempt_never_acts_on_the_next(self):
        self.on_fetch = lambda kw: setattr(self.app, "is_rec", True)
        self.app.install_update(TAG)                                  # attempt A: downloaded, waiting for the recording
        stale = self.timers.pop(0)[1]                                 # A's 2-second timer
        self.app._cancel_update()
        self.app.is_rec, self.on_fetch = False, None
        self.app._update_ready = None                                 # (so that B has to download)
        self.mode = "deferred"
        self.assertTrue(self.app.install_update(TAG))                 # attempt B: downloading
        self.app._update_ready = (TAG, self.installer)                # ...and the file turns up meanwhile
        stale()                                                       # A's timer fires
        self.assertEqual(self.launch.call_count, 0)
        self.assertEqual(self.window.stage, "downloading")            # B is untouched
        self.assertEqual(self.timers, [])                             # and nothing re-armed
        self.assertEqual(self.pending[1:], [])
        self.run_worker()                                             # B goes on by itself
        self.run_worker()
        self.launch.assert_called_once()

    def test_a_timer_cancelled_with_its_attempt_never_installs_after_the_recording_ends(self):
        self.on_fetch = lambda kw: setattr(self.app, "is_rec", True)
        self.app.install_update(TAG)
        stale = self.timers.pop(0)[1]
        self.app._cancel_update()
        self.app.is_rec = False
        stale()
        self.launch.assert_not_called()
        self.assertEqual(self.timers, [])

    def test_a_dictation_that_starts_while_the_installer_runs_is_refused_with_a_hint(self):
        self.app._update_closing = True
        self.app._start()
        self.assertFalse(self.app.is_rec)
        self.assertEqual(self.app.hints, [("Gusan is updating", "It closes and opens again in a moment.", None)])
        self.app.overlay.show_overlay.assert_not_called()
        self.app._update_closing = False

    def test_no_file_job_once_the_installer_runs(self):
        self.assertEqual(file_jobs.refusal_message(self.app), "")
        self.app._update_closing = True
        self.assertEqual(file_jobs.refusal_message(self.app), file_jobs.MSG_UPDATING)

    def test_nothing_is_asked_at_quit_once_the_installer_runs(self):
        self.app.meeting_session = SimpleNamespace(is_busy=True, state="recording")
        self.assertIn("still recording", self.app._quit_question())
        self.app._update_closing = True
        self.assertEqual(self.app._quit_question(), "")

    def test_the_gusan_name_never_goes_old(self):
        self.app.install_update(TAG)
        texts = [updater.UPDATING_TITLE, updater.UPDATING_TEXT, updater.MSG_NO_PERMISSION, updater.MSG_BLOCKED,
                 updater.MSG_NO_START, updater.MSG_FILE_MISSING, file_jobs.MSG_UPDATING]
        for text in texts:
            self.assertNotIn("Transcribe", text)
            self.assertNotIn("limit", text.lower())


class TestTypedKey(Harness):
    def setUp(self):
        super().setUp()
        self.unsaved = True
        self.app.main_win = SimpleNamespace(pages=lambda: {"home": SimpleNamespace(),
                                                           "settings": SimpleNamespace(unsaved_edits=lambda: self.unsaved)})

    def test_asked_once_not_yet_waits_and_install_now_goes_on(self):
        self.app.install_update(TAG)
        self.question.assert_called_once()
        args, kw = self.question.call_args
        self.assertIs(args[0], self.window)                           # over the update window, never under it
        self.assertEqual(args[1], "Install the update now?")
        self.assertIn("isn't saved yet", args[2])
        self.assertEqual([b[0] for b in args[3]], ["install", "wait"])
        self.assertEqual(kw["default"], "wait")                       # Enter never loses a key
        self.assertEqual(self.window.last("show_waiting")[2], {"text": None, "can_install": True})
        self.launch.assert_not_called()
        self.assertFalse(self.app._update_asking)
        self.run_next_timer()                                         # polled again: not asked again
        self.question.assert_called_once()
        self.launch.assert_not_called()
        self.window.install_requested.emit()                          # the window's Install now
        self.launch.assert_called_once()
        self.assertEqual(self.window.stage, "installing")

    def test_saving_the_key_lets_it_go_on_by_itself(self):
        self.app.install_update(TAG)
        self.unsaved = False
        self.run_next_timer()
        self.launch.assert_called_once()
        self.question.assert_called_once()

    def test_install_now_in_the_question_installs(self):
        self.question.return_value = "install"
        self.app.install_update(TAG)
        self.launch.assert_called_once()

    def test_escape_in_the_question_is_not_yet(self):
        self.question.return_value = None
        self.app.install_update(TAG)
        self.launch.assert_not_called()
        self.assertEqual(self.window.stage, "waiting")

    def test_no_question_when_nothing_is_typed(self):
        self.unsaved = False
        self.app.install_update(TAG)
        self.question.assert_not_called()
        self.launch.assert_called_once()

    def test_a_page_that_cannot_say_never_stops_an_update(self):
        self.app.main_win = SimpleNamespace(pages=lambda: {"settings": SimpleNamespace(unsaved_edits=lambda: 1 / 0)})
        self.app.install_update(TAG)
        self.launch.assert_called_once()
        self.reset().main_win = None                                  # no window built at all
        self.app.install_update(TAG)
        self.launch.assert_called_once()

    def test_cancel_while_the_question_is_open_installs_nothing(self):
        def cancel_meanwhile(*a, **k):
            self.app._cancel_update()
            return "install"
        self.question.side_effect = cancel_meanwhile
        self.app.install_update(TAG)
        self.launch.assert_not_called()
        self.assertEqual(self.timers, [])
        self.assertFalse(self.window.isVisible())

    def test_a_dictation_started_while_the_question_is_open_is_waited_for(self):
        def alt_r_meanwhile(*a, **k):
            self.app.is_rec = True
            return "install"
        self.question.side_effect = alt_r_meanwhile
        self.app.install_update(TAG)
        self.launch.assert_not_called()
        self.assertEqual(self.window.last("show_waiting")[2], {"text": None, "can_install": False})
        self.assertFalse(self.app._update_closing)                    # it can still stop
        self.app.is_rec = False
        self.run_next_timer()
        self.launch.assert_called_once()
        self.question.assert_called_once()                            # asked once, "Install now" stands

    def test_no_second_update_and_no_notice_while_the_question_is_open(self):
        seen = []

        def meanwhile(*a, **k):
            seen.append((self.app.install_update(TAG), self.app._update_asking))
            self.app._prompt_update("v9.9.9")
            self.app.note_update_check(updater.AVAILABLE, "v9.9.9", manual=False)
            return "wait"
        self.question.side_effect = meanwhile
        self.app.install_update(TAG)
        self.assertEqual(seen, [(False, True)])
        self.assertEqual(self.app.hints, [])
        self.assertEqual(self.app.sig_update_available.emitted, [])
        self.assertEqual(len(self.fetches), 1)
        self.question.assert_called_once()


class TestCancel(Harness):
    def test_cancel_closes_the_window_at_once_even_mid_download(self):
        calls, done = self.done_calls()
        self.mode = "deferred"
        self.app.install_update(TAG, on_done=done)
        self.window.cancel_requested.emit()                           # Cancel, Esc or the X
        self.assertEqual(self.window.calls[-1][0], "hide")
        self.assertEqual(calls, [(False, "")])
        self.assertTrue(self.fetches == [] and len(self.pending) == 1)
        self.assertTrue(self.app._update_installing)                  # until the worker has noticed
        self.window.cancel_requested.emit()                           # pressed twice: once
        self.assertEqual(calls, [(False, "")])

    def test_the_worker_stops_at_the_next_chunk_for_a_cancel_it_started(self):
        self.mode = "deferred"
        self.app.install_update(TAG)
        self.app._cancel_update()
        self.run_worker()                                             # asks should_cancel: True
        self.assertEqual(self.fetches[0][1]["should_cancel"](), True)
        self.assertFalse(self.app._update_installing)
        self.assertEqual(self.launch.call_count, 0)
        self.assertIsNone(self.app._update_ready)

    def test_install_again_while_a_cancelled_download_winds_down_is_queued_never_two_downloads(self):
        self.mode = "deferred"
        self.app.install_update(TAG)
        self.app._cancel_update()
        calls, done = self.done_calls()
        self.assertFalse(self.app.install_update(TAG, on_done=done))
        self.assertEqual(self.window.last("show_downloading") is not None, True)
        self.assertEqual(self.window.stage, "downloading")
        self.assertEqual(len(self.pending), 1)                        # no second download yet
        self.run_worker()                                             # the stalled one gives up
        self.assertEqual([ms for ms, _fn in self.timers], [0])        # the queued one starts next
        self.assertEqual(len(self.pending), 0)
        self.run_next_timer()
        self.assertEqual(len(self.pending), 1)                        # started now
        self.assertEqual(self.launch.call_count, 0)
        self.run_worker()                                             # the download
        self.run_worker()                                             # the installer starts
        self.launch.assert_called_once()
        self.assertEqual(calls, [])                                   # it installed: nobody is told it did not

    def test_cancel_also_drops_a_queued_retry(self):
        self.mode = "deferred"
        self.app.install_update(TAG)
        self.app._cancel_update()
        calls, done = self.done_calls()
        self.app.install_update(TAG, on_done=done)                    # queued: the window comes back
        self.assertTrue(self.window.isVisible())
        self.app._cancel_update()                                     # ...and is cancelled again
        self.assertIsNone(self.app._update_queued)
        self.assertFalse(self.window.isVisible())
        self.assertEqual(calls, [(False, "")])                        # the page that asked is told
        self.run_worker()
        self.assertEqual(self.timers, [])
        self.assertEqual(self.pending, [])
        self.assertFalse(self.app._update_installing)

    def test_a_cancel_that_lands_as_the_download_finishes_wins(self):
        for finished in (True, False):
            app = self.reset()
            self.honour_cancel = False                                # the worker is past its last look
            calls, done = self.done_calls()
            self.fetch_result = (True, self.installer) if finished else (False, updater.MSG_NETWORK)
            self.on_fetch = lambda kw: self.app._cancel_update()      # Cancel pressed just now
            app.install_update(TAG, on_done=done)
            self.assertEqual(app._update_window.calls[-1][0], "hide")
            self.launch.assert_not_called()
            self.assertNotIn("show_failed", app._update_window.names())
            self.assertEqual(calls, [(False, "")])                    # a cancel, not an error
            self.assertEqual(app.cfg["pending_update_version"], TAG)
            self.assertFalse(app._update_installing)
            self.assertEqual(self.timers, [])
            # a finished download is still kept for "Install updates when I quit"
            self.assertEqual(app._update_ready, (TAG, self.installer) if finished else None)

    def test_cancel_stops_a_download_it_started_but_not_the_quiet_one_ahead_for_quit(self):
        self.mode = "deferred"
        self.app._prepare_update_on_quit(TAG)                         # the quiet fetch ahead
        self.assertEqual(self.app._update_fetching, TAG)
        self.assertEqual(len(self.pending), 1)
        self.app.install_update(TAG)                                  # the window joins it: no second download
        self.assertEqual(len(self.pending), 1)
        self.app._cancel_update()
        quiet = self.fetches                                          # (nothing ran yet)
        self.assertFalse(self.app._update_installing)                 # nothing of this attempt is running
        self.run_worker()
        self.assertEqual(self.fetches[0][1]["should_cancel"](), False)       # it carried on
        self.assertEqual(self.app._update_ready, (TAG, self.installer))      # so Quit still gets its file
        self.assertEqual(self.launch.call_count, 0)
        self.assertEqual(self.app._update_fetching, None)
        del quiet

    def test_both_stop_when_gusan_quits(self):
        self.mode = "deferred"
        self.app._prepare_update_on_quit(TAG)
        self.app._quitting = True
        self.run_worker()
        self.assertEqual(self.fetches[0][1]["should_cancel"](), True)
        self.assertIsNone(self.app._update_ready)


class TestOneDownload(Harness):
    def test_install_now_joins_the_fetch_ahead_and_follows_its_progress(self):
        self.mode = "deferred"
        self.app._prepare_update_on_quit(TAG)
        calls, done = self.done_calls()
        self.assertTrue(self.app.install_update(TAG, on_done=done))
        self.assertEqual(len(self.pending), 1)                        # one download, shared
        self.assertEqual(self.window.names(), ["show_downloading"])
        self.on_fetch = lambda kw: kw["on_progress"](30 * MB, 100 * MB)
        self.run_worker()
        self.assertEqual(self.window.last("show_progress")[1], (30 * MB, 100 * MB))
        self.assertEqual(self.window.stage, "installing")             # the same fetch fed the waiting window
        self.assertEqual(len(self.fetches), 1)
        self.assertEqual(self.pending[0:1] != [], True)               # the launch worker
        self.run_worker()
        self.launch.assert_called_once()

    def test_a_download_kept_for_quit_makes_install_now_instant(self):
        self.app._update_ready = (TAG, self.installer)
        self.app.install_update(TAG)
        self.assertEqual(self.fetches, [])
        self.launch.assert_called_once()

    def test_install_now_works_with_the_quit_setting_off(self):
        self.app.cfg["install_updates_on_quit"] = False
        self.app._prepare_update_on_quit(TAG)
        self.assertEqual(self.fetches, [])
        self.app.install_update(TAG)
        self.assertEqual(len(self.fetches), 1)
        self.launch.assert_called_once()

    def test_the_fetch_uses_the_shared_cache_with_progress_and_cancel(self):
        self.app.install_update(TAG)
        tag, kw = self.fetches[0]
        self.assertEqual(tag, TAG)
        self.assertEqual(set(kw), {"on_progress", "should_cancel"})

    def test_progress_reaches_the_window_at_most_ten_times_a_second_and_the_last_always(self):
        clock = [100.0]
        self.mode = "deferred"
        self.app.install_update(TAG)

        def flood(kw):
            for i in range(1, 201):                                    # ~100 chunks a second on a fast line
                clock[0] += 0.001
                kw["on_progress"](i * MB // 2, 100 * MB)
            kw["on_progress"](100 * MB, 100 * MB)
        self.on_fetch = flood
        with mock.patch.object(main.time, "monotonic", lambda: clock[0]):
            self.run_worker()
        shown = [c[1] for c in self.window.calls if c[0] == "show_progress"]
        self.assertLessEqual(len(shown), 4)
        self.assertEqual(shown[-1], (100 * MB, 100 * MB))
        self.assertEqual(shown, sorted(shown))

    def test_a_late_progress_after_cancel_is_ignored(self):
        self.mode = "deferred"
        self.app.install_update(TAG)
        self.app._cancel_update()
        before = list(self.window.calls)
        self.app._on_update_progress(10 * MB, 100 * MB)
        self.assertEqual(self.window.calls, before)

    def test_another_release_downloading_makes_a_new_request_wait_for_it(self):
        self.mode = "deferred"
        self.app.cfg["pending_update_version"] = "v2.0.2"
        with mock.patch.object(updater, "should_prompt", return_value=True):
            self.app._prepare_update_on_quit("v2.0.1")                # an older release is being fetched ahead
        self.assertTrue(self.app.install_update("v2.0.2"))
        self.assertEqual(len(self.pending), 1)                        # not two at once
        self.run_worker()                                             # v2.0.1 arrives
        self.assertEqual([ms for ms, _fn in self.timers], [0])
        self.run_next_timer()                                         # v2.0.2 starts
        self.assertEqual(len(self.pending), 1)
        self.run_worker()
        self.assertEqual([t for t, _kw in self.fetches], ["v2.0.1", "v2.0.2"])
        self.run_worker()                                             # the installer starts
        self.launch.assert_called_once()
        self.assertEqual(self.launch.call_args[0][0], self.installer)


class TestNotInstallable(Harness):
    def _assert_the_page_opens(self, app, tag=TAG, manual=True):
        calls, done = self.done_calls()
        self.assertFalse(app.install_update(tag, manual=manual, on_done=done))
        self.browser.assert_called_once_with(updater.release_page(tag))
        self.assertEqual(calls, [(False, "")])                        # the row resets with no red text
        self.assertEqual(self.fetches, [])
        self.launch.assert_not_called()
        self.assertEqual(app.hints[0][:2], ("Gusan " + tag, "Opening the download page in your browser."))
        self.assertIsNone(app._update_window)
        self.assertFalse(app._update_installing)
        self.assertEqual(app.tracked[-1], ("update_install_result", {"manual": manual, "ok": False}))

    def test_a_mac_opens_the_download_page(self):
        with mock.patch.object(main.sys, "platform", "darwin"):
            self._assert_the_page_opens(self.app)

    def test_the_portable_zip_and_a_source_checkout_open_the_download_page(self):
        self.can_self = False
        self._assert_the_page_opens(self.app)

    def test_a_notice_click_on_such_a_copy_is_tracked_as_started(self):
        self.can_self = False
        self._assert_the_page_opens(self.app, manual=False)
        self.assertEqual(self.app.tracked[0], ("update_install_started", {"manual": False, "to": TAG}))

    def test_such_a_copy_never_fetches_ahead_for_quit(self):
        self.can_self = False
        self.app._prepare_update_on_quit(TAG)
        self.assertEqual(self.fetches, [])
        with mock.patch.object(main.sys, "platform", "darwin"):
            self.can_self = True
            self.app._prepare_update_on_quit(TAG)
        self.assertEqual(self.fetches, [])


class TestEntryPoints(Harness):
    def test_the_notice_click_settings_and_try_again_all_call_install_update(self):
        calls = []
        self.app.install_update = lambda tag, manual=True, on_done=None: calls.append((tag, manual))
        self.app._prompt_update(TAG)
        title, text, on_click = self.app.hints[0]
        self.assertEqual(title, "Update available")
        on_click()
        self.assertEqual(calls, [(TAG, False)])
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(here, "ui", "pages", "settings.py"), encoding="utf-8") as f:
            settings = f.read()
        self.assertIn('call(self.app, "install_update", tag, on_done=_failed)', settings)
        with open(os.path.join(here, "main.py"), encoding="utf-8") as f:
            source = f.read()
        self.assertIn("self.install_update(self._update_tag, manual=True, on_done=self._update_on_done)", source)

    def test_no_wizard_path_is_left_in_the_controller(self):
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(here, "main.py"), encoding="utf-8") as f:
            source = f.read()
        for gone in ("updater.install(", "updater.launch(", "_on_update_installed", "QMessageBox", "start_update(",
                     "_quit_for_update", "qapp.exit("):
            self.assertNotIn(gone, source, gone)
        # the final close is the one proper quit
        self.assertIn("QTimer.singleShot(delay, self.quit_app)", source)

    def test_the_settings_row_hears_a_plain_cancel_as_no_error(self):
        # on_done(False, "") is what the page's _install_failed turns into "no red text"
        calls, done = self.done_calls()
        self.mode = "deferred"
        self.app.install_update(TAG, on_done=done)
        self.app._cancel_update()
        self.assertEqual(calls, [(False, "")])


# ═════════════════════════ Install updates when I quit ═════════════════════════
class TestInstallOnQuit(Harness):
    def _ready(self):
        self.app._update_ready = (TAG, self.installer)

    def test_quit_starts_a_quiet_per_user_install_with_no_reopen_and_no_watcher(self):
        self._ready()
        self.app._run_update_on_quit()
        self.launch.assert_called_once_with(self.installer, all_users=False, log_path=LOG, relaunch=False)
        self.spawn.assert_not_called()                                # the person chose to quit: nothing reopens it
        self.assertEqual(self.app.cfg["update_attempt"], {"to": TAG, "log": LOG, "from": OLD, "quit": True})
        self.assertEqual(self.app.saves, 1)                           # quit_app flushes it right after
        self.assertEqual(self.app.cfg["pending_update_version"], TAG)  # until the new version runs
        self.assertEqual(self.tracked("update_install_started"), [{"manual": False, "to": TAG}])
        self.assertEqual(self.tracked("update_install_result"), [{"manual": False, "ok": True}])
        self.assertIsNone(self.app._update_ready)

    def test_an_all_users_copy_never_asks_for_administrator_rights_at_quit(self):
        self._ready()
        self.all_users = True
        self.app._run_update_on_quit()
        self.launch.assert_not_called()
        self.assertEqual(self.app.tracked, [])

    def test_a_portable_or_source_copy_never_installs_at_quit(self):
        self._ready()
        self.can_self = False
        self.app._run_update_on_quit()
        self.launch.assert_not_called()

    def test_the_setting_off_a_skipped_version_a_missing_file_and_a_started_install_never_run(self):
        self._ready()
        self.app.cfg["install_updates_on_quit"] = False
        self.app._run_update_on_quit()
        self._ready()
        self.app.cfg.update(install_updates_on_quit=True, dismissed_update_version=TAG)
        self.app._run_update_on_quit()
        self.app.cfg["dismissed_update_version"] = ""
        self.app._update_ready = (TAG, os.path.join(self.tmp, "gone.exe"))
        self.app._run_update_on_quit()
        self._ready()
        self.app._update_closing = True                                # the update window already handed over
        self.app._run_update_on_quit()
        self.launch.assert_not_called()

    def test_a_start_that_fails_only_leaves_the_pending_update(self):
        self._ready()
        self.launch.return_value = (False, updater.MSG_BLOCKED)
        self.app._run_update_on_quit()
        self.assertNotIn("quit", self.app.cfg["update_attempt"])
        self.assertEqual(self.app.cfg["update_attempt"], {})
        self.assertEqual(self.app.cfg["pending_update_version"], TAG)
        self.assertEqual(self.tracked("update_install_result"), [{"manual": False, "ok": False}])

    def test_quit_app_runs_it_after_the_meeting_is_saved_and_never_for_the_in_app_install(self):
        # quit_app calls _run_update_on_quit; once the in-app install has handed over it is a no-op
        self.app._update_ready = (TAG, self.installer)
        self.app._update_closing = True
        self.app._run_update_on_quit()
        self.launch.assert_not_called()

    def test_the_fetch_ahead_keeps_its_gates(self):
        self.app._prepare_update_on_quit(TAG)
        self.assertEqual(len(self.fetches), 1)
        self.assertEqual(self.app._update_ready, (TAG, self.installer))
        self.app._prepare_update_on_quit(TAG)                         # already kept
        self.assertEqual(len(self.fetches), 1)
        app = self.make_app()
        app.cfg["install_updates_on_quit"] = False
        app._prepare_update_on_quit(TAG)
        app = self.make_app()
        app.cfg["dismissed_update_version"] = TAG
        app._prepare_update_on_quit(TAG)
        app = self.make_app(_update_installing=True)                  # the update window fetches it
        app._prepare_update_on_quit(TAG)
        app = self.make_app()
        app._prepare_update_on_quit("v2.0.0")                         # not newer
        self.assertEqual(len(self.fetches), 1)
        with mock.patch.object(main.sys, "platform", "linux"):
            self.make_app()._prepare_update_on_quit(TAG)
        self.assertEqual(len(self.fetches), 1)

    def test_the_fetch_ahead_on_an_all_users_copy_still_happens_so_install_now_is_instant(self):
        self.all_users = True
        self.app._prepare_update_on_quit(TAG)
        self.assertEqual(len(self.fetches), 1)
        self.app._run_update_on_quit()
        self.launch.assert_not_called()                               # ...but Quit never runs it

    def test_a_failed_fetch_ahead_is_only_logged(self):
        self.fetch_result = (False, updater.MSG_NETWORK)
        self.app._prepare_update_on_quit(TAG)
        self.assertIsNone(self.app._update_ready)
        self.assertIsNone(self.app._update_window)
        self.assertEqual(self.app.hints, [])
        self.assertIsNone(self.app._update_fetching)


# ═════════════════════════ the next start ═════════════════════════
class TestAfterRestart(Harness):
    def _start(self, attempt, version=OLD, log_text=None, **over):
        app = self.make_app(**over)
        app.cfg["update_attempt"] = attempt
        if log_text is not None:
            os.makedirs(os.path.dirname(attempt["log"]), exist_ok=True)
            with open(attempt["log"], "w", encoding="utf-8") as f:
                f.write(log_text)
        self.app = app
        with mock.patch.object(main, "APP_VERSION", version):
            app._after_restart_checks()
        return app

    def _attempt(self, **kw):
        return {"to": "v2.0.1", "log": os.path.join(self.tmp, "logs", "gusan-update-aa.log"), "from": OLD, **kw}

    def run_all_timers(self, version=OLD):
        with mock.patch.object(main, "APP_VERSION", version):
            while self.timers:
                self.run_next_timer()

    def test_an_ordinary_start_does_nothing(self):
        app = self._start({})
        self.assertEqual(self.timers, [])
        self.assertEqual(app.hints, [])
        self.save_config.assert_not_called()
        self.assertFalse(app._update_failed_notice)

    def test_it_worked_says_so_once_and_tidies_up(self):
        app = self._start(self._attempt(), version="2.0.1")
        self.assertEqual(app.cfg["update_attempt"], {})
        self.save_config.assert_called_once()
        self.assertEqual(sorted(ms for ms, _fn in self.timers), [2500, 30000])      # the hint now, the tidying later
        self.run_all_timers("2.0.1")
        self.assertEqual(app.hints, [("Gusan is up to date", "Updated to version 2.0.1.", None)])
        self.assertEqual(self.prune.call_count, 2)                    # the installers and the logs
        self.assertFalse(app._update_failed_notice)
        self.assertIsNone(app._update_window)

    def test_a_newer_version_than_the_attempt_counts_as_updated_too(self):
        app = self._start(self._attempt(), version="2.0.5")
        self.assertEqual(app.cfg["update_attempt"], {})

    def test_a_stopped_install_is_never_silent(self):
        app = self._start(self._attempt(), log_text="Setup started\nLog closed.\n")
        self.assertTrue(app._update_failed_notice)                    # held back until it is said
        self.assertEqual([ms for ms, _fn in self.timers], [3000])
        self.assertEqual(app.cfg["update_attempt"] == {}, False)       # kept until it is said
        self.run_all_timers()
        win = app._update_window
        message, detail = win.last("show_failed")[1][0], win.last("show_failed")[2]["detail"]
        self.assertEqual(message, "The installer stopped before it finished.")
        self.assertEqual(detail, "Details: " + self._attempt()["log"])
        self.assertEqual(win.tag, "v2.0.1")                           # Try again = that version
        self.assertEqual(app._update_tag, "v2.0.1")
        self.assertEqual(win.calls[-1], ("front", (), {}))
        self.assertEqual(app.cfg["update_attempt"], {})               # said once
        self.assertFalse(app._update_failed_notice)

    def test_no_log_at_all_means_it_never_started_which_is_a_stopped_install(self):
        app = self._start(self._attempt())                            # no log file
        self.run_all_timers()
        win = app._update_window
        self.assertEqual(win.last("show_failed")[2]["detail"], "")    # nothing to point at

    def test_a_setup_that_still_runs_says_nothing_and_keeps_the_record(self):
        self.setup_alive = True
        app = self._start(self._attempt(), log_text="Setup started\nExtracting files\n")
        self.assertEqual(self.timers, [])
        self.assertFalse(app._update_failed_notice)
        self.assertEqual(app.cfg["update_attempt"]["to"], "v2.0.1")
        self.save_config.assert_not_called()
        self.assertEqual(app.hints, [])

    def test_a_killed_setup_is_a_stopped_install_though_its_log_never_closed(self):
        # End task, a power cut: Setup never writes its last line - the process tells it is gone.
        self.setup_alive = False
        app = self._start(self._attempt(), log_text="Setup started\nExtracting files\n")
        self.assertTrue(app._update_failed_notice)
        self.run_all_timers()
        self.assertEqual(app._update_window.last("show_failed")[1][0], "The installer stopped before it finished.")
        self.assertEqual(app.cfg["update_attempt"], {})

    def test_when_nobody_can_tell_if_setup_runs_the_log_decides_by_its_age(self):
        self.setup_alive = None
        log = self._attempt()["log"]
        app = self._start(self._attempt(), log_text="Setup started\n")        # written a moment ago
        self.assertEqual(self.timers, [])
        self.assertFalse(app._update_failed_notice)
        os.utime(log, (1_000_000_000, 1_000_000_000))                          # ...and one that went quiet long ago
        app = self._start(self._attempt())
        self.assertTrue(app._update_failed_notice)

    def test_the_failure_notice_holds_the_update_available_notice_back_until_shown(self):
        app = self._start(self._attempt(), log_text="Log closed.\n")
        app._prompt_update("v2.0.2")
        app.note_update_check(updater.AVAILABLE, "v2.0.2", manual=False)
        app._nudge_update_after_dictation()
        self.assertEqual((app.hints, app.sig_update_available.emitted, self.timers), ([], [], [(3000, self.timers[0][1])]))
        self.run_all_timers()
        self.assertFalse(app._update_failed_notice)
        app._prompt_update("v2.0.2")                                  # after the window was shown: the quiet notice may come
        self.assertEqual(app.hints[-1][0], "Update available")

    def test_the_failure_never_covers_an_update_started_since(self):
        app = self._start(self._attempt(), log_text="Log closed.\n")
        app._update_installing = True                                 # they pressed Install in the 3 seconds
        self.run_all_timers()
        self.assertIsNone(app._update_window)
        self.assertEqual(app.cfg["update_attempt"], {})
        self.assertFalse(app._update_failed_notice)

    def test_a_stopped_install_at_quit_is_one_notice_whose_click_tries_again(self):
        app = self._start(self._attempt(quit=True), log_text="Log closed.\n")
        self.run_all_timers()
        self.assertIsNone(app._update_window)                         # no window: nobody was watching
        title, text, on_click = app.hints[0]
        self.assertEqual(title, "Gusan didn't update")
        self.assertIn("v2.0.1", text)
        again = []
        app.install_update = lambda tag, manual=True, on_done=None: again.append((tag, manual))
        on_click()
        self.assertEqual(again, [("v2.0.1", True)])

    def test_a_garbled_record_is_dropped(self):
        for bad in ({"to": ""}, {"to": "v2/../x"}, {"log": "x"}):
            app = self._start(dict(bad))
            self.assertEqual(app.cfg["update_attempt"], {})
        app = self._start("not a dict")
        self.assertEqual(self.timers, [])

    def test_the_failed_window_offers_try_again_which_installs_again(self):
        app = self._start(self._attempt(), log_text="Log closed.\n")
        self.run_all_timers()
        with mock.patch.object(main, "APP_VERSION", OLD):
            app._update_window.retry_requested.emit()
        self.assertEqual(len(self.fetches), 1)
        self.launch.assert_called_once()
        self.assertEqual(app.cfg["update_attempt"], {"to": "v2.0.1", "log": LOG, "from": OLD})


# ═════════════════════════ the calm notices stay calm ═════════════════════════
class TestNoSecondPopup(Harness):
    def test_no_notice_while_an_update_runs_or_starts(self):
        for attr in ("_update_installing", "_update_closing", "_update_failed_notice"):
            app = self.make_app(**{attr: True})
            app._prompt_update("v2.0.2")
            app._nudge_update_after_dictation()
            app.note_update_check(updater.AVAILABLE, "v2.0.2", manual=False)
            self.assertEqual(app.hints, [], attr)
            self.assertEqual(app.sig_update_available.emitted, [], attr)
            self.assertIsNone(app._update_notice_shown, attr)         # still comes, once, after a later dictation

    def test_one_quiet_notice_a_run_stays_as_it_was(self):
        self.app._prompt_update("v2.0.2")
        self.app._prompt_update("v2.0.2")
        self.assertEqual(len(self.app.hints), 1)


class TestAttemptRecord(unittest.TestCase):
    """cfg["update_attempt"] is the controller's own note about the install it handed to Setup."""

    def test_it_is_a_known_setting_that_no_page_can_write(self):
        import settings_apply
        self.assertEqual(main.DEFAULT["update_attempt"], {})
        self.assertIn("update_attempt", settings_apply.PROTECTED_KEYS)
        me = SimpleNamespace(cfg={"update_attempt": {"to": "v2.0.1", "log": "x.log", "from": OLD}})
        self.assertEqual(settings_apply.apply(me, {"update_attempt": {}}), {"update_attempt": False})
        self.assertEqual(me.cfg["update_attempt"]["to"], "v2.0.1")


if __name__ == "__main__":
    unittest.main()
