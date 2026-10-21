"""When the speaker pass after the notes runs (speaker_followup.py): only while the
computer is free, polled gently; stopped at once when a recording / dictation / file
job starts and tried again later - at most five times per meeting; ended for good by
Cancel and by quitting; one meeting at a time, oldest first. The pass itself is a fake
here (its work is tested with meeting_speakers.refine_meeting).

Pure Python: runs under test_core's suite-wide stubs."""
import threading
import time
import unittest

import speaker_followup as sf


class Result:
    def __init__(self, state="done"):
        self.state = state


class Harness:
    """A follow-up whose worker runs inline and whose waiting is a hook."""

    def __init__(self, busy_for=0, on_run=None, max_retries=5, settle=0):
        self.busy = busy_for > 0            # busy until this many polls have passed
        self.polls = 0
        self.busy_polls = busy_for
        self.runs = []                      # (folder, cancel check results) per call
        self.changes = []
        self.on_run = on_run
        self.f = sf.SpeakerFollowUp(
            is_busy=lambda: self.busy, run=self._run, on_change=self.changes.append,
            spawn=lambda fn: fn(), poll_s=0.0, max_retries=max_retries, wait=self._wait,
            settle_polls=settle)

    def _wait(self, seconds):
        self.polls += 1
        if self.polls >= self.busy_polls:
            self.busy = False

    def _run(self, folder, should_cancel, status):
        status("working")
        self.runs.append(folder)
        if self.on_run is not None:
            return self.on_run(self, folder, should_cancel)
        return Result("done")


class TestWhenItRuns(unittest.TestCase):
    def test_it_runs_at_once_when_nothing_else_needs_the_computer(self):
        h = Harness()
        self.assertTrue(h.f.enqueue("A"))
        self.assertEqual(h.runs, ["A"])
        self.assertEqual(h.polls, 0)                       # nothing to wait for
        self.assertEqual(h.f.state("A"), "")               # and it is over
        self.assertEqual(h.f.state(), "")
        self.assertEqual(h.f.status_text, "working")

    def test_it_waits_gently_while_the_computer_is_busy(self):
        h = Harness(busy_for=3)
        h.f.enqueue("A")
        self.assertEqual(h.polls, 3)                       # looked three times, then ran
        self.assertEqual(h.runs, ["A"])
        self.assertEqual(sf.POLL_S, 5.0)                   # a gentle poll by default

    def test_state_goes_waiting_running_then_nothing_and_every_step_is_reported(self):
        seen = []

        def run(h, folder, cancel):
            seen.append(h.f.state(folder))
            return Result()
        h = Harness(busy_for=1, on_run=run)
        orig_wait = h._wait

        def wait(seconds):
            seen.append(h.f.state("A"))                    # while it waits for the computer
            orig_wait(seconds)
        h.f._wait = wait
        h.f.enqueue("A")
        self.assertEqual(seen, ["waiting", "running"])
        self.assertEqual(h.f.state("A"), "")
        self.assertEqual(set(h.changes), {"A"})            # the page is told
        self.assertGreaterEqual(len(h.changes), 3)         # queued, started, ended

    def test_one_meeting_at_a_time_oldest_first_and_no_duplicates(self):
        order = []

        def run(h, folder, cancel):
            order.append(folder)
            if folder == "A":
                self.assertFalse(h.f.enqueue("A"))         # being worked on
                h.f.enqueue("B")
                self.assertFalse(h.f.enqueue("B"))         # already queued
                self.assertEqual(h.f.state("B"), "waiting")
                self.assertEqual(h.f.folders(), ["A", "B"])
            return Result()
        h = Harness(on_run=run)
        h.f.enqueue("A")
        self.assertEqual(order, ["A", "B"])

    def test_nothing_starts_by_itself(self):
        h = Harness()
        self.assertEqual(h.runs, [])                       # only enqueue() starts anything
        self.assertEqual(h.f.folders(), [])

    def test_a_pass_that_raises_does_not_stop_the_next_one(self):
        def run(h, folder, cancel):
            if folder == "A":
                raise RuntimeError("boom")
            return Result()
        h = Harness(on_run=run)
        h.f.enqueue("A")
        h.f.enqueue("B")
        self.assertEqual(h.runs, ["A", "B"])
        self.assertEqual(h.f.state(), "")

    def test_a_busy_check_that_fails_means_busy(self):
        calls = []
        f = sf.SpeakerFollowUp(is_busy=lambda: 1 / 0, run=lambda *a: calls.append(1) or Result(),
                               spawn=lambda fn: fn(), poll_s=0.0,
                               wait=lambda s: f.shutdown())     # ends the wait: the app is quitting
        f.enqueue("A")
        self.assertEqual(calls, [])                        # never ran


class TestSettling(unittest.TestCase):
    """After the computer was busy the pass lets it stay quiet for a few looks first, so a
    person who dictates every few seconds does not burn the retries on starts that are
    stopped again at once."""

    def test_a_computer_that_is_free_at_once_means_no_waiting_at_all(self):
        h = Harness(settle=3)
        h.f.enqueue("A")
        self.assertEqual((h.polls, h.runs), (0, ["A"]))

    def test_after_being_busy_it_waits_for_a_quiet_stretch(self):
        h = Harness(busy_for=2, settle=3)
        h.f.enqueue("A")
        self.assertEqual(h.polls, 5)                       # two looks busy, then three quiet ones
        self.assertEqual(h.runs, ["A"])
        self.assertEqual(sf.SETTLE_POLLS * sf.POLL_S, 15.0)

    def test_the_computer_getting_busy_again_starts_the_quiet_stretch_over(self):
        looks = iter([True, False, True] + [False] * 20)
        seen = []
        f = sf.SpeakerFollowUp(is_busy=lambda: seen.append(1) or next(looks), run=lambda *a: Result(),
                               spawn=lambda fn: fn(), poll_s=0.0, wait=lambda s: None, settle_polls=2)
        runs = []
        f._run = lambda folder, c, st: runs.append(len(seen)) or Result()
        f.enqueue("A")
        self.assertEqual(runs, [6])                        # busy, quiet, busy again, then three quiet looks

    def test_a_pass_that_was_interrupted_settles_before_it_tries_again(self):
        state = {"n": 0}

        def run(h, folder, cancel):
            state["n"] += 1
            if state["n"] == 1:
                h.busy = True
                cancel()
                return Result("cancelled")
            return Result("done")
        h = Harness(busy_for=1, on_run=run, settle=2)
        h.busy, h.polls = False, 0
        h.f.enqueue("A")
        self.assertEqual(h.runs, ["A", "A"])
        self.assertEqual(h.polls, 3)                       # one busy look, then two quiet ones


class TestInterruptions(unittest.TestCase):
    def test_a_recording_that_starts_mid_pass_stops_it_at_once_and_it_runs_again_later(self):
        state = {"n": 0}

        def run(h, folder, cancel):
            state["n"] += 1
            if state["n"] == 1:
                h.busy = True                              # a recording starts
                self.assertTrue(cancel())                  # the pass is told at its next look
                return Result("cancelled")
            return Result("done")
        h = Harness(busy_for=1, on_run=run)
        h.busy = False                                     # free at the start
        h.polls = 0
        h.f.enqueue("A")
        self.assertEqual(h.runs, ["A", "A"])
        self.assertEqual(h.f.retries("A"), 1)
        self.assertEqual(h.f.state("A"), "")
        self.assertGreaterEqual(h.polls, 1)                # it waited for the computer before the retry

    def test_at_most_five_retries_then_the_meeting_is_left_alone(self):
        def run(h, folder, cancel):
            h.busy = True
            cancel()                                       # interrupted every time
            return Result("cancelled")
        h = Harness(on_run=run)
        h._wait = lambda s: setattr(h, "busy", False)      # the interruption always passes
        h.f._wait = h._wait
        h.f.enqueue("A")
        self.assertEqual(len(h.runs), 6)                   # the first run + five retries
        self.assertEqual(h.f.retries("A"), 6)
        self.assertEqual(h.f.state("A"), "")
        self.assertEqual(h.f.folders(), [])                # not queued again

    def test_the_bound_is_per_meeting(self):
        runs = {"A": 0, "B": 0}

        def run(h, folder, cancel):
            runs[folder] += 1
            if folder == "A":
                h.busy = True
                cancel()
                return Result("cancelled")
            return Result("done")
        h = Harness(on_run=run, max_retries=2)
        h.f._wait = lambda s: setattr(h, "busy", False)
        h.f.enqueue("A")
        h.f.enqueue("B")
        self.assertEqual(runs, {"A": 3, "B": 1})

    def test_a_pass_that_ends_with_a_reason_is_not_retried(self):
        for state in ("failed", "nothing", "concluded", "done"):
            h = Harness(on_run=lambda h, f, c, s=state: Result(s))
            h.f.enqueue("A")
            self.assertEqual(h.runs, ["A"], state)

    def test_a_cancel_that_was_not_the_computer_getting_busy_is_not_retried(self):
        h = Harness(on_run=lambda h, f, c: Result("cancelled"))
        h.f.enqueue("A")
        self.assertEqual(h.runs, ["A"])
        self.assertEqual(h.f.retries("A"), 0)


class TestCancelAndQuit(unittest.TestCase):
    def test_cancel_while_it_works_ends_it_for_good(self):
        def run(h, folder, cancel):
            self.assertFalse(cancel())
            self.assertTrue(h.f.cancel("A"))
            self.assertTrue(cancel())                      # told at the next look
            return Result("cancelled")
        h = Harness(on_run=run)
        h.f.enqueue("A")
        self.assertEqual(h.runs, ["A"])                    # no retry
        self.assertEqual(h.f.retries("A"), 0)
        self.assertEqual(h.f.state("A"), "")

    def test_cancel_while_it_waits_for_the_computer_drops_it(self):
        h = Harness(busy_for=99)
        orig = h._wait

        def wait(seconds):
            h.f.cancel("A")                                # the user presses Cancel on the banner
            orig(seconds)
        h.f._wait = wait
        h.f.enqueue("A")
        self.assertEqual(h.runs, [])
        self.assertEqual(h.f.state("A"), "")

    def test_cancel_drops_a_queued_meeting_and_leaves_the_others(self):
        order = []

        def run(h, folder, cancel):
            order.append(folder)
            if folder == "A":
                h.f.enqueue("B")
                h.f.enqueue("C")
                self.assertTrue(h.f.cancel("B"))
                self.assertFalse(h.f.cancel("nothing here"))
            return Result()
        h = Harness(on_run=run)
        h.f.enqueue("A")
        self.assertEqual(order, ["A", "C"])

    def test_cancel_everything(self):
        def run(h, folder, cancel):
            h.f.enqueue("B")
            self.assertTrue(h.f.cancel())
            return Result("cancelled")
        h = Harness(on_run=run)
        h.f.enqueue("A")
        self.assertEqual(h.runs, ["A"])
        self.assertEqual(h.f.state(), "")

    def test_quitting_stops_the_pass_and_forgets_the_queue(self):
        def run(h, folder, cancel):
            h.f.enqueue("B")
            h.f.shutdown()
            self.assertTrue(cancel())
            return Result("cancelled")
        h = Harness(on_run=run)
        h.f.enqueue("A")
        self.assertEqual(h.runs, ["A"])                    # no retry, B never started
        self.assertEqual(h.f.folders(), [])
        self.assertFalse(h.f.enqueue("C"))                 # nothing starts once the app is quitting
        self.assertEqual(h.runs, ["A"])

    def test_quitting_wakes_a_waiting_pass(self):
        h = Harness(busy_for=10 ** 6)
        h.f._wait = lambda s: h.f.shutdown()
        h.f.enqueue("A")
        self.assertEqual(h.runs, [])


class TestReviewHoles(unittest.TestCase):
    """What a second look found: a fresh job gets a fresh retry count, and a caller that cancelled
    can wait until the pass has really let go."""

    def test_a_meeting_that_used_up_its_retries_gets_a_fresh_count_when_queued_again(self):
        def run(h, folder, cancel):
            h.busy = True
            cancel()                                       # interrupted every time
            return Result("cancelled")
        h = Harness(on_run=run, max_retries=2)
        h.f._wait = lambda s: setattr(h, "busy", False)
        h.f.enqueue("A")
        self.assertEqual((len(h.runs), h.f.retries("A")), (3, 3))        # gave up after 2 retries
        # The meeting is continued and stopped again: queued again by a new Stop.
        h.runs.clear()
        h.f.enqueue("A")
        self.assertEqual(len(h.runs), 3)                   # it gets its two retries again, not none
        self.assertEqual(h.f.retries("A"), 3)

    def test_a_retry_queued_by_the_pass_itself_keeps_counting(self):
        # (only a NEW enqueue resets: the interrupted pass puts itself back without it)
        def run(h, folder, cancel):
            h.busy = True
            cancel()
            return Result("cancelled")
        h = Harness(on_run=run, max_retries=2)
        h.f._wait = lambda s: setattr(h, "busy", False)
        h.f.enqueue("A")
        self.assertEqual(len(h.runs), 3)

    def test_wait_idle_is_true_at_once_when_nothing_is_there(self):
        h = Harness()
        self.assertTrue(h.f.wait_idle("A", timeout=0.0))

    def test_wait_idle_waits_for_a_cancelled_pass_to_really_end(self):
        started, release, ended = threading.Event(), threading.Event(), threading.Event()

        def run(folder, cancel, status):
            started.set()
            while not cancel():
                time.sleep(0.01)
            release.wait(5)                              # cleaning up after the cancel takes a moment
            ended.set()
            return Result("cancelled")
        f = sf.SpeakerFollowUp(is_busy=lambda: False, run=run, poll_s=0.01)
        f.enqueue("A")
        self.assertTrue(started.wait(5))
        self.assertTrue(f.cancel("A"))
        self.assertEqual(f.state("A"), "running")          # told, but not over yet
        self.assertFalse(f.wait_idle("A", timeout=0.1))    # a caller that waits sees that
        threading.Timer(0.1, release.set).start()
        self.assertTrue(f.wait_idle("A", timeout=5.0))
        self.assertTrue(ended.is_set())                    # it came back only after the pass had ended
        self.assertEqual(f.state("A"), "")

    def test_wait_idle_on_the_passs_own_thread_does_not_wait_for_itself(self):
        seen = {}

        def run(h, folder, cancel):
            t0 = time.monotonic()
            seen["idle"] = h.f.wait_idle(folder, timeout=30.0)
            seen["took"] = time.monotonic() - t0
            return Result()
        h = Harness(on_run=run)
        h.f.enqueue("A")
        self.assertFalse(seen["idle"])
        self.assertLess(seen["took"], 2.0)                 # not the 30 s timeout


def _worker_alive():
    return any(t.name == "gusan-speaker-pass" and t.is_alive() for t in threading.enumerate())


class TestRealThread(unittest.TestCase):
    """One run with a real worker thread: it exists only while there is work."""

    def test_the_worker_comes_and_goes_with_the_work(self):
        release, started, done = threading.Event(), threading.Event(), threading.Event()

        def run(folder, cancel, status):
            started.set()
            release.wait(5)
            done.set()
            return Result()
        f = sf.SpeakerFollowUp(is_busy=lambda: False, run=run, poll_s=0.01)
        self.assertFalse(_worker_alive())
        f.enqueue("A")
        self.assertTrue(started.wait(5))
        self.assertEqual(f.state("A"), "running")
        self.assertTrue(_worker_alive())
        f.enqueue("B")                                     # queued behind it
        self.assertEqual(f.state("B"), "waiting")
        release.set()
        end = time.time() + 5
        while time.time() < end and (f.state() or _worker_alive()):
            time.sleep(0.01)
        self.assertEqual(f.state(), "")
        self.assertFalse(_worker_alive())                  # nothing idles
        # and it can start again
        done.clear()
        f.enqueue("C")
        self.assertTrue(done.wait(5))

    def test_cancel_wakes_a_sleeping_poll_at_once(self):
        f = sf.SpeakerFollowUp(is_busy=lambda: True, run=lambda *a: Result(), poll_s=30.0)
        f.enqueue("A")
        time.sleep(0.05)
        self.assertEqual(f.state("A"), "waiting")
        t0 = time.time()
        f.cancel("A")
        end = time.time() + 5
        while time.time() < end and f.state():
            time.sleep(0.01)
        self.assertEqual(f.state(), "")
        self.assertLess(time.time() - t0, 3.0)             # not after the 30 s poll


if __name__ == "__main__":
    unittest.main()
