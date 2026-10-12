"""Gusan 2.0: model_manager.DownloadManager - one state map, dedupe, the state
machine, wait_ready, removal, telemetry and the friendly failure messages.

Pure: fake operations stand in for main.download_whisper_model / local_llm, so
nothing touches the network or the disk, and neither main nor PySide6 is
imported (the Qt face, ModelDownloads, is tested in tests_qt).
"""
import ast
import errno
import os
import sys
import threading
import time
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import hardware  # noqa: E402
import model_manager as mm  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
WAIT = 5.0     # generous ceiling for the threaded tests; they finish in ms


class FakeOps:
    """Disk = a set of ids. download() can block on a gate and fail on demand."""

    def __init__(self, on_disk=(), progress=((10, 10, 100), (50, 50, 100), (100, 100, 100)),
                 fail=None, incomplete=False, remove_error=None, gate=None):
        self.on_disk = set(on_disk)
        self.progress = progress
        self.fail = fail
        self.incomplete = incomplete
        self.remove_error = remove_error
        self.gate = gate
        self.started = threading.Event()
        self.calls = []
        self.lock = threading.Lock()

    def normalize(self, name):
        return hardware.LEGACY_LOCAL_AI_IDS.get(name, name)

    def downloaded(self, kind, name):
        return name in self.on_disk

    def download(self, kind, name, on_progress):
        with self.lock:
            self.calls.append(("download", kind, name))
        self.started.set()
        if self.gate is not None:
            assert self.gate.wait(WAIT), "gate never opened"
        for p in self.progress:
            on_progress(*p)
        if self.fail is not None:
            raise self.fail
        if not self.incomplete:
            self.on_disk.add(name)

    def remove(self, kind, name):
        self.calls.append(("remove", kind, name))
        if self.remove_error is not None:
            raise self.remove_error
        self.on_disk.discard(name)

    def downloads(self):
        return [c for c in self.calls if c[0] == "download"]


def sync_spawn(fn, *args):
    fn(*args)


def thread_spawn(fn, *args):
    t = threading.Thread(target=fn, args=args, daemon=True)
    t.start()


class Harness:
    def __init__(self, ops=None, spawn=sync_spawn, clock=None):
        self.ops = ops or FakeOps()
        self.events = []
        self.tracked = []
        kw = {"clock": clock} if clock else {}
        self.m = mm.DownloadManager(ops=self.ops, track=lambda e, p: self.tracked.append((e, p)),
                                    notify=lambda *e: self.events.append(e), spawn=spawn, **kw)

    def of(self, kind):
        return [e for e in self.events if e[0] == kind]

    def track_names(self):
        return [e for e, _ in self.tracked]


class TestScan(unittest.TestCase):
    def test_state_is_missing_before_a_scan(self):
        h = Harness(FakeOps(on_disk={"small"}))
        self.assertFalse(h.m.is_scanned())
        self.assertEqual(h.m.state("small"), mm.MISSING)

    def test_scan_reads_the_disk(self):
        h = Harness(FakeOps(on_disk={"small", "qwen_tiny"}))
        states = h.m.scan_now()
        self.assertTrue(h.m.is_scanned())
        self.assertEqual(states["small"], mm.READY)
        self.assertEqual(states["base"], mm.MISSING)
        self.assertEqual(states["qwen_tiny"], mm.READY)
        self.assertEqual(set(states), set(mm.ALL_IDS))
        self.assertIn(("state", "small", mm.READY), h.events)
        self.assertEqual(h.events[-1], ("scanned",))
        self.assertEqual(h.m.ready(), ["small", "qwen_tiny"])
        self.assertEqual(h.m.ready("whisper"), ["small"])
        self.assertEqual(h.m.ready("llm"), ["qwen_tiny"])

    def test_rescan_only_reports_changes(self):
        h = Harness(FakeOps(on_disk={"small"}))
        h.m.scan_now()
        h.events.clear()
        h.ops.on_disk.add("base")
        h.m.scan_now()
        self.assertEqual(h.of("state"), [("state", "base", mm.READY)])

    def test_scan_runs_on_the_spawner(self):
        seen = []
        h = Harness(FakeOps(), spawn=lambda fn, *a: seen.append(fn))
        h.m.scan()
        self.assertEqual(seen, [h.m.scan_now])
        self.assertFalse(h.m.is_scanned())

    def test_scan_leaves_a_download_alone(self):
        gate = threading.Event()
        h = Harness(FakeOps(gate=gate), spawn=thread_spawn)
        h.m.start("base")
        self.assertTrue(h.ops.started.wait(WAIT))
        h.m.scan_now()
        self.assertEqual(h.m.state("base"), mm.DOWNLOADING)
        gate.set()
        self.assertTrue(h.m.wait_ready("base", WAIT))

    def test_scan_never_undoes_a_newer_change(self):
        h = Harness()
        m = h.m

        class RacingOps(FakeOps):
            raced = False

            def downloaded(self, kind, name):
                if name == "base" and not self.raced:
                    # A download finishes while the scan is still looking.
                    self.raced = True
                    with m._lock:
                        m._set("base", mm.READY)
                    return False
                return super().downloaded(kind, name)

        m._ops = RacingOps()
        m.scan_now()
        self.assertEqual(m.state("base"), mm.READY)

    def test_failed_stays_failed_until_found(self):
        h = Harness(FakeOps(fail=ConnectionError("down")))
        h.m.start("base")
        h.m.scan_now()
        self.assertEqual(h.m.state("base"), mm.FAILED)
        self.assertEqual(h.m.error("base"), mm.MSG_NETWORK)
        h.ops.on_disk.add("base")
        h.m.scan_now()
        self.assertEqual(h.m.state("base"), mm.READY)
        self.assertEqual(h.m.error("base"), "")


class TestDownload(unittest.TestCase):
    def test_happy_path(self):
        h = Harness()
        self.assertTrue(h.m.start("base"))
        self.assertEqual(h.m.state("base"), mm.READY)
        self.assertEqual(h.ops.downloads(), [("download", "whisper", "base")])
        kinds = [e[0] for e in h.events]
        self.assertEqual(kinds[0], "state")
        self.assertEqual(h.events[0], ("state", "base", mm.DOWNLOADING))
        self.assertEqual(h.events[-2:], [("state", "base", mm.READY), ("finished", "base", mm.READY, "")])
        self.assertIn(("progress", "base", 10, 10, 100), h.events)
        self.assertEqual(h.m.progress_of("base"), (100, 100, 100))
        self.assertEqual(h.tracked, [("model_download_started", {"kind": "whisper", "model": "base"}),
                                     ("model_download_completed", {"kind": "whisper", "model": "base"})])
        self.assertEqual(h.m.downloading(), [])

    def test_local_ai_models_and_legacy_ids(self):
        h = Harness()
        self.assertTrue(h.m.start("aibuben_tiny"))          # 1.9.x id
        self.assertEqual(h.ops.downloads(), [("download", "llm", "qwen_tiny")])
        self.assertEqual(h.m.state("qwen_tiny"), mm.READY)
        self.assertEqual(h.m.state("aibuben_tiny"), mm.READY)
        self.assertEqual(h.tracked[0], ("model_download_started", {"kind": "llm", "model": "qwen_tiny"}))
        self.assertEqual(h.m.kind("gemma_2b"), "llm")
        self.assertEqual(h.m.kind("large-v3"), "whisper")
        self.assertIsNone(h.m.kind("gpt-9"))

    def test_dedupe_one_download_per_model(self):
        gate = threading.Event()
        h = Harness(FakeOps(gate=gate), spawn=thread_spawn)
        self.assertTrue(h.m.start("small"))
        self.assertTrue(h.ops.started.wait(WAIT))
        self.assertTrue(h.m.start("small"))                 # already running: no second one
        self.assertTrue(h.m.start("small"))
        self.assertEqual(h.m.downloading(), ["small"])
        gate.set()
        self.assertTrue(h.m.wait_ready("small", WAIT))
        self.assertEqual(len(h.ops.downloads()), 1)
        self.assertEqual(h.track_names().count("model_download_started"), 1)
        self.assertEqual(len(h.of("finished")), 1)

    def test_parallel_downloads_of_different_models(self):
        gate = threading.Event()
        h = Harness(FakeOps(gate=gate), spawn=thread_spawn)
        h.m.start("small")
        h.m.start("qwen_tiny")
        self.assertEqual(sorted(h.m.downloading()), ["qwen_tiny", "small"])
        gate.set()
        self.assertTrue(h.m.wait_ready("small", WAIT))
        self.assertTrue(h.m.wait_ready("qwen_tiny", WAIT))

    def test_refusals(self):
        h = Harness(FakeOps(on_disk={"small"}))
        self.assertFalse(h.m.start("gpt-9"))                 # unknown id
        h.m.scan_now()
        self.assertFalse(h.m.start("small"))                 # already ready
        self.assertEqual(h.ops.downloads(), [])

    def test_found_on_disk_before_the_scan(self):
        # start() before the first scan: the worker finds it and finishes at once,
        # without a download or download telemetry.
        h = Harness(FakeOps(on_disk={"base"}))
        self.assertTrue(h.m.start("base"))
        self.assertEqual(h.m.state("base"), mm.READY)
        self.assertEqual(h.ops.downloads(), [])
        self.assertEqual(h.tracked, [])
        self.assertEqual(h.of("finished"), [("finished", "base", mm.READY, "")])

    def test_failure_then_retry(self):
        h = Harness(FakeOps(fail=ConnectionError("Max retries exceeded")))
        self.assertTrue(h.m.start("base"))
        self.assertEqual(h.m.state("base"), mm.FAILED)
        self.assertEqual(h.m.error("base"), mm.MSG_NETWORK)
        self.assertEqual(h.of("finished"), [("finished", "base", mm.FAILED, mm.MSG_NETWORK)])
        self.assertEqual(h.tracked[-1], ("model_download_failed",
                                         {"kind": "whisper", "model": "base", "reason": "network"}))
        h.ops.fail = None
        self.assertTrue(h.m.start("base"))                   # failed -> can retry
        self.assertEqual(h.m.state("base"), mm.READY)
        self.assertEqual(h.m.error("base"), "")

    def test_incomplete_download_fails(self):
        h = Harness(FakeOps(incomplete=True))
        h.m.start("small")
        self.assertEqual(h.m.state("small"), mm.FAILED)
        self.assertEqual(h.m.error("small"), mm.MSG_INCOMPLETE)
        self.assertEqual(h.tracked[-1][1]["reason"], "incomplete")

    def test_no_thread_means_failed_not_stuck(self):
        def broken(fn, *a):
            raise RuntimeError("can't start new thread")
        h = Harness(spawn=broken)
        self.assertFalse(h.m.start("base"))
        self.assertEqual(h.m.state("base"), mm.FAILED)
        self.assertEqual(h.of("finished"), [("finished", "base", mm.FAILED, mm.MSG_OTHER)])
        self.assertFalse(h.m.wait_ready("base", 0.01))

    def test_listener_and_analytics_errors_are_contained(self):
        def boom(*a):
            raise RuntimeError("listener died")
        m = mm.DownloadManager(ops=FakeOps(), track=boom, notify=boom, spawn=sync_spawn)
        self.assertTrue(m.start("base"))
        self.assertEqual(m.state("base"), mm.READY)

    def test_progress_is_throttled(self):
        now = [0.0]
        steps = [(0.00, 1), (0.05, 2), (0.20, 3), (0.30, 3), (0.81, 3), (0.82, 4), (0.83, 100)]

        class TimedOps(FakeOps):
            def download(self, kind, name, on_progress):
                for t, pct in steps:
                    now[0] = t
                    on_progress(pct, pct, 100)
                self.on_disk.add(name)

        h = Harness(TimedOps(), clock=lambda: now[0])
        h.m.start("base")
        self.assertEqual([e[2] for e in h.of("progress")], [1, 3, 3, 100])

    def test_unknown_percent_reads_as_zero(self):
        h = Harness(FakeOps(progress=((None, 0, 0),)))
        h.m.start("base")
        self.assertEqual(h.of("progress")[0], ("progress", "base", 0, 0, 0))

    def test_odd_progress_values_never_break_the_download(self):
        # local_llm doesn't guard its callback: a raise here would fail the download.
        h = Harness(FakeOps(progress=(("x", "y", object()), (float("inf"), -5, None), (100, 10, 10))))
        h.m.start("qwen_tiny")
        self.assertEqual(h.m.state("qwen_tiny"), mm.READY)
        self.assertEqual(h.of("progress")[0], ("progress", "qwen_tiny", 0, 0, 0))

    def test_big_byte_counts_survive(self):
        big = 3 * 1024 ** 3                                  # large-v3: more than 2**31
        h = Harness(FakeOps(progress=((50, big // 2, big), (100, big, big))))
        h.m.start("large-v3")
        self.assertEqual(h.m.progress_of("large-v3"), (100, big, big))


class TestWaitReady(unittest.TestCase):
    def test_waits_for_the_running_download(self):
        gate = threading.Event()
        h = Harness(FakeOps(gate=gate), spawn=thread_spawn)
        h.m.start("small")
        self.assertTrue(h.ops.started.wait(WAIT))
        result = []
        waiter = threading.Thread(target=lambda: result.append(h.m.wait_ready("small", WAIT)))
        waiter.start()
        time.sleep(0.05)
        self.assertEqual(result, [])                          # still waiting
        gate.set()
        waiter.join(WAIT)
        self.assertEqual(result, [True])

    def test_times_out_while_downloading(self):
        gate = threading.Event()
        h = Harness(FakeOps(gate=gate), spawn=thread_spawn)
        h.m.start("small")
        t0 = time.monotonic()
        self.assertFalse(h.m.wait_ready("small", 0.05))
        self.assertLess(time.monotonic() - t0, 2)
        gate.set()
        self.assertTrue(h.m.wait_ready("small", WAIT))

    def test_false_when_the_download_fails(self):
        gate = threading.Event()
        h = Harness(FakeOps(gate=gate, fail=OSError(errno.ENOSPC, "No space left on device")), spawn=thread_spawn)
        h.m.start("medium")
        gate.set()
        self.assertFalse(h.m.wait_ready("medium", WAIT))
        self.assertEqual(h.m.error("medium"), mm.MSG_DISK_FULL)

    def test_checks_the_disk_when_the_map_is_stale(self):
        h = Harness(FakeOps(on_disk={"small"}))
        self.assertTrue(h.m.wait_ready("small", 0))          # never scanned
        self.assertEqual(h.m.state("small"), mm.READY)
        self.assertIn(("state", "small", mm.READY), h.events)
        h.m.scan_now()
        h.ops.on_disk.add("base")                             # e.g. a silent download
        self.assertEqual(h.m.state("base"), mm.MISSING)
        self.assertTrue(h.m.wait_ready("base"))

    def test_never_starts_a_download(self):
        h = Harness()
        t0 = time.monotonic()
        self.assertFalse(h.m.wait_ready("base", WAIT))       # missing: returns at once
        self.assertLess(time.monotonic() - t0, 1)
        self.assertEqual(h.ops.downloads(), [])
        self.assertFalse(h.m.wait_ready("gpt-9"))


class TestFetch(unittest.TestCase):
    """fetch(): the blocking download for workers (file jobs, a dictation)."""

    def test_downloads_and_forwards_progress(self):
        h = Harness()
        seen = []
        self.assertEqual(h.m.fetch("small", on_progress=lambda *a: seen.append(a)), "small")
        self.assertEqual(h.m.state("small"), mm.READY)
        # Exactly what the cards got (throttled: 50% came within 0.1 s of 10%).
        self.assertEqual(seen, [e[2:] for e in h.of("progress")])
        self.assertEqual((seen[0], seen[-1]), ((10, 10, 100), (100, 100, 100)))
        self.assertEqual(h.ops.downloads(), [("download", "whisper", "small")])
        self.assertEqual(h.track_names(), ["model_download_started", "model_download_completed"])
        self.assertEqual(h.m._listeners, {})                  # the listener is gone afterwards

    def test_ready_or_on_disk_means_no_download(self):
        h = Harness(FakeOps(on_disk={"base", "qwen_tiny"}))
        self.assertEqual(h.m.fetch("base"), "base")           # never scanned: found on disk
        self.assertEqual(h.m.state("base"), mm.READY)
        self.assertEqual(h.m.fetch("base"), "base")           # ready in the map
        self.assertEqual(h.m.fetch("aibuben_tiny"), "qwen_tiny")   # 1.9.x id normalised
        self.assertEqual(h.ops.downloads(), [])
        self.assertEqual(h.tracked, [])

    def test_joins_the_running_download(self):
        gate = threading.Event()
        h = Harness(FakeOps(gate=gate), spawn=thread_spawn)
        self.assertTrue(h.m.start("medium"))                  # e.g. the Settings card
        self.assertTrue(h.ops.started.wait(WAIT))
        result, seen = [], []
        worker = threading.Thread(target=lambda: result.append(
            h.m.fetch("medium", on_progress=lambda *a: seen.append(a))))
        worker.start()
        time.sleep(0.05)
        self.assertEqual(result, [])                          # still waiting
        gate.set()
        worker.join(WAIT)
        self.assertEqual(result, ["medium"])
        self.assertEqual(len(h.ops.downloads()), 1)           # one download, shared
        self.assertIn((100, 100, 100), seen)

    def test_failure_raises_the_card_sentence(self):
        h = Harness(FakeOps(fail=OSError(errno.ENOSPC, "No space left on device")))
        with self.assertRaises(mm.DownloadError) as cm:
            h.m.fetch("large-v3")
        self.assertEqual(str(cm.exception), mm.MSG_DISK_FULL)
        self.assertEqual(h.m.state("large-v3"), mm.FAILED)
        self.assertEqual(h.m._listeners, {})

    def test_refusals(self):
        with self.assertRaises(mm.DownloadError) as cm:
            Harness().m.fetch("gpt-9")
        self.assertEqual(str(cm.exception), mm.MSG_UNAVAILABLE)

        jobs = []
        h = Harness(FakeOps(on_disk={"small"}), spawn=lambda fn, *a: jobs.append((fn, a)))
        h.m.scan_now()
        self.assertTrue(h.m.remove("small"))                  # still "ready" while it runs
        with self.assertRaises(mm.DownloadError) as cm:
            h.m.fetch("small")                                # ...but about to vanish
        self.assertEqual(str(cm.exception), mm.MSG_REMOVING)
        fn, a = jobs.pop()
        fn(*a)
        self.assertEqual(h.m.state("small"), mm.MISSING)

        def broken(fn, *a):
            raise RuntimeError("can't start new thread")
        with self.assertRaises(mm.DownloadError) as cm:
            Harness(spawn=broken).m.fetch("base")
        self.assertEqual(str(cm.exception), mm.MSG_OTHER)

    def test_timeout(self):
        gate = threading.Event()
        h = Harness(FakeOps(gate=gate), spawn=thread_spawn)
        t0 = time.monotonic()
        with self.assertRaises(mm.DownloadError) as cm:
            h.m.fetch("small", timeout=0.05)
        self.assertLess(time.monotonic() - t0, 2)
        self.assertEqual(str(cm.exception), mm.MSG_STILL_DOWNLOADING)
        gate.set()
        self.assertEqual(h.m.fetch("small", timeout=WAIT), "small")
        self.assertEqual(len(h.ops.downloads()), 1)

    def test_a_broken_listener_is_contained(self):
        def boom(*a):
            raise RuntimeError("listener died")
        h = Harness()
        self.assertEqual(h.m.fetch("tiny", on_progress=boom), "tiny")

    def test_matches_the_file_jobs_downloader_contract(self):
        # file_jobs calls downloader(model, on_progress=cb) and catches Exception.
        h = Harness()
        downloader = h.m.fetch
        downloader("base", on_progress=lambda pct, got=0, total=0: None)
        self.assertTrue(issubclass(mm.DownloadError, Exception))


class TestRemove(unittest.TestCase):
    def test_remove(self):
        h = Harness(FakeOps(on_disk={"small"}))
        h.m.scan_now()
        h.events.clear()
        self.assertTrue(h.m.remove("small"))
        self.assertEqual(h.m.state("small"), mm.MISSING)
        self.assertEqual(h.ops.calls[-1], ("remove", "whisper", "small"))
        self.assertEqual(h.events, [("state", "small", mm.MISSING), ("removed", "small", True, "")])
        self.assertEqual(h.tracked, [("model_removed", {"kind": "whisper", "model": "small"})])
        self.assertFalse(h.m.is_removing("small"))

    def test_remove_runs_on_a_worker_and_is_not_a_state(self):
        jobs = []
        h = Harness(FakeOps(on_disk={"small"}), spawn=lambda fn, *a: jobs.append((fn, a)))
        h.m.scan_now()
        self.assertTrue(h.m.remove("small"))
        self.assertTrue(h.m.is_removing("small"))
        self.assertEqual(h.m.state("small"), mm.READY)        # still ready until it is gone
        self.assertFalse(h.m.remove("small"))                 # already being removed
        self.assertFalse(h.m.start("small"))                  # no download meanwhile
        fn, a = jobs.pop()
        fn(*a)
        self.assertEqual(h.m.state("small"), mm.MISSING)

    def test_refused_while_downloading(self):
        gate = threading.Event()
        h = Harness(FakeOps(gate=gate), spawn=thread_spawn)
        h.m.start("small")
        self.assertFalse(h.m.remove("small"))
        gate.set()
        self.assertTrue(h.m.wait_ready("small", WAIT))
        self.assertNotIn(("remove", "whisper", "small"), h.ops.calls)

    def test_busy_local_ai_model(self):
        class LocalLLMError(RuntimeError):
            pass
        h = Harness(FakeOps(on_disk={"qwen_3b"},
                            remove_error=LocalLLMError("The model is busy right now - try again when it finishes.")))
        h.m.scan_now()
        self.assertTrue(h.m.remove("qwen_3b"))
        self.assertEqual(h.m.state("qwen_3b"), mm.READY)
        self.assertEqual(h.of("removed"), [("removed", "qwen_3b", False, mm.MSG_IN_USE)])
        self.assertNotIn("model_removed", h.track_names())

    def test_files_left_behind(self):
        class StubbornOps(FakeOps):
            def remove(self, kind, name):
                self.calls.append(("remove", kind, name))     # deletes nothing
        h = Harness(StubbornOps(on_disk={"base"}))
        h.m.scan_now()
        h.m.remove("base")
        self.assertEqual(h.m.state("base"), mm.READY)
        self.assertEqual(h.of("removed"), [("removed", "base", False, mm.MSG_REMOVE_FAILED)])

    def test_nothing_to_remove_is_not_tracked(self):
        # main.remove_whisper_model / local_llm.remove_model return False when
        # nothing was there: the card still ends "missing", no model_removed.
        class EmptyOps(FakeOps):
            def remove(self, kind, name):
                super().remove(kind, name)
                return False
        h = Harness(EmptyOps())
        self.assertTrue(h.m.remove("base"))
        self.assertEqual(h.of("removed"), [("removed", "base", True, "")])
        self.assertEqual(h.m.state("base"), mm.MISSING)
        self.assertEqual(h.tracked, [])

    def test_unknown(self):
        self.assertFalse(Harness().m.remove("gpt-9"))


class TestMessages(unittest.TestCase):
    def test_failure_messages(self):
        class DiskFull(OSError):
            winerror = 112

        class ReadTimeout(Exception):
            pass

        class TransportError(Exception):          # httpx's base for network failures
            pass

        class ConnectError(TransportError):       # httpx: no "connection" in the name
            pass

        def http(code):
            e = RuntimeError("HTTP error")
            e.response = SimpleNamespace(status_code=code)
            return e

        cases = [
            (OSError(errno.ENOSPC, "No space left on device"), "disk_full"),
            (DiskFull("There is not enough space on the disk"), "disk_full"),
            (RuntimeError("[Errno 28] No space left on device"), "disk_full"),
            (PermissionError("access denied"), "permission"),
            (ConnectionError("reset"), "network"),
            (ReadTimeout("read"), "network"),
            (ConnectError("[WinError 10061] refused"), "network"),
            (RuntimeError("Failed to resolve: getaddrinfo failed"), "network"),
            (http(404), "unavailable"),
            (http(401), "unavailable"),
            (http(503), "server"),
            (http(429), "server"),
            (ValueError("weird"), "other"),
        ]
        for exc, reason in cases:
            with self.subTest(exc=exc):
                got_reason, msg = mm.failure_message(exc)
                self.assertEqual(got_reason, reason)
                self.assertTrue(msg)

    def test_remove_failure_messages(self):
        self.assertEqual(mm.remove_failure_message(PermissionError("in use")), mm.MSG_IN_USE)
        self.assertEqual(mm.remove_failure_message(RuntimeError("model is busy")), mm.MSG_IN_USE)
        self.assertEqual(mm.remove_failure_message(OSError("x")), mm.MSG_REMOVE_FAILED)

    def test_messages_are_short_and_say_gusan(self):
        msgs = [v for k, v in vars(mm).items() if k.startswith("MSG_")]
        self.assertGreaterEqual(len(msgs), 8)
        for msg in msgs:
            self.assertNotIn("Transcribe", msg)
            self.assertLessEqual(len(msg), 90, msg)


class TestRealOps(unittest.TestCase):
    """ModelOps wraps main's functions (never re-importing main) and local_llm."""

    def _fake_main(self, calls):
        mod = ModuleType("fake_main")
        mod.model_downloaded = lambda name: calls.append(("downloaded", name)) or name == "small"
        mod.download_whisper_model = lambda name, on_progress=None: calls.append(("download", name, on_progress))
        mod.remove_whisper_model = lambda name: calls.append(("remove", name)) or True
        return mod

    def test_prefers_the_running_main_module(self):
        calls = []
        running = self._fake_main(calls)
        with patch.dict(sys.modules, {"__main__": running}):
            sys.modules.pop("main", None)
            self.assertIs(mm._main_module(), running)          # never re-imports main.py
        second_copy = self._fake_main(calls)
        with patch.dict(sys.modules, {"main": second_copy, "__main__": running}):
            self.assertIs(mm._main_module(), running)
        # Under pytest / tools __main__ isn't the app: an imported main is used.
        with patch.dict(sys.modules, {"main": second_copy, "__main__": ModuleType("pytest_main")}):
            self.assertIs(mm._main_module(), second_copy)

    def test_whisper_calls(self):
        calls, order = [], []
        fake = self._fake_main(calls)
        ops = mm.ModelOps(unload_whisper=lambda name: order.append(("unload", name)))
        with patch.dict(sys.modules, {"main": fake}):
            self.assertTrue(ops.downloaded("whisper", "small"))
            self.assertFalse(ops.downloaded("whisper", "base"))
            cb = lambda *a: None  # noqa: E731
            ops.download("whisper", "base", cb)
            fake.remove_whisper_model = lambda name: order.append(("remove", name)) or True
            ops.remove("whisper", "small")
        self.assertIn(("download", "base", cb), calls)
        self.assertEqual(order, [("unload", "small"), ("remove", "small")])   # unload first

    def test_local_ai_calls(self):
        calls = []
        fake_llm = ModuleType("local_llm")
        fake_llm.model_downloaded = lambda name: calls.append(("downloaded", name)) or True
        fake_llm.download_model = lambda name, on_progress=None: calls.append(("download", name))
        fake_llm.remove_model = lambda name: calls.append(("remove", name)) or True
        unloads = []
        ops = mm.ModelOps(unload_whisper=unloads.append)
        with patch.dict(sys.modules, {"local_llm": fake_llm}):
            self.assertTrue(ops.downloaded("llm", "qwen_tiny"))
            ops.download("llm", "qwen_3b", None)
            ops.remove("llm", "qwen_7b")
        self.assertEqual(calls, [("downloaded", "qwen_tiny"), ("download", "qwen_3b"), ("remove", "qwen_7b")])
        self.assertEqual(unloads, [])                         # local_llm unloads itself
        self.assertEqual(ops.normalize("aibuben_gpu"), "qwen_7b")


class TestModuleShape(unittest.TestCase):
    def test_no_qt_at_import(self):
        tree = ast.parse((ROOT / "model_manager.py").read_text(encoding="utf-8"))
        for node in tree.body:                                # module level only
            if isinstance(node, ast.Import):
                self.assertFalse(any(a.name.startswith("PySide6") for a in node.names))
            if isinstance(node, ast.ImportFrom):
                self.assertFalse((node.module or "").startswith("PySide6"))
                self.assertNotEqual(node.module, "main")
            if isinstance(node, ast.Import):
                self.assertFalse(any(a.name in ("main", "local_llm") for a in node.names))

    def test_unknown_attribute(self):
        with self.assertRaises(AttributeError):
            mm.not_a_thing  # noqa: B018

    def test_telemetry_names_are_allow_listed(self):
        import telemetry
        for e in ("model_download_started", "model_download_completed", "model_download_failed", "model_removed"):
            self.assertIn(e, telemetry.ALLOWED_EVENTS)


if __name__ == "__main__":
    unittest.main()
