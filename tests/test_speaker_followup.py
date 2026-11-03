"""When the speaker pass after the notes runs (speaker_followup.py): only while the
computer is free, polled gently; stopped at once when a recording / dictation / file
job starts and tried again later - at most five times per meeting that got nowhere (a try that
moved the pass forward is no failure); ended for good by Cancel and by quitting; one meeting at a time, oldest first. The pass itself is a fake
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
        self.assertEqual(sf.POLL_S, 2.0)                   # a short wait, and a visible one

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
        self.assertEqual(sf.SETTLE_POLLS * sf.POLL_S, 6.0)

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


    def test_a_pass_interrupted_by_something_already_over_still_lets_the_computer_settle(self):
        state = {"n": 0}

        def run(h, folder, cancel):
            state["n"] += 1
            if state["n"] == 1:
                h.busy = True
                cancel()                                   # the pass saw the computer busy ...
                h.busy = False                             # ... and it is free again before the pass looks
                return Result("cancelled")
            return Result("done")
        h = Harness(on_run=run, settle=2)
        h.f.enqueue("A")
        self.assertEqual(h.runs, ["A", "A"])
        self.assertEqual(h.polls, 2)                       # two quiet looks before it starts again: it was busy


class TestInterruptions(unittest.TestCase):
    def test_a_recording_that_starts_mid_pass_stops_it_at_once_and_it_runs_again_later(self):
        state = {"n": 0}

        tries = []

        def run(h, folder, cancel):
            state["n"] += 1
            tries.append(h.f.retries(folder))
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
        self.assertEqual(tries, [0, 1])                    # the second try knows it is the second
        self.assertEqual(h.f.retries("A"), 0)              # (and nothing of it is kept once the job ended)
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
        self.assertEqual(h.f.retries("A"), 0)              # the job is over: nothing is kept of its count
        self.assertEqual(h.f.take_result("A")[1], "gave_up")
        self.assertEqual(h.f.state("A"), "")
        self.assertEqual(h.f.folders(), [])                # not queued again

    def _crawling(self, per_try, jobs=12, **kw):
        """A pass that makes ``per_try`` voice prints and is interrupted by a dictation after each of its
        first ``jobs`` tries (the voice prints it made are kept in ``status.resume``, as the real pass does)."""
        state = {"n": 0}

        def run(h, folder, cancel, status=None):
            state["n"] += 1
            if state["n"] <= jobs:
                box = h.f._resume[sf._key(folder)]
                box["prints"] = box.get("prints", []) + [None] * per_try
                h.busy = True
                cancel()
                return Result("cancelled")
            return Result("done")
        h = Harness(on_run=run, **kw)
        h.f._wait = lambda s: setattr(h, "busy", False)
        return h, state

    def test_an_interruption_after_a_try_that_made_progress_does_not_count(self):
        # a person who dictates every half minute: every try gets some prints done, none of them is a failure
        h, state = self._crawling(per_try=sf.MIN_PROGRESS, jobs=12)
        h.f.enqueue("A")
        self.assertEqual(state["n"], 13)                   # twelve interruptions, far past the five of a failing pass
        self.assertEqual(h.f.take_result("A")[1], "done")

    def test_an_interruption_after_a_try_that_made_next_to_nothing_counts(self):
        h, state = self._crawling(per_try=sf.MIN_PROGRESS - 1, jobs=12)
        h.f.enqueue("A")
        self.assertEqual(state["n"], 6)                    # the first try + five retries
        self.assertEqual(h.f.take_result("A")[1], "gave_up")

    def test_progress_the_pass_reports_counts_too(self):
        # (a pass that keeps nothing in resume but reports how far it got)
        state = {"n": 0}

        def run(h, folder, cancel):
            state["n"] += 1
            if state["n"] <= 9:
                h.f._progress(folder, "voices", 0, 100)
                h.f._progress(folder, "voices", 10, 100)
                h.busy = True
                cancel()
                return Result("cancelled")
            return Result("done")
        h = Harness(on_run=run)
        h.f._wait = lambda s: setattr(h, "busy", False)
        h.f.enqueue("A")
        self.assertEqual((state["n"], h.f.take_result("A")[1]), (10, "done"))

    def test_however_much_they_make_a_job_is_not_interrupted_for_ever(self):
        h, state = self._crawling(per_try=50, jobs=500)
        h.f.enqueue("A")
        self.assertEqual(state["n"], sf.MAX_ATTEMPTS + 1)
        self.assertEqual(h.f.take_result("A")[1], "gave_up")

    def test_a_pass_that_gave_up_keeps_its_voice_prints_for_the_next_identify_speakers(self):
        h, state = self._crawling(per_try=1, jobs=6)
        h.f.enqueue("A")
        self.assertEqual(h.f.take_result("A")[1], "gave_up")
        self.assertEqual(h.f.state("A"), "")
        self.assertEqual(len(h.f._resume[sf._key("A")]["prints"]), 6)           # six tries' worth, kept
        seen = []
        h.on_run = lambda hh, folder, cancel: seen.append(
            len(hh.f._resume[sf._key(folder)].get("prints", []))) or Result("done")
        h.f.enqueue("A", first=True)                      # the person presses Identify speakers
        self.assertEqual(seen, [6])                        # ... it goes on from them, it does not start over
        self.assertEqual(h.f._resume, {})                  # (and once it is done nothing is kept)

    def test_only_the_last_few_meetings_keep_their_voice_prints(self):
        h, _state = self._crawling(per_try=1, jobs=10 ** 6)
        for name in ("A", "B", "C", "D", "E"):
            h.f.enqueue(name)
        self.assertEqual(len(h.f._resume), sf.MAX_KEPT)
        self.assertEqual(set(h.f._resume), {sf._key(n) for n in ("C", "D", "E")})
        h.f.cancel("E")                                    # a later Cancel clears what is kept
        self.assertNotIn(sf._key("E"), h.f._resume)

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
        self.assertEqual(len(h.runs), 3)                                 # gave up after 2 retries
        self.assertEqual(h.f.take_result("A")[1], "gave_up")
        # The meeting is continued and stopped again: queued again by a new Stop.
        h.runs.clear()
        h.f.enqueue("A")
        self.assertEqual(len(h.runs), 3)                   # it gets its two retries again, not none
        self.assertEqual(h.f.take_result("A")[1], "gave_up")

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

class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


class TestWhatItWaitsFor(unittest.TestCase):
    """The page says WHAT a queued pass is waiting for: the reason is_busy gave, another meeting's pass
    ahead of it, or the short stretch it lets the computer stay quiet."""

    def _reasons_while_waiting(self, *reasons, settle=0):
        seen = []
        looks = iter(reasons)
        h = Harness(busy_for=len(reasons) + 1, settle=settle)
        h.busy = next(looks)

        def wait(seconds):
            seen.append(h.f.wait_reason("A"))
            h.polls += 1
            nxt = next(looks, False)
            h.busy = nxt
        h.f._wait = wait
        h.f.enqueue("A")
        return seen, h

    def test_each_kind_of_work_is_named(self):
        seen, h = self._reasons_while_waiting("recording", "dictation", "file", "notes")
        self.assertEqual(seen, ["recording", "dictation", "file", "notes"])
        self.assertEqual(h.runs, ["A"])

    def test_a_busy_check_that_names_nothing_is_just_busy(self):
        seen, _h = self._reasons_while_waiting(True, True)
        self.assertEqual(seen, ["busy", "busy"])

    def test_a_check_that_fails_is_busy_too(self):
        f = sf.SpeakerFollowUp(is_busy=lambda: 1 / 0, run=lambda *a: Result(), spawn=lambda fn: fn(),
                               poll_s=0.0, wait=lambda s: seen.append(f.wait_reason("A")) or f.shutdown())
        seen = []
        f.enqueue("A")
        self.assertEqual(seen, ["busy"])

    def test_after_being_busy_it_says_it_is_letting_the_computer_stay_quiet(self):
        seen, _h = self._reasons_while_waiting("dictation", False, False, settle=2)
        self.assertEqual(seen, ["dictation", "settling", "settling"])

    def test_a_pass_queued_behind_another_meeting_says_so(self):
        got = {}

        def run(h, folder, cancel):
            if folder == "A":
                h.f.enqueue("B")
                h.f.enqueue("C")
                got["B"], got["C"] = h.f.wait_reason("B"), h.f.wait_reason("C")
                got["A"] = h.f.wait_reason("A")          # (running: it waits for nothing)
            return Result()
        h = Harness(on_run=run)
        h.f.enqueue("A")
        self.assertEqual(got, {"B": "queue", "C": "queue", "A": ""})

    def test_a_pass_only_just_queued_is_about_to_start(self):
        f = sf.SpeakerFollowUp(is_busy=lambda: False, run=lambda *a: Result(), spawn=lambda fn: None)
        f.enqueue("A")                                   # the worker has not started yet
        self.assertEqual(f.wait_reason("A"), "settling")
        f.enqueue("B")
        self.assertEqual(f.wait_reason("B"), "queue")    # ... and the one behind it waits for it
        self.assertEqual(f.wait_reason("nothing"), "")

    def test_a_meeting_that_is_not_waiting_waits_for_nothing(self):
        h = Harness()
        self.assertEqual(h.f.wait_reason("A"), "")


class TestSnapshot(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()

    def _running(self, body):
        """Run one pass whose body (called with the status, the follow-up) looks at the snapshots."""
        box = {}

        def run(folder, cancel, status):
            body(status, f, box)
            return Result()
        f = sf.SpeakerFollowUp(is_busy=lambda: False, run=run, spawn=lambda fn: fn(), poll_s=0.0,
                               wait=lambda s: None, clock=self.clock, settle_polls=0)
        f.enqueue("A")
        return box, f

    def test_nothing_queued_is_an_empty_snapshot(self):
        f = sf.SpeakerFollowUp(is_busy=lambda: False, run=lambda *a: Result(), spawn=lambda fn: fn())
        snap = f.snapshot("A")
        self.assertEqual(snap, {"state": "", "stopping": False, "waiting_for": "", "stage": "", "done": 0,
                                "total": 0, "fraction": None, "eta_s": None, "tries": 0})

    def test_a_waiting_pass_says_what_it_waits_for_and_has_no_progress(self):
        h = Harness(busy_for=2)
        h.busy = "dictation"
        seen = []
        orig = h._wait

        def wait(seconds):
            seen.append(h.f.snapshot("A"))
            orig(seconds)
        h.f._wait = wait
        h.f.enqueue("A")
        self.assertEqual(seen[0]["state"], "waiting")
        self.assertEqual(seen[0]["waiting_for"], "dictation")
        self.assertEqual((seen[0]["stage"], seen[0]["fraction"], seen[0]["eta_s"]), ("", None, None))

    def test_a_pass_that_has_not_reported_yet_is_starting(self):
        def body(status, f, box):
            box["snap"] = f.snapshot("A")
        box, _f = self._running(body)
        self.assertEqual((box["snap"]["state"], box["snap"]["stage"], box["snap"]["fraction"]),
                         ("running", "", None))

    def test_reading_the_sound_is_a_stage_without_a_percentage_until_the_count_is_known(self):
        def body(status, f, box):
            status.progress("reading", 0, 0)
            box["snap"] = f.snapshot("A")
        box, _f = self._running(body)
        self.assertEqual((box["snap"]["stage"], box["snap"]["fraction"], box["snap"]["eta_s"]), ("reading", None, None))

    def test_reading_the_sound_again_keeps_the_percentage_of_the_voice_prints_already_made(self):
        # (a try that goes on from an earlier one: the bar must not fall back to "getting ready" for a moment)
        def body(status, f, box):
            status.progress("reading", 51, 100)
            box["snap"] = f.snapshot("A")
        box, _f = self._running(body)
        snap = box["snap"]
        self.assertEqual((snap["stage"], snap["done"], snap["total"], snap["fraction"], snap["eta_s"]),
                         ("reading", 51, 100, 0.51, None))

    def test_the_download_of_the_speaker_model_is_its_own_stage_with_a_real_percentage(self):
        snaps = []

        def body(status, f, box):
            status.progress("download", 0, 0)
            snaps.append(f.snapshot("A"))                    # the size is not known yet
            status.progress("download", 10, 40)
            snaps.append(f.snapshot("A"))
        self._running(body)
        self.assertEqual([(s["stage"], s["fraction"]) for s in snaps], [("download", None), ("download", 0.25)])
        self.assertIsNone(snaps[1]["eta_s"])                 # no time left is guessed for a download

    def test_the_percentage_is_the_real_count_and_the_time_left_comes_from_this_tries_pace(self):
        snaps = []

        def body(status, f, box):
            status.progress("voices", 0, 100)
            snaps.append(f.snapshot("A"))                  # 0 of 100: no pace yet
            self.clock.advance(1.0)
            status.progress("voices", 3, 100)
            snaps.append(f.snapshot("A"))                  # three pieces but only a second: too early to say
            self.clock.advance(4.0)
            status.progress("voices", 10, 100)
            snaps.append(f.snapshot("A"))                  # 10 pieces in 5 s: 90 left, 45 s
            self.clock.advance(20.0)
            status.progress("voices", 50, 100)
            snaps.append(f.snapshot("A"))                  # 50 in 25 s: 50 left, 25 s
            status.progress("voices", 100, 100)
            snaps.append(f.snapshot("A"))
        self._running(body)
        self.assertEqual([s["fraction"] for s in snaps], [0.0, 0.03, 0.1, 0.5, 1.0])
        self.assertEqual([s["eta_s"] for s in snaps], [None, None, 45.0, 25.0, 0.0])
        self.assertEqual((snaps[3]["done"], snaps[3]["total"], snaps[3]["stage"]), (50, 100, "voices"))
        self.assertTrue(all(s["state"] == "running" for s in snaps))

    def test_a_pass_that_goes_on_from_earlier_work_measures_only_its_own_pace(self):
        snaps = []

        def body(status, f, box):
            status.progress("voices", 60, 100)             # 60 were made by an earlier try
            self.clock.advance(5.0)
            status.progress("voices", 70, 100)
            snaps.append(f.snapshot("A"))
        self._running(body)
        self.assertEqual(snaps[0]["eta_s"], 15.0)         # 10 pieces in 5 s: 30 left at that pace

    def test_a_count_that_goes_wrong_never_leaves_the_range(self):
        snaps = []

        def body(status, f, box):
            status.progress("voices", 150, 100)
            snaps.append(f.snapshot("A")["fraction"])
            status.progress("voices", -5, 100)
            snaps.append(f.snapshot("A")["fraction"])
            status.progress("voices", 1, 0)
            snaps.append(f.snapshot("A")["fraction"])
        self._running(body)
        self.assertEqual(snaps, [1.0, 0.0, None])

    def test_cancel_says_stopping_until_the_worker_has_ended(self):
        snaps = []

        def body(status, f, box):
            status.progress("voices", 1, 10)
            snaps.append(f.snapshot("A")["stopping"])
            f.cancel("A")
            snaps.append(f.snapshot("A")["stopping"])
            snaps.append(f.snapshot("other")["stopping"])
        _box, f = self._running(body)
        self.assertEqual(snaps, [False, True, False])
        self.assertFalse(f.snapshot("A")["stopping"])       # ... and nothing is stopping once it ended

    def test_the_tries_so_far_and_a_new_try_starting_from_nothing(self):
        snaps, state, busy = [], {"n": 0}, {"now": False}

        def run(folder, cancel, status):
            state["n"] += 1
            snaps.append(f.snapshot(folder))
            if state["n"] == 1:
                status.progress("voices", 5, 10)
                busy["now"] = True
                cancel()
                return Result("cancelled")
            return Result()
        f = sf.SpeakerFollowUp(is_busy=lambda: busy["now"], run=run, spawn=lambda fn: fn(), poll_s=0.0,
                               wait=lambda s: busy.update(now=False), settle_polls=0, clock=self.clock)
        f.enqueue("A")
        self.assertEqual([s["tries"] for s in snaps], [0, 1])
        self.assertEqual([s["stage"] for s in snaps], ["", ""])      # the second try has nothing reported yet
        self.assertEqual(snaps[1]["fraction"], None)

    def test_the_status_is_still_a_plain_text_callback_and_progress_updates_it(self):
        def body(status, f, box):
            status("Working…")
            box["text"] = f.status_text
            status.progress("voices", 34, 100)
            box["after"] = f.status_text
        box, _f = self._running(body)
        self.assertEqual(box["text"], "Working…")
        self.assertEqual(box["after"], "Telling the voices apart… 34%")


class TestHowItEnded(unittest.TestCase):
    def test_a_finished_pass_hands_over_its_result_once(self):
        h = Harness(on_run=lambda h, f, c: Result("done"))
        h.f.enqueue("A")
        result, how = h.f.take_result("A")
        self.assertEqual((result.state, how), ("done", "done"))
        self.assertEqual(h.f.take_result("A"), (None, ""))             # once

    def test_the_result_is_there_before_the_page_is_told_the_pass_ended(self):
        seen = []

        def on_change(folder):
            if not h.f.state(folder):
                seen.append(h.f.take_result(folder)[1])
        h = Harness()
        h.f._on_change = on_change
        h.f.enqueue("A")
        self.assertEqual(seen, ["done"])

    def test_cancel_while_it_runs_is_a_cancel_that_stays_cancelled(self):
        def run(h, folder, cancel):
            h.f.cancel(folder)
            return Result("cancelled")
        h = Harness(on_run=run)
        h.f.enqueue("A")
        result, how = h.f.take_result("A")
        self.assertEqual((result.state, how), ("cancelled", "cancelled"))

    def test_cancel_while_it_waits_hands_over_no_result_but_says_cancelled(self):
        h = Harness(busy_for=99)
        orig = h._wait
        h.f._wait = lambda s: (h.f.cancel("A"), orig(s))
        h.f.enqueue("A")
        self.assertEqual(h.f.take_result("A"), (None, "cancelled"))

    def test_cancel_of_a_queued_meeting_is_recorded_too(self):
        def run(h, folder, cancel):
            if folder == "A":
                h.f.enqueue("B")
                h.f.cancel("B")
            return Result()
        h = Harness(on_run=run)
        h.f.enqueue("A")
        self.assertEqual(h.f.take_result("B"), (None, "cancelled"))
        self.assertEqual(h.runs, ["A"])

    def test_a_pass_that_raised_ended_badly_and_says_so(self):
        def run(h, folder, cancel):
            raise RuntimeError("boom")
        h = Harness(on_run=run)
        h.f.enqueue("A")
        result, how = h.f.take_result("A")
        self.assertEqual((result.state, how), ("failed", "done"))

    def test_interrupted_too_often_is_gave_up_and_an_interruption_alone_is_no_result(self):
        between = []

        def run(h, folder, cancel):
            between.append(h.f.take_result(folder))
            h.busy = True
            cancel()
            return Result("cancelled")
        h = Harness(on_run=run, max_retries=2)
        h.f._wait = lambda s: setattr(h, "busy", False)
        h.f.enqueue("A")
        self.assertEqual(between, [(None, "")] * 3)                     # nothing to say between tries
        self.assertEqual(h.f.take_result("A")[1], "gave_up")

    def test_a_new_job_clears_the_old_result(self):
        f = sf.SpeakerFollowUp(is_busy=lambda: False, run=lambda *a: Result(), spawn=lambda fn: None)
        with f._lock:
            f._results[sf._key("A")] = (Result("failed"), "done")      # the last job ended and nobody took it
        f.enqueue("A")
        self.assertEqual(f.take_result("A"), (None, ""))              # the new job's result will come, not that one

    def test_nothing_is_kept_for_a_job_that_ended_but_the_voice_prints_of_one_that_gave_up(self):
        for how in ("done", "cancelled", "gave_up", "crashed"):
            def run(h, folder, cancel, how=how):
                h.f._resume.setdefault(sf._key(folder), {})["kept"] = 1
                if how == "crashed":
                    raise RuntimeError("boom")
                if how == "cancelled":
                    h.f.cancel(folder)
                    return Result("cancelled")
                if how == "gave_up":
                    h.busy = True
                    cancel()
                    return Result("cancelled")
                return Result()
            h = Harness(on_run=run, max_retries=1)
            h.f._wait = lambda s, h=h: setattr(h, "busy", False)
            h.f.enqueue("A")
            kept = {sf._key("A"): {"kept": 1}} if how == "gave_up" else {}      # (what it made stays for Identify speakers)
            self.assertEqual((h.f._retries, h.f._attempts, h.f._resume, h.f._prog, h.f._cooling),
                             ({}, {}, kept, {}, set()), how)
            self.assertEqual(h.f.state(), "", how)


class TestKeepingTheWorkDone(unittest.TestCase):
    def test_what_a_stopped_try_made_is_there_for_the_next_one(self):
        seen = []
        state = {"n": 0}

        def run(folder, cancel, status):
            state["n"] += 1
            seen.append(dict(status.resume))
            status.resume["prints"] = [state["n"]] * state["n"]
            if state["n"] == 1:
                busy["now"] = True
                cancel()
                return Result("cancelled")
            return Result()
        busy = {"now": False}
        f = sf.SpeakerFollowUp(is_busy=lambda: busy["now"], run=run, spawn=lambda fn: fn(), poll_s=0.0,
                               wait=lambda s: busy.update(now=False), settle_polls=0)
        f.enqueue("A")
        self.assertEqual(seen, [{}, {"prints": [1]}])                 # the second try saw what the first made

    def test_a_new_job_for_the_same_meeting_starts_with_nothing(self):
        seen = []

        def run(folder, cancel, status):
            seen.append(dict(status.resume))
            status.resume["prints"] = [1]
            return Result()
        f = sf.SpeakerFollowUp(is_busy=lambda: False, run=run, spawn=lambda fn: fn(), poll_s=0.0)
        f.enqueue("A")
        f.enqueue("A")
        self.assertEqual(seen, [{}, {}])

    def test_each_meeting_has_its_own(self):
        boxes = {}

        def run(folder, cancel, status):
            boxes[folder] = status.resume
            return Result()
        f = sf.SpeakerFollowUp(is_busy=lambda: False, run=run, spawn=lambda fn: fn(), poll_s=0.0)
        f.enqueue("A")
        f.enqueue("B")
        self.assertIsNot(boxes["A"], boxes["B"])


class TestTheOneTheyAskedFor(unittest.TestCase):
    def test_a_request_by_hand_goes_to_the_front(self):
        order = []

        def run(h, folder, cancel):
            order.append(folder)
            if folder == "A":
                h.f.enqueue("B")
                h.f.enqueue("C")
                h.f.enqueue("D", first=True)
                self.assertEqual(h.f.folders(), ["A", "D", "B", "C"])
                self.assertEqual(h.f.wait_reason("D"), "queue")
            return Result()
        h = Harness(on_run=run)
        h.f.enqueue("A")
        self.assertEqual(order, ["A", "D", "B", "C"])

    def test_asking_again_for_what_is_queued_or_running_changes_nothing(self):
        def run(h, folder, cancel):
            if folder == "A":
                h.f.enqueue("B")
                self.assertFalse(h.f.enqueue("B", first=True))        # queued: stays where it is
                self.assertFalse(h.f.enqueue("A", first=True))        # running
                self.assertEqual(h.f.folders(), ["A", "B"])
            return Result()
        h = Harness(on_run=run)
        h.f.enqueue("A")

    def test_nothing_starts_once_the_app_is_quitting_not_even_by_hand(self):
        h = Harness()
        h.f.shutdown()
        self.assertFalse(h.f.enqueue("A", first=True))


class TestNoStaleState(unittest.TestCase):
    """A banner that never goes away is the worst outcome: nothing may stay waiting or running for ever."""

    def test_a_worker_that_could_not_be_started_leaves_nothing_queued(self):
        changes = []

        def spawn(fn):
            raise RuntimeError("no threads left")
        f = sf.SpeakerFollowUp(is_busy=lambda: False, run=lambda *a: Result(), spawn=spawn,
                               on_change=changes.append)
        with self.assertLogs("transcribe", level="WARNING"):
            self.assertFalse(f.enqueue("A"))
        self.assertEqual((f.state("A"), f.state(), f.folders()), ("", "", []))
        self.assertEqual(changes, ["A", "A"])                          # queued, then taken out: the page is told twice
        # ... and it can be tried again
        f2 = sf.SpeakerFollowUp(is_busy=lambda: False, run=lambda *a: Result(), spawn=lambda fn: fn())
        self.assertTrue(f2.enqueue("A"))

    def test_a_worker_thread_that_is_gone_does_not_leave_a_pass_waiting_or_running(self):
        changes = []
        f = sf.SpeakerFollowUp(is_busy=lambda: False, run=lambda *a: Result(), spawn=lambda fn: None,
                               on_change=changes.append)
        f.enqueue("A")
        # the worker took A and then died without cleaning up (its thread id is not alive any more)
        with f._lock:
            f._queue.clear()
            f._current, f._running, f._worker_ident = "A", True, -123456
        with self.assertLogs("transcribe", level="WARNING") as cm:
            state = f.state("A")
        self.assertEqual(state, "")
        self.assertTrue(any("ended without finishing" in m for m in cm.output))
        self.assertEqual(f.state(), "")
        result, how = f.take_result("A")
        self.assertEqual((result.state, how), ("failed", "done"))      # the page hears that it ended, and not well
        self.assertEqual(changes[-1], "A")

    def test_a_dead_worker_does_not_strand_the_meetings_queued_behind_it(self):
        spawned = []
        f = sf.SpeakerFollowUp(is_busy=lambda: False, run=lambda *a: Result(), spawn=spawned.append)
        f.enqueue("A")
        f.enqueue("B")
        with f._lock:
            f._queue[:] = ["B"]
            f._current, f._running, f._worker_ident = "A", True, -123456
        with self.assertLogs("transcribe", level="WARNING"):
            self.assertEqual(f.state("A"), "")
        self.assertEqual(f.state("B"), "waiting")
        self.assertEqual(len(spawned), 2)                              # B got a new worker

    def test_a_live_worker_is_left_alone(self):
        gate, started = threading.Event(), threading.Event()

        def run(folder, cancel, status):
            started.set()
            gate.wait(5)
            return Result()
        f = sf.SpeakerFollowUp(is_busy=lambda: False, run=run, poll_s=0.01)
        f.enqueue("A")
        self.assertTrue(started.wait(5))
        for _ in range(5):
            self.assertEqual(f.state("A"), "running")
        gate.set()
        end = time.time() + 5
        while time.time() < end and f.state():
            time.sleep(0.01)
        self.assertEqual(f.state(), "")


if __name__ == "__main__":
    unittest.main()
