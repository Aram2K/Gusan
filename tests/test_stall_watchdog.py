"""The field watchdog for a frozen window (stall_watchdog.py): fake clocks, no Qt.

It must say ONE thing when the GUI stops beating for 5 s, repeat at most every
30 s, say how long the freeze was when the window answers again, name where the
GUI thread is stuck and what the other threads do - and nothing private.
"""
import logging
import sys
import threading
import time
import unittest

import stall_watchdog
from stall_watchdog import StallWatchdog


class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class _Records(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.records = []

    def emit(self, record):
        self.records.append(record)

    def messages(self):
        return [r.getMessage() for r in self.records]


def _logger(name):
    lg = logging.getLogger(name)
    lg.setLevel(logging.DEBUG)
    lg.propagate = False
    for h in list(lg.handlers):
        lg.removeHandler(h)
    h = _Records()
    lg.addHandler(h)
    return lg, h


def _dog(name, **kw):
    lg, h = _logger(name)
    clock = _Clock()
    kw.setdefault("poll_s", 1.0)
    dog = StallWatchdog(lg, clock=clock, **kw)
    return dog, clock, h


def _run(dog, clock, seconds, beats_until=None, look_every=1.0):
    """Advance the fake clock second by second: the GUI beats every second
    until ``beats_until`` (seconds from now; None = for ever), the watchdog looks."""
    t0 = clock.t
    out = []
    n = int(seconds / look_every)
    for _ in range(n):
        clock.t += look_every
        if beats_until is None or clock.t - t0 <= beats_until:
            dog.beat()
        out.append(dog.check())
    return out


class TestDecision(unittest.TestCase):
    def test_a_beating_gui_is_never_logged(self):
        dog, clock, h = _dog("wd.quiet")
        _run(dog, clock, 120)
        self.assertEqual(h.messages(), [])
        self.assertEqual(dog.stalls, 0)

    def test_a_gap_under_the_threshold_is_silent(self):
        dog, clock, h = _dog("wd.short")
        _run(dog, clock, 3)
        clock.t += 4.9                      # the window was busy for 4.9 s
        self.assertIsNone(dog.check())
        dog.beat()
        clock.t += 1
        self.assertIsNone(dog.check())
        self.assertEqual(h.messages(), [])

    def test_one_warning_at_five_seconds_with_the_stack_and_threads(self):
        dog, clock, h = _dog("wd.once", gui_thread_id=threading.get_ident())
        _run(dog, clock, 5, beats_until=2)  # beats stop after 2 s
        # 3 s of silence so far: nothing yet
        self.assertEqual(h.messages(), [])
        got = _run(dog, clock, 4, beats_until=0)
        self.assertEqual(got.count("stall"), 1)
        self.assertEqual(len(h.records), 1)
        rec = h.records[0]
        self.assertEqual(rec.levelno, logging.WARNING)
        msg = rec.getMessage()
        self.assertIn("GUI thread is not responding", msg)
        self.assertIn("GUI thread is in:", msg)
        # where the GUI thread is: this very test (the "GUI thread" of this fake)
        self.assertIn("test_one_warning_at_five_seconds_with_the_stack_and_threads", msg)
        self.assertIn("test_stall_watchdog.py", msg)
        self.assertIn("other threads", msg)

    def test_other_threads_are_listed_by_name(self):
        stop = threading.Event()
        t = threading.Thread(target=stop.wait, name="gusan-audio-test-thread", daemon=True)
        t.start()
        try:
            dog, clock, h = _dog("wd.threads", gui_thread_id=threading.get_ident())
            clock.t += 6
            dog.check()
        finally:
            stop.set()
            t.join(2)
        self.assertIn("gusan-audio-test-thread", h.messages()[0])

    def test_repeats_every_thirty_seconds_while_it_lasts(self):
        dog, clock, h = _dog("wd.repeat")
        got = _run(dog, clock, 100, beats_until=0)
        self.assertEqual(got.count("stall"), 1)
        self.assertEqual(got.count("still"), 3)             # 35 s, 65 s, 95 s
        self.assertEqual(len(h.records), 4)
        self.assertIn("still not responding", h.messages()[1])
        self.assertIn("35 s so far", h.messages()[1])

    def test_a_long_freeze_is_capped(self):
        dog, clock, h = _dog("wd.cap")
        _run(dog, clock, 60 * 60, beats_until=0)
        self.assertEqual(len(h.records), stall_watchdog.MAX_ENTRIES)

    def test_recovery_says_how_long_it_was(self):
        dog, clock, h = _dog("wd.recover", gui_thread_id=threading.get_ident())
        _run(dog, clock, 3)
        beat_before = clock.t
        _run(dog, clock, 12, beats_until=0)                 # silent for 12 s
        self.assertEqual(len(h.records), 1)
        clock.t += 0.5
        dog.beat()                                           # the window answers
        clock.t += 1
        self.assertEqual(dog.check(), "recovered")
        self.assertEqual(len(h.records), 2)
        self.assertIn("answering again after 12.5", h.messages()[1])
        self.assertAlmostEqual(dog.longest_s, 12.5, delta=0.6)
        self.assertGreaterEqual(clock.t - beat_before, 12)
        # and the next second is quiet again
        _run(dog, clock, 5)
        self.assertEqual(len(h.records), 2)

    def test_a_freeze_the_watchdog_itself_slept_through_is_still_reported(self):
        # Another thread held the interpreter lock for a minute: neither the
        # GUI timer nor this thread ran. The first look after says so.
        dog, clock, h = _dog("wd.gil")
        _run(dog, clock, 3)
        clock.t += 60
        dog.beat()                                           # the GUI beats first...
        clock.t += 0.2
        self.assertEqual(dog.check(), "recovered")           # ...then the watchdog wakes
        msg = h.messages()[-1]
        self.assertIn("answering again after 60", msg)
        self.assertIn("held up", msg)
        self.assertIn("ended before the watchdog could look", msg)

    def test_the_watchdog_waking_late_with_a_silent_gui_says_so(self):
        dog, clock, h = _dog("wd.late")
        _run(dog, clock, 3)
        clock.t += 90                                        # the whole process stalled
        self.assertEqual(dog.check(), "stall")
        self.assertIn("held up", h.messages()[0])
        self.assertIn("interpreter lock", h.messages()[0])


class _Cpu:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


class TestSleepIsNotAFreeze(unittest.TestCase):
    """A closed lid holds the watchdog thread up as much as a frozen window does -
    but the program uses no CPU while the computer sleeps."""

    def _dog(self, name):
        lg, h = _logger(name)
        clock, cpu = _Clock(), _Cpu()
        dog = StallWatchdog(lg, clock=clock, cpu_clock=cpu, poll_s=1.0, fault_stream=None)
        for _ in range(3):
            clock.t += 1
            cpu.t += 0.02
            dog.beat()
            dog.check()
        return dog, clock, cpu, h

    def _frozen_lines(self, h):
        return [m for m in h.messages() if "not responding" in m or "answering again" in m]

    def test_a_night_asleep_is_not_a_frozen_window(self):
        dog, clock, cpu, h = self._dog("wd.sleep1")
        clock.t += 8 * 3600                  # the lid was closed: no CPU used
        self.assertEqual(dog.check(), "slept")           # the watchdog thread wakes first...
        dog.beat()                                        # ...then the window
        clock.t += 1
        cpu.t += 0.02
        self.assertIsNone(dog.check())
        self.assertEqual(self._frozen_lines(h), [])
        self.assertEqual((dog.stalls, dog.slept), (0, 1))
        self.assertTrue(any("not a frozen window" in m for m in h.messages()))

    def test_the_window_waking_first_after_the_sleep_is_not_reported_either(self):
        dog, clock, cpu, h = self._dog("wd.sleep2")
        clock.t += 3600
        dog.beat()                                        # the window beats first: a one-hour "gap"
        clock.t += 0.2
        self.assertEqual(dog.check(), "slept")
        clock.t += 1
        cpu.t += 0.02
        dog.beat()
        self.assertIsNone(dog.check())
        self.assertEqual(self._frozen_lines(h), [])
        self.assertEqual(dog.stalls, 0)

    def test_a_stall_of_the_whole_program_that_used_the_cpu_is_still_a_freeze(self):
        # A native call that keeps the interpreter lock computes: the CPU is busy.
        dog, clock, cpu, h = self._dog("wd.sleep3")
        clock.t += 90
        cpu.t += 88
        self.assertEqual(dog.check(), "stall")
        self.assertEqual(dog.slept, 0)
        self.assertIn("held up", h.messages()[-1])

    def test_a_freeze_the_watchdog_saw_coming_is_unchanged(self):
        dog, clock, cpu, h = self._dog("wd.sleep4")
        for _ in range(8):                               # the window stops beating; the watchdog keeps looking
            clock.t += 1
            cpu.t += 0.9
            dog.check()
        self.assertEqual(dog.stalls, 1)
        self.assertEqual(dog.slept, 0)

    def test_a_real_clock_reads_the_cpu_and_a_fake_one_does_not(self):
        lg, _h = _logger("wd.sleep5")
        self.assertIsNotNone(StallWatchdog(lg, fault_stream=None)._cpu)
        self.assertIsNone(StallWatchdog(lg, clock=_Clock(), fault_stream=None)._cpu)


class TestPrivacy(unittest.TestCase):
    def test_no_variables_no_source_lines_no_file_names_of_the_user(self):
        secret = "ANNA-IS-PREGNANT-and-the-budget-is-1.2M"
        found = {}

        def where_the_gui_is_stuck(transcript_text):
            dog, clock, h = _dog("wd.privacy", gui_thread_id=threading.get_ident())
            clock.t += 6
            dog.check()
            found["msg"] = h.messages()[0]
            return transcript_text

        where_the_gui_is_stuck(secret)
        msg = found["msg"]
        self.assertIn("where_the_gui_is_stuck", msg)
        self.assertNotIn(secret, msg)
        self.assertNotIn("transcript_text", msg)             # no variable names either
        self.assertNotIn("found[", msg)                      # no source lines
        # a source location is "dir/file.py:line in function", two path parts at most
        for line in msg.splitlines():
            if ".py:" in line:
                path = line.strip().split(":")[0]
                self.assertLessEqual(path.count("/"), 1, line)
                self.assertNotIn("\\", path)

    def test_nothing_but_log_text_leaves_the_module(self):
        # It writes through the logger only: no network, no files of its own,
        # no analytics - the module imports nothing that could send anything.
        import ast
        import inspect
        tree = ast.parse(inspect.getsource(stall_watchdog))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add((node.module or "").split(".")[0])
        allowed = {"faulthandler", "logging", "os", "sys", "threading", "time", "traceback"}
        self.assertEqual(imported - allowed, set())


class TestSwitches(unittest.TestCase):
    def test_off_under_pytest_unless_asked(self):
        self.assertFalse(stall_watchdog.enabled_by_environment())          # this run
        self.assertFalse(stall_watchdog.enabled_by_environment({}, {"pytest": object()}))
        self.assertTrue(stall_watchdog.enabled_by_environment({}, {}))
        self.assertTrue(stall_watchdog.enabled_by_environment({stall_watchdog.ENV: "1"}, {"pytest": 1}))
        self.assertFalse(stall_watchdog.enabled_by_environment({stall_watchdog.ENV: "0"}, {}))
        self.assertFalse(stall_watchdog.enabled_by_environment({"PYTEST_CURRENT_TEST": "x"}, {}))

    def test_start_is_a_no_op_in_tests(self):
        self.assertIsNone(stall_watchdog.start())
        before = {t.name for t in threading.enumerate()}
        self.assertNotIn("gusan-stall-watchdog", before)

    def test_start_when_asked_returns_a_running_watchdog(self):
        lg, h = _logger("wd.start")
        dog = stall_watchdog.start(lg, environ={stall_watchdog.ENV: "1"}, modules={}, fault_stream=None,
                                   poll_s=0.02, threshold_s=0.1)
        self.assertIsNotNone(dog)
        try:
            names = [t.name for t in threading.enumerate()]
            self.assertIn("gusan-stall-watchdog", names)
        finally:
            dog.stop()
        self.assertNotIn("gusan-stall-watchdog", [t.name for t in threading.enumerate()])


class TestRealThread(unittest.TestCase):
    def test_a_real_freeze_is_logged_and_the_recovery_too(self):
        lg, h = _logger("wd.real")
        dog = StallWatchdog(lg, threshold_s=0.15, relog_s=0.3, poll_s=0.02,
                            gui_thread_id=threading.get_ident(), fault_stream=None)
        dog.start()
        try:
            end = time.monotonic() + 0.5          # the "GUI" is busy: never beats
            while time.monotonic() < end:
                time.sleep(0.01)
            dog.beat()                             # answers again
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and not any("answering again" in m for m in h.messages()):
                dog.beat()
                time.sleep(0.02)
        finally:
            dog.stop()
        msgs = h.messages()
        self.assertTrue(any("not responding" in m for m in msgs), msgs)
        self.assertTrue(any("answering again after" in m for m in msgs), msgs)
        self.assertGreaterEqual(dog.stalls, 1)

    def test_the_cost_of_a_look_is_tiny(self):
        dog, clock, h = _dog("wd.cost")
        t0 = time.perf_counter()
        for _ in range(2000):
            clock.t += 1
            dog.beat()
            dog.check()
        self.assertLess(time.perf_counter() - t0, 0.5)       # 2000 s of watching in half a second at most


class TestFaultDumpBackstop(unittest.TestCase):
    def test_it_is_armed_for_the_whole_process_stall_and_pushed_back_rarely(self):
        import faulthandler
        calls = []
        orig = faulthandler.dump_traceback_later
        faulthandler.dump_traceback_later = lambda *a, **k: calls.append((a, k))
        try:
            dog = StallWatchdog(_logger("wd.fault")[0], fault_stream=sys.stderr, fault_dump_s=45, fault_rearm_s=10)
            dog._arm_fault_dump(now=100.0)
            dog._arm_fault_dump(now=105.0)                   # too soon
            dog._arm_fault_dump(now=111.0)
        finally:
            faulthandler.dump_traceback_later = orig
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][0][0], 45)
        self.assertIs(calls[0][1]["file"], sys.stderr)
        self.assertTrue(calls[0][1]["repeat"])

    def test_no_stream_no_backstop(self):
        import faulthandler
        calls = []
        orig = faulthandler.dump_traceback_later
        faulthandler.dump_traceback_later = lambda *a, **k: calls.append(1)
        try:
            StallWatchdog(_logger("wd.nofault")[0], fault_stream=None)._arm_fault_dump(now=1.0)
        finally:
            faulthandler.dump_traceback_later = orig
        self.assertEqual(calls, [])

    def test_the_log_file_of_the_root_logger_is_found(self):
        import os
        import tempfile
        d = tempfile.mkdtemp()
        path = os.path.join(d, "x.log")
        lg = logging.getLogger("wd.stream.owner")
        lg.propagate = False
        fh = logging.FileHandler(path)
        lg.addHandler(fh)
        try:
            self.assertIs(stall_watchdog.log_stream(lg), fh.stream)
        finally:
            lg.removeHandler(fh)
            fh.close()
        self.assertIsNone(stall_watchdog.log_stream(lg))


if __name__ == "__main__":
    unittest.main()
