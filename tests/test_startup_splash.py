"""The loading card's logic (startup_splash.py): when there is one, how it is started
and ended, and that it never outlives the app. Qt-free: the card process itself is
faked (a plain loop over the same lifecycle code); the real window is in
tests_qt/test_startup_splash_window.py. Real processes where the point is a real
process: the app dying, a stalled app, a hung start-up."""
import ast
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock

try:
    from tests import test_core  # noqa: F401  (importing installs the stand-ins main needs)
except ImportError:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import test_core  # noqa: F401

import main
import startup_splash as ss

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FROZEN_WIN = {"env": {}, "frozen": True, "platform": "win32"}


def _read(name):
    with open(os.path.join(ROOT, name), encoding="utf-8") as f:
        return f.read()


class FakePipe:
    def __init__(self, broken=False):
        self.data, self.closed, self.broken = b"", False, broken

    def write(self, b):
        if self.broken:
            raise BrokenPipeError()
        self.data += b

    def flush(self):
        if self.broken:
            raise OSError("broken pipe")

    def close(self):
        self.closed = True


class FakeProc:
    def __init__(self, broken=False):
        self.stdin, self.killed = FakePipe(broken), False

    def kill(self):
        self.killed = True


class Popens:
    """A Popen stand-in that records how the card was started."""

    def __init__(self, fail=False):
        self.calls, self.procs, self.fail = [], [], fail

    def __call__(self, cmd, **kw):
        if self.fail:
            raise OSError("no such file")
        self.calls.append((cmd, kw))
        self.procs.append(FakeProc())
        return self.procs[-1]


class CardState(unittest.TestCase):
    """Every test starts with no card and ends with none."""

    def setUp(self):
        ss._settle()
        ss._active = None
        ss._starter = None

    tearDown = setUp


class TestWhenThereIsACard(unittest.TestCase):
    def test_a_packaged_windows_build_gets_one(self):
        self.assertTrue(ss.enabled(env={}, frozen=True, platform="win32"))

    def test_a_source_run_gets_none_unless_asked(self):
        self.assertFalse(ss.enabled(env={}, frozen=False, platform="win32"))
        self.assertTrue(ss.enabled(env={"GUSAN_SPLASH": "1"}, frozen=False, platform="win32"))

    def test_it_can_be_switched_off(self):
        self.assertFalse(ss.enabled(env={"GUSAN_SPLASH": "0"}, frozen=True, platform="win32"))

    def test_tests_get_none(self):
        self.assertFalse(ss.enabled(env={"PYTEST_CURRENT_TEST": "x", "GUSAN_SPLASH": "1"}, frozen=True,
                                    platform="win32"))

    def test_other_systems_get_none_until_they_are_tried(self):
        # macOS: a second copy of the bundle would show a second Dock icon.
        self.assertEqual(ss.PLATFORMS, ("win32",))
        self.assertFalse(ss.enabled(env={}, frozen=True, platform="darwin"))
        self.assertFalse(ss.enabled(env={}, frozen=True, platform="linux"))

    def test_the_quiet_start_at_login_launch_gets_none(self):
        self.assertFalse(ss.launch_wants_splash(["TranscribeApp.exe", "--background"], **FROZEN_WIN))
        self.assertTrue(ss.launch_wants_splash(["TranscribeApp.exe"], **FROZEN_WIN))
        self.assertTrue(ss.launch_wants_splash(["TranscribeApp.exe", "--show-settings"], **FROZEN_WIN))

    def test_the_helper_processes_get_none(self):
        for flag in ("--splash-worker", "--diarize-worker"):
            self.assertFalse(ss.launch_wants_splash(["TranscribeApp.exe", flag, "1"], **FROZEN_WIN))


class TestASecondLaunch(unittest.TestCase):
    """main.py asks first thing: is another Gusan on the port? Then hand over and end."""

    def test_the_constants_are_main_s_own(self):
        self.assertEqual(ss.SINGLE_INSTANCE_PORT, main.SINGLE_INSTANCE_PORT)
        self.assertEqual(ss.SINGLE_INSTANCE_PORT, 47823)
        self.assertEqual(ss.IPC_ACTIONS, main.IPC_ACTIONS)
        self.assertEqual(ss.HANDSHAKE, b"transcribe-ok")

    def test_the_request_is_main_s_launch_action(self):
        for argv in (["x"], ["x", "show_meeting"], ["x", "live_prompter"], ["x", "show_onboarding"],
                     ["x", "--background"], ["x", "--show-settings"], ["x", "--background", "show_meeting"],
                     ["x", "nonsense"]):
            self.assertEqual(ss.second_launch_action(argv), main.launch_action(argv), argv)

    def _listener(self):
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(5)
        got = []

        def accept():
            try:
                conn, _ = srv.accept()
                got.append(conn.recv(64))
                conn.sendall(ss.HANDSHAKE)
                conn.close()
            except OSError:
                pass
        t = threading.Thread(target=accept, daemon=True)
        t.start()
        return srv, srv.getsockname()[1], got, t

    def test_a_free_port_is_free_and_stays_bindable_afterwards(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        self.assertTrue(ss.port_is_free(port))
        again = socket.socket()
        again.bind(("127.0.0.1", port))          # the probe did not keep the port
        again.close()

    def test_an_owned_port_is_not_free(self):
        srv, port, _, _ = self._listener()
        try:
            self.assertFalse(ss.port_is_free(port))
        finally:
            srv.close()

    def test_a_running_gusan_gets_the_request_and_this_launch_ends(self):
        srv, port, got, t = self._listener()
        try:
            self.assertTrue(ss.another_is_running(["Gusan.exe", "show_meeting"], port=port, **FROZEN_WIN))
            t.join(3)
        finally:
            srv.close()
        self.assertEqual(got, [b"show_meeting"])

    def test_a_plain_second_launch_asks_to_open_the_app(self):
        srv, port, got, t = self._listener()
        try:
            self.assertTrue(ss.another_is_running(["Gusan.exe"], port=port, **FROZEN_WIN))
            t.join(3)
        finally:
            srv.close()
        self.assertEqual(got, [b"show_settings"])

    def test_a_copy_that_has_not_started_accepting_still_ends_this_launch(self):
        # Bound and listening (the backlog keeps the request) but its accept thread is not
        # running yet, so no reply comes: the launch ends all the same, without a long wait.
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(5)
        try:
            t0 = time.time()
            self.assertTrue(ss.another_is_running(["Gusan.exe"], port=srv.getsockname()[1], **FROZEN_WIN))
            self.assertLess(time.time() - t0, 3.0)
            conn, _ = srv.accept()                # when it does accept, the request is waiting
            self.assertEqual(conn.recv(64), b"show_settings")
            conn.close()
        finally:
            srv.close()

    def test_a_free_port_means_this_launch_is_the_app(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        self.assertFalse(ss.another_is_running(["Gusan.exe"], port=port, **FROZEN_WIN))

    def test_a_launch_without_a_card_never_looks_at_the_port(self):
        with mock.patch.object(ss, "port_is_free") as free, mock.patch.object(ss, "deliver") as deliver:
            self.assertFalse(ss.another_is_running(["Gusan.exe", "--background"], **FROZEN_WIN))
            self.assertFalse(ss.another_is_running(["Gusan.exe"], env={}, frozen=False, platform="win32"))
        free.assert_not_called()
        deliver.assert_not_called()


class TestStarting(CardState):
    def test_it_starts_the_program_again_with_the_app_s_pid(self):
        popen = Popens()
        self.assertTrue(ss.start(["Gusan.exe"], gate=FROZEN_WIN, popen=popen, platform="win32",
                                 frozen=True, executable=r"C:\Gusan\TranscribeApp.exe",
                                 getpid=lambda: 4321, now=lambda: 1700000000.5))
        ss._settle()
        (cmd, kw), = popen.calls
        self.assertEqual(cmd, [r"C:\Gusan\TranscribeApp.exe", "--splash-worker", "4321", "1700000000.5"])
        self.assertEqual(kw["stdin"], subprocess.PIPE)
        self.assertEqual(kw["stdout"], subprocess.DEVNULL)
        self.assertEqual(kw["stderr"], subprocess.DEVNULL)
        self.assertTrue(kw["close_fds"])

    def test_it_has_no_console_and_never_competes_with_the_app_for_the_cpu(self):
        popen = Popens()
        ss.start(["Gusan.exe"], gate=FROZEN_WIN, popen=popen, platform="win32", frozen=True)
        ss._settle()
        flags = popen.calls[0][1]["creationflags"]
        self.assertTrue(flags & 0x08000000, "CREATE_NO_WINDOW")
        self.assertTrue(flags & 0x00004000, "BELOW_NORMAL_PRIORITY_CLASS")

    def test_a_source_run_starts_main_py(self):
        popen = Popens()
        ss.start(["main.py"], gate={"env": {"GUSAN_SPLASH": "1"}, "frozen": False, "platform": "win32"},
                 popen=popen, platform="win32", frozen=False, executable="python.exe")
        ss._settle()
        cmd = popen.calls[0][0]
        self.assertEqual(cmd[0], "python.exe")
        self.assertEqual(os.path.normcase(cmd[1]), os.path.normcase(os.path.join(ROOT, "main.py")))
        self.assertEqual(cmd[2], "--splash-worker")

    def test_a_launch_without_a_card_starts_nothing(self):
        popen = Popens()
        self.assertFalse(ss.start(["Gusan.exe", "--background"], gate=FROZEN_WIN, popen=popen))
        self.assertFalse(ss.start(["main.py"], gate={"env": {}, "frozen": False, "platform": "win32"}, popen=popen))
        ss._settle()
        self.assertEqual(popen.calls, [])

    def test_a_card_that_cannot_start_is_no_card_and_no_error(self):
        popen = Popens(fail=True)
        ss.start(["Gusan.exe"], gate=FROZEN_WIN, popen=popen, platform="win32", frozen=True)
        ss._settle()
        self.assertIsNone(ss._active)
        ss.dismiss()
        ss.kill()                              # nothing to end, nothing raised

    def test_it_never_starts_twice(self):
        popen = Popens()
        for _ in range(3):
            ss.start(["Gusan.exe"], gate=FROZEN_WIN, popen=popen, platform="win32", frozen=True)
            ss._settle()
        self.assertEqual(len(popen.calls), 1)
        ss.start_for_window(gate=FROZEN_WIN, popen=popen, platform="win32", frozen=True)
        ss._settle()
        self.assertEqual(len(popen.calls), 1, "the launch's own card is kept when the window opens")

    def test_starting_does_not_wait_for_the_process(self):
        # CreateProcess takes ~50 ms: it runs on a thread of its own.
        gate = threading.Event()

        def slow(cmd, **kw):
            gate.wait(5)
            return FakeProc()
        t0 = time.time()
        ss.start(["Gusan.exe"], gate=FROZEN_WIN, popen=slow, platform="win32", frozen=True)
        self.assertLess(time.time() - t0, 1.0)
        gate.set()
        ss._settle()
        self.assertIsNotNone(ss._active)

    def test_a_dismiss_while_the_process_is_being_created_never_waits_and_still_ends_the_card(self):
        gate, procs = threading.Event(), []

        def slow(cmd, **kw):
            gate.wait(5)
            procs.append(FakeProc())
            return procs[-1]
        ss.start(["Gusan.exe"], gate=FROZEN_WIN, popen=slow, platform="win32", frozen=True)
        t0 = time.time()
        ss.dismiss()                            # the window is up before the card process even exists
        self.assertLess(time.time() - t0, 0.5, "the app's window thread must never wait for the card")
        gate.set()
        ss._settle()
        self.assertTrue(procs[0].killed, "a card created after its dismiss must not stay up")
        self.assertIsNone(ss._active)

    def test_a_card_started_after_a_dismiss_is_a_new_card(self):
        popen = Popens()
        ss.start(["Gusan.exe"], gate=FROZEN_WIN, popen=popen, platform="win32", frozen=True)
        ss._settle()
        ss.dismiss()
        self.assertTrue(ss.start_for_window(gate=FROZEN_WIN, popen=popen, platform="win32", frozen=True))
        ss._settle()
        self.assertEqual(len(popen.calls), 2)
        self.assertIs(ss._active.proc, popen.procs[1])
        self.assertFalse(popen.procs[1].killed)

    def test_the_window_of_a_running_app_gets_a_card_when_it_is_built_for_the_first_time(self):
        popen = Popens()
        self.assertTrue(ss.start_for_window(gate=FROZEN_WIN, popen=popen, platform="win32", frozen=True))
        ss._settle()
        self.assertEqual(len(popen.calls), 1)
        self.assertFalse(ss.start_for_window(gate={"env": {}, "frozen": False, "platform": "win32"}))


class TestEnding(CardState):
    def _card(self, broken=False):
        proc = FakeProc(broken)
        ss._active = ss.Card(proc)
        return proc

    def test_dismiss_sends_close_and_shuts_the_pipe(self):
        proc = self._card()
        ss.dismiss()
        self.assertEqual(proc.stdin.data, b"c")
        self.assertTrue(proc.stdin.closed)
        self.assertFalse(proc.killed)
        self.assertIsNone(ss._active)

    def test_dismiss_is_idempotent_and_safe_with_no_card(self):
        proc = self._card()
        ss.dismiss()
        ss.dismiss()
        ss.kill()
        self.assertEqual(proc.stdin.data, b"c")
        self.assertFalse(proc.killed)

    def test_a_card_that_has_already_ended_cannot_break_the_app(self):
        proc = self._card(broken=True)
        ss.dismiss()                            # BrokenPipeError inside: swallowed
        self.assertTrue(proc.stdin.closed)

    def test_kill_ends_it_at_once_without_a_fade(self):
        proc = self._card()
        ss.kill()
        self.assertEqual(proc.stdin.data, b"")
        self.assertTrue(proc.stdin.closed)
        self.assertTrue(proc.killed)


class TestLifecycle(unittest.TestCase):
    class Watch:
        def __init__(self):
            self.up = True

        def alive(self):
            return self.up

    def _life(self, hard=60.0):
        self.now = [100.0]
        self.watch = self.Watch()
        return ss.Lifecycle(self.watch, clock=lambda: self.now[0], hard_timeout=hard)

    def test_it_runs_while_the_app_is_alive_and_silent_however_long_it_stalls(self):
        life = self._life()
        for t in (0.5, 5.0, 17.0, 30.0, 59.0):    # the log shows window stalls of 5-17 s
            self.now[0] = 100.0 + t
            self.assertEqual(life.poll(), ss.RUN, t)

    def test_close_fades(self):
        life = self._life()
        life.close_requested = True
        self.assertEqual(life.poll(), ss.FADE)

    def test_a_closed_pipe_fades(self):
        life = self._life()
        life.pipe_closed = True
        self.assertEqual(life.poll(), ss.FADE)

    def test_an_app_that_is_gone_ends_it_at_once_even_mid_fade(self):
        life = self._life()
        self.watch.up = False
        self.assertEqual(life.poll(), ss.QUIT)
        life.close_requested = True
        self.assertEqual(life.poll(), ss.QUIT)

    def test_the_last_guard_for_an_app_that_never_says_it_is_ready(self):
        life = self._life(hard=60.0)
        self.now[0] = 100.0 + 59.9
        self.assertEqual(life.poll(), ss.RUN)
        self.now[0] = 100.0 + 60.0
        self.assertEqual(life.poll(), ss.FADE)
        self.assertEqual(ss.HARD_TIMEOUT_S, 60.0)

    def test_it_runs_without_a_watch(self):
        life = ss.Lifecycle(None)
        self.assertEqual(life.poll(), ss.RUN)


class TestPipeReader(unittest.TestCase):
    def _run(self, reads):
        it = iter(reads)

        def read(fd, n):
            item = next(it)
            if isinstance(item, Exception):
                raise item
            return item
        life = ss.Lifecycle(None)
        ss.watch_pipe(life, read=read).join(3)
        return life

    def test_close_means_the_window_is_up(self):
        life = self._run([b"c"])
        self.assertTrue(life.close_requested)
        self.assertFalse(life.pipe_closed)

    def test_end_of_input_means_the_app_is_gone(self):
        life = self._run([b""])
        self.assertTrue(life.pipe_closed)
        self.assertFalse(life.close_requested)

    def test_a_pipe_that_cannot_be_read_changes_nothing(self):
        life = self._run([OSError("bad fd")])
        self.assertFalse(life.close_requested or life.pipe_closed)
        self.assertEqual(life.poll(), ss.RUN)


class TestMotion(unittest.TestCase):
    def test_animations_are_on_off_windows_and_when_they_cannot_be_read(self):
        self.assertTrue(ss.animations_enabled(platform="linux"))
        self.assertTrue(ss.animations_enabled(platform="darwin"))
        self.assertIsInstance(ss.animations_enabled(), bool)

    @unittest.skipUnless(sys.platform == "win32", "Windows' setting")
    def test_the_windows_setting_is_read_and_a_failing_read_means_on(self):
        import ctypes

        def off(action, param, ref, flags):
            ref._obj.value = 0
            return 1
        with mock.patch.object(ctypes, "windll") as windll:
            windll.user32.SystemParametersInfoW.side_effect = off
            self.assertFalse(ss.animations_enabled())
            windll.user32.SystemParametersInfoW.side_effect = OSError("no")
            self.assertTrue(ss.animations_enabled())
            windll.user32.SystemParametersInfoW.side_effect = None
            windll.user32.SystemParametersInfoW.return_value = 0
            self.assertTrue(ss.animations_enabled())

    def test_the_card_is_told(self):
        seen = {}

        def ui(life, show_at=None, motion=True):
            seen.update(show_at=show_at, motion=motion)
            return 0
        with mock.patch.object(ss, "animations_enabled", return_value=False):
            self.assertEqual(ss.worker_main(["4242", "1700000000.0"], ui=ui, platform="linux"), 0)
        self.assertEqual(seen, {"show_at": 1700000000.25, "motion": False})


class TestParentWatch(unittest.TestCase):
    def test_no_pid_means_alive(self):
        self.assertTrue(ss.ParentWatch(None).alive())
        self.assertTrue(ss.ParentWatch(0).alive())

    @unittest.skipUnless(sys.platform == "win32", "the process handle is Windows' own")
    def test_a_process_handle_follows_a_real_process(self):
        p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(1.5)"])
        try:
            w = ss.ParentWatch(p.pid)
            self.assertTrue(w.alive())
            self.assertTrue(w.alive())
            p.wait(10)
            self.assertFalse(w.alive())
            self.assertFalse(w.alive())          # and stays so (the handle is closed once)
        finally:
            p.kill()
        gone = ss.ParentWatch(p.pid)
        self.assertFalse(gone.alive(), "a process that cannot be opened is not there")

    def test_elsewhere_it_follows_the_parent_pid(self):
        w = ss.ParentWatch(4242, platform="linux")
        with mock.patch.object(os, "getppid", return_value=4242), mock.patch.object(os, "kill"):
            self.assertTrue(w.alive())
        with mock.patch.object(os, "getppid", return_value=1):
            self.assertFalse(w.alive())

    def test_a_failing_system_call_means_alive_not_dead(self):
        w = ss.ParentWatch(4242, platform="linux")
        with mock.patch.object(os, "getppid", return_value=4242), \
                mock.patch.object(os, "kill", side_effect=PermissionError()):
            self.assertTrue(w.alive())


# A throwaway card process (no Qt): the real worker_main, lifecycle and pipe reader,
# with a plain loop as its window. Writes how it ended to a file.
CARD = textwrap.dedent('''
    import sys, time
    sys.path.insert(0, %(root)r)
    import startup_splash as ss
    result = %(result)r
    def ui(life, show_at=None, motion=True):
        while True:
            verdict = life.poll()
            if verdict != ss.RUN:
                with open(result, "w") as f:
                    f.write("%%s %%r" %% (verdict, time.time()))
                return 0
            time.sleep(0.02)
    sys.exit(ss.worker_main(sys.argv[1:], ui=ui, hard_timeout=%(hard)r))
''')

# A throwaway app: starts the card the way startup_splash does (stdin pipe, its own
# pid and launch time), then behaves as asked.
APP = textwrap.dedent('''
    import os, subprocess, sys, time
    card = subprocess.Popen([sys.executable, "-c", %(card)r, str(os.getpid()), repr(time.time())],
                            stdin=subprocess.PIPE)
    print(card.pid, flush=True)
    mode = %(mode)r
    if mode == "wait":
        time.sleep(60)
    elif mode == "die":
        time.sleep(1.5)
        os._exit(1)                              # a crash: nothing is closed, nothing is said
    elif mode == "stall_then_close":
        time.sleep(%(stall)r)                    # a window thread stuck for seconds: no heartbeat, no sign
        card.stdin.write(b"c"); card.stdin.flush(); card.stdin.close()
        card.wait(10)
''')


class TestTheCardNeverOutlivesTheApp(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="gusan-splash-test-")
        self.result = os.path.join(self.dir, "result.txt")
        self.procs = []

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)
        for p in self.procs:
            try:
                p.kill()
            except Exception:
                pass
        for pid in getattr(self, "pids", []):
            try:
                if sys.platform == "win32":
                    subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True)
                else:
                    os.kill(pid, 9)
            except Exception:
                pass

    def _app(self, mode, hard=60.0, stall=0.0):
        card = CARD % {"root": ROOT, "result": self.result, "hard": hard}
        code = APP % {"card": card, "mode": mode, "stall": stall}
        app = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
        self.procs.append(app)
        self.pids = [int(app.stdout.readline())]
        return app

    def _wait_result(self, timeout):
        end = time.time() + timeout
        while time.time() < end:
            try:
                with open(self.result) as f:
                    text = f.read()
                if text:
                    verdict, stamp = text.split()
                    return verdict, float(stamp)
            except (OSError, ValueError):
                pass
            time.sleep(0.05)
        return None

    def test_a_killed_app_takes_the_card_with_it(self):
        # "End task", or Windows closing a window that does not answer: TerminateProcess.
        app = self._app("wait")
        time.sleep(1.5)                          # the card is up and polling
        self.assertIsNone(self._wait_result(0.0), "the card must stay while its app lives")
        t0 = time.time()
        app.kill()
        got = self._wait_result(6)
        self.assertIsNotNone(got, "the card is still there after its app was killed")
        self.assertLess(got[1] - t0, 2.5)
        self.assertIn(got[0], ("quit", "fade"))

    def test_a_crashed_app_takes_the_card_with_it(self):
        app = self._app("die")
        t0 = time.time()
        got = self._wait_result(8)
        self.assertIsNotNone(got, "the card is still there after its app crashed")
        self.assertLess(got[1] - t0, 3.5)                # the app lives 1.5 s, then ends at once
        app.wait(5)

    def test_a_live_app_that_stalls_for_seconds_keeps_its_card(self):
        # The window thread is stuck for 4 s (the founder's log: 5-17 s) and sends nothing.
        app = self._app("stall_then_close", stall=4.0)
        t0 = time.time()
        time.sleep(3.2)
        self.assertIsNone(self._wait_result(0.0), "a silent app is not a dead app")
        got = self._wait_result(8)
        self.assertIsNotNone(got)
        self.assertEqual(got[0], "fade")
        self.assertGreaterEqual(got[1] - t0, 3.5)
        app.wait(10)

    def test_the_last_guard_ends_a_card_whose_app_stays_alive_and_silent(self):
        app = self._app("wait", hard=2.0)
        t0 = time.time()
        got = self._wait_result(8)
        self.assertIsNotNone(got)
        self.assertEqual(got[0], "fade")
        self.assertGreaterEqual(got[1] - t0, 1.5)
        self.assertIsNone(app.poll(), "the app itself was not touched")


class TestMainWiring(CardState):
    """Where main.py starts and ends the card."""

    def test_the_card_starts_before_anything_heavy_is_imported(self):
        src = _read("main.py")
        heavy = src.index("import numpy as np")
        for marker in ("import startup_splash", "startup_splash.another_is_running(sys.argv)",
                       "startup_splash.start(sys.argv)", '["--splash-worker"]'):
            self.assertLess(src.index(marker), heavy, marker)
        self.assertLess(src.index("startup_splash.another_is_running(sys.argv)"),
                        src.index("startup_splash.start(sys.argv)"))

    def test_the_card_process_is_dispatched_before_anything_else_loads(self):
        src = _read("main.py")
        branch = src.index('sys.argv[1:2] == ["--splash-worker"]')
        self.assertLess(branch, src.index("import threading"))
        self.assertLess(src.index('sys.argv[1:2] == ["--diarize-worker"]'), branch)
        self.assertIn("startup_splash.worker_main(sys.argv[2:])", src)

    def test_a_second_launch_ends_without_a_card_even_when_it_lost_the_race(self):
        src = _read("main.py")
        body = src[src.index("def main():"):]
        branch = body[body.index("if not lock_sock:"):body.index("sys.exit(0)")]
        self.assertIn("startup_splash.kill()", branch)
        # the single-instance lock is still taken inside main(), after the imports
        self.assertEqual(body.count("acquire_single_instance_lock()"), 1)
        self.assertNotIn("acquire_single_instance_lock", src[:src.index("def acquire_single_instance_lock")])

    def test_the_ipc_port_and_its_handshake_are_untouched(self):
        src = _read("main.py")
        self.assertIn("SINGLE_INSTANCE_PORT = 47823", src)
        self.assertIn('b"transcribe-ok"', src)
        self.assertIn('IPC_ACTIONS = ("show_settings", "show_onboarding", "show_meeting", "live_prompter")', src)

    def test_the_quit_ends_the_card_before_anything_shuts_down(self):
        tree = ast.parse(_read("main.py"))
        quit_src = next(ast.get_source_segment(_read("main.py"), n) for n in ast.walk(tree)
                        if isinstance(n, ast.FunctionDef) and n.name == "quit_app")
        self.assertLess(quit_src.index("startup_splash.dismiss()"), quit_src.index("for stop in ("))

    # -- the three ways a window can (not) appear: each ends the card ------------
    def _recording(self):
        calls = []
        patches = [mock.patch.object(main.startup_splash, "dismiss", lambda: calls.append("dismiss")),
                   mock.patch.object(main.startup_splash, "start_for_window",
                                     lambda **k: calls.append("start_for_window"))]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        return calls

    def _window(self, calls, name="win"):
        w = SimpleNamespace(isMinimized=lambda: False, show=lambda: calls.append(name + ".show"),
                            raise_=lambda: None, activateWindow=lambda: calls.append(name + ".activate"),
                            current_route=lambda: None)
        w.open = lambda route, **o: calls.append(name + ".open")
        return w

    def test_the_main_window_shows_then_the_card_ends(self):
        calls = self._recording()
        win = self._window(calls)
        app = SimpleNamespace(main_win=None, _ensure_main_window=lambda: win,
                              _bring_to_front=main.AppController._bring_to_front,
                              track=lambda *a, **k: calls.append("track"))
        self.assertIs(main.AppController.show_main(app, "home"), win)
        self.assertEqual(calls, ["start_for_window", "win.open", "win.show", "win.activate", "dismiss", "track"])

    def test_a_window_that_is_already_built_starts_no_new_card(self):
        calls = self._recording()
        win = self._window(calls)
        app = SimpleNamespace(main_win=win, _ensure_main_window=lambda: win,
                              _bring_to_front=main.AppController._bring_to_front, track=lambda *a, **k: None)
        main.AppController.show_main(app, "home")
        self.assertNotIn("start_for_window", calls)
        self.assertEqual(calls[-1], "dismiss")

    def test_the_first_run_wizard_shows_then_the_card_ends(self):
        calls = self._recording()
        wiz = self._window(calls, "wizard")
        wiz.accepted = SimpleNamespace(connect=lambda f: None)
        fake_module = SimpleNamespace(OnboardingWizard=lambda controller: wiz)
        app = SimpleNamespace(cfg={"onboarding_done": False}, downloads=None, _bring_to_front=main.AppController._bring_to_front)
        with mock.patch.dict(sys.modules, {"ui.onboarding_v2": fake_module}):
            self.assertIs(main.AppController.show_onboarding(app), wiz)
        self.assertEqual(calls, ["wizard.show", "wizard.activate", "dismiss"])

    def test_a_wizard_that_cannot_be_built_ends_the_card_too(self):
        calls = self._recording()

        def broken(controller):
            raise RuntimeError("no wizard")
        fake_module = SimpleNamespace(OnboardingWizard=broken)
        app = SimpleNamespace(cfg={"onboarding_done": False}, downloads=None, _bring_to_front=main.AppController._bring_to_front)
        with mock.patch.dict(sys.modules, {"ui.onboarding_v2": fake_module}):
            with self.assertRaises(RuntimeError):
                main.AppController.show_onboarding(app)
        self.assertEqual(calls, ["dismiss"])

    def test_a_window_that_cannot_be_built_ends_the_card_and_says_so(self):
        calls, hints = self._recording(), []
        app = SimpleNamespace(main_win=None, _ensure_main_window=mock.MagicMock(side_effect=RuntimeError("no screen")),
                              _bring_to_front=main.AppController._bring_to_front,
                              show_tray_hint=lambda t, m, on_click=None: hints.append(t),
                              track=lambda *a, **k: calls.append("track"))
        self.assertIsNone(main.AppController.show_main(app, "home"))
        self.assertEqual(calls, ["start_for_window", "dismiss"])
        self.assertEqual(hints, ["Gusan couldn't open its window"])

    def test_a_window_whose_show_fails_still_ends_the_card(self):
        calls = self._recording()
        win = self._window(calls)

        def boom():
            raise RuntimeError("native window error")
        win.show = boom
        with self.assertRaises(RuntimeError):
            main.AppController._bring_to_front(win)
        self.assertEqual(calls, ["dismiss"])


class TestNothingHeavyAndBundled(unittest.TestCase):
    def test_the_module_needs_only_the_standard_library(self):
        tree = ast.parse(_read("startup_splash.py"))
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                names.add(node.module.split(".")[0])
        self.assertEqual(names - set(sys.stdlib_module_names), {"splash_window"})   # the card's window, loaded only in its process
        code = ("import sys; sys.modules['PySide6'] = None; import startup_splash; "
                "print(sorted(m for m in ('PySide6', 'numpy', 'psutil', 'main', 'splash_window') if m in sys.modules "
                "and sys.modules[m] is not None))")
        out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(out.stdout.strip(), "[]", out.stderr)

    def test_the_card_window_is_loaded_only_in_the_card_process(self):
        src = _read("startup_splash.py")
        self.assertEqual(src.count("from splash_window import run"), 1)
        # (a plain import statement inside worker_main: PyInstaller bundles the module because of it)
        self.assertLess(src.index("def worker_main"), src.index("from splash_window import run"))

    def test_the_card_window_imports_only_the_qt_basics(self):
        tree = ast.parse(_read("splash_window.py"))
        mods = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.ImportFrom) and n.module:
                mods.add(n.module)
            elif isinstance(n, ast.Import):
                mods.update(a.name for a in n.names)
        # no widgets, no ctypes, no theme or other app module: the card process starts fast
        self.assertEqual(mods, {"math", "sys", "time", "PySide6.QtCore", "PySide6.QtGui", "app_info", "startup_splash"})


if __name__ == "__main__":
    unittest.main()
