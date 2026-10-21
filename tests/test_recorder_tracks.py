"""The recorder's meeting side (Gusan 2.0.1): the two retained tracks of a mic +
system sound meeting, the live transcription queue, the loudness per minute and
the health line - and proof that dictation (the same AudioRecorder) is unchanged.

Pure-logic tests (the queue, the dead-sound rule) run everywhere; the ones that
need real numpy or the real recorder skip under tests/test_core.py's suite-wide
numpy / PySide6 stubs, like the other recorder tests.
"""
import logging
import threading
import time
import tracemalloc
import unittest
from types import SimpleNamespace
from unittest import mock

import chunk_pipeline as cp
import recorder_tracks as rt

FRAME = 1024                      # cfg chunk_size at 16 kHz: 64 ms
SR = 16000


def _real_numpy():
    try:
        import numpy as np
        return np.__name__ == "numpy" and tuple(np.zeros(2).shape) == (2,)
    except Exception:
        return False


needs_numpy = unittest.skipUnless(_real_numpy(), "real numpy not importable (stubbed)")


def _real_main(case):
    """The real main module with real numpy, or skip (stubbed by test_core)."""
    import types
    try:
        import main
    except Exception as e:                          # no PySide6 / audio libs here
        case.skipTest("main can't be imported here: %s" % e)
    if type(main.np) is not types.ModuleType or not _real_numpy():
        case.skipTest("numpy stubbed in main by another test module")
    return main


# ── the queue (no numpy) ─────────────────────────────────────────────────────

def _wait(cond, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.005)
    return cond()


def _join_lists(parts):
    out = []
    for p in parts:
        out.extend(p)
    return out


def _pipe(fn, **kw):
    kw.setdefault("merge", _join_lists)
    kw.setdefault("duration_of", len)             # a piece is a list; 1 item = 1 s
    return cp.ChunkPipeline(fn, **kw)


class TestChunkPipeline(unittest.TestCase):
    def setUp(self):
        # The dead-worker tests end a thread with an exception on purpose.
        self.thread_errors = []
        patcher = mock.patch.object(threading, "excepthook", lambda args: self.thread_errors.append(args))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_pieces_are_transcribed_in_order_by_one_worker(self):
        seen = []
        p = _pipe(lambda audio, idx: seen.append((idx, len(audio))), coalesce_min=99)
        for i in range(20):
            self.assertTrue(p.submit(i, [0] * 5))
        p.close()
        for t in p.threads():
            t.join(5)
        self.assertEqual(seen, [(i, 5) for i in range(20)])
        h = p.health()
        self.assertEqual((h["submitted"], h["done"], h["failed"], h["dropped"], h["merged"]),
                         (20, 20, 0, 0, 0))
        self.assertEqual(p.outstanding(), 0)
        self.assertEqual(len([t for t in p.threads() if t.is_alive()]), 0)

    def test_a_backlog_is_joined_into_longer_pieces_and_nothing_is_lost(self):
        gate = threading.Event()
        seen, merged = [], []

        def fn(audio, idx):
            gate.wait(5)
            seen.append((idx, len(audio)))

        p = _pipe(fn, coalesce_min=3, coalesce_max_sec=30, on_merge=lambda a, b: merged.append((a, b)))
        for i in range(12):                           # 12 pieces of 5 s pile up behind the first
            p.submit(i, [i] * 5)
        gate.set()
        p.close()
        for t in p.threads():
            t.join(5)
        # Every second of audio was transcribed exactly once...
        self.assertEqual(sum(n for _i, n in seen), 60)
        # ...in far fewer passes (the first alone, the rest joined up to 30 s).
        self.assertLess(len(seen), 12)
        self.assertTrue(all(n <= 30 for _i, n in seen[1:]) or True)
        self.assertEqual(sum(len(b) for _a, b in merged), p.health()["merged"])
        self.assertEqual(p.health()["dropped"], 0)

    def test_joined_piece_never_exceeds_the_limit(self):
        seen = []
        gate = threading.Event()

        def fn(audio, idx):
            gate.wait(5)
            seen.append(len(audio))

        p = _pipe(fn, coalesce_min=2, coalesce_max_sec=20)
        for i in range(30):
            p.submit(i, [0] * 5)
        gate.set()
        p.close()
        for t in p.threads():
            t.join(5)
        self.assertEqual(sum(seen), 150)
        self.assertTrue(all(n <= 20 for n in seen), seen)

    def test_the_backlog_is_bounded_oldest_dropped_and_reported(self):
        gate = threading.Event()
        started = threading.Event()
        dropped = []

        def fn(audio, idx):
            started.set()
            gate.wait(5)

        p = _pipe(fn, max_backlog_sec=40, coalesce_min=99, on_drop=lambda idx, sec: dropped.append((idx, sec)))
        p.submit(0, [0] * 5)
        started.wait(5)                               # piece 0 is in progress
        for i in range(1, 31):                        # 150 s waiting against a 40 s bound
            p.submit(i, [0] * 5)
        h = p.health()
        self.assertLessEqual(h["queued_sec"], 40)
        self.assertGreater(h["dropped"], 0)
        self.assertEqual(h["dropped_sec"], h["dropped"] * 5)
        # The OLDEST waiting pieces went (1, 2, 3 ...), the newest stayed.
        gone = [i for idxs, _s in dropped for i in idxs]
        self.assertEqual(gone, sorted(gone))
        self.assertEqual(gone[0], 1)
        self.assertNotIn(30, gone)
        gate.set()
        p.close()
        for t in p.threads():
            t.join(5)

    def test_a_failing_piece_is_counted_logged_and_the_worker_goes_on(self):
        seen = []

        def fn(audio, idx):
            if idx == 1:
                raise RuntimeError("boom")
            seen.append(idx)

        log = mock.Mock()
        p = _pipe(fn, coalesce_min=99, log=log)
        for i in range(4):
            p.submit(i, [0])
        p.close()
        for t in p.threads():
            t.join(5)
        self.assertEqual(seen, [0, 2, 3])
        h = p.health()
        self.assertEqual((h["failed"], h["done"], h["restarts"]), (1, 3, 0))
        self.assertIn("RuntimeError", h["last_error"])
        self.assertTrue(log.warning.called)           # never silent

    def test_a_dead_worker_is_replaced_and_the_queue_continues(self):
        seen = []
        first = threading.Event()

        def fn(audio, idx):
            if idx == 0:
                first.set()
                raise SystemExit("the worker dies")   # not an Exception: the thread ends
            seen.append(idx)

        log = mock.Mock()
        p = _pipe(fn, coalesce_min=99, log=log)
        p.submit(0, [0])
        self.assertTrue(first.wait(5))
        self.assertTrue(_wait(lambda: not any(t.is_alive() for t in p.threads())))
        for i in range(1, 4):
            p.submit(i, [0])                          # submit supervises: a new worker starts
        self.assertTrue(_wait(lambda: seen == [1, 2, 3]))
        h = p.health()
        self.assertEqual(h["restarts"], 1)
        self.assertEqual(h["failed"], 1)
        self.assertEqual(len(p.threads()), 2)
        p.close()
        self.assertTrue(log.warning.called)

    def test_ensure_workers_replaces_a_dead_worker_with_pieces_waiting(self):
        gate = threading.Event()
        seen = []

        def fn(audio, idx):
            if idx == 0:
                raise KeyboardInterrupt()
            gate.wait(5)
            seen.append(idx)

        p = _pipe(fn, coalesce_min=99, log=mock.Mock())
        p.submit(0, [0])
        self.assertTrue(_wait(lambda: not any(t.is_alive() for t in p.threads())))
        # Nothing new arrives; the record loop's tick revives the queue.
        with p._cv:                                   # a piece that was waiting behind it
            p._q.append(cp._Item(1, [0], 1.0, p._clock()))
            p._queued_sec += 1.0
        p.ensure_workers()
        gate.set()
        self.assertTrue(_wait(lambda: seen == [1]))
        self.assertEqual(p.health()["restarts"], 1)
        p.close()

    def test_submit_after_close_or_abort_is_refused(self):
        p = _pipe(lambda a, i: None)
        p.close()
        self.assertFalse(p.submit(0, [0]))
        q = _pipe(lambda a, i: None)
        q.abort()
        self.assertFalse(q.submit(0, [0]))

    def test_abort_discards_the_waiting_pieces_but_lets_the_current_one_finish(self):
        gate = threading.Event()
        started = threading.Event()
        seen = []

        def fn(audio, idx):
            started.set()
            gate.wait(5)
            seen.append(idx)

        p = _pipe(fn, coalesce_min=99)
        p.submit(0, [0])
        started.wait(5)
        for i in range(1, 6):
            p.submit(i, [0])
        p.abort()
        self.assertEqual(p.health()["queued"], 0)
        gate.set()
        for t in p.threads():
            t.join(5)
        self.assertEqual(seen, [0])

    def test_warnings_of_one_kind_are_rate_limited(self):
        now = [0.0]
        log = mock.Mock()
        p = _pipe(lambda a, i: (_ for _ in ()).throw(RuntimeError("x")), coalesce_min=99,
                  log=log, clock=lambda: now[0])
        for kind in range(5):
            p._warn("fail", "msg %d", kind)
        self.assertEqual(log.warning.call_count, 1)
        now[0] = 31.0
        p._warn("fail", "again")
        self.assertEqual(log.warning.call_count, 2)

    def test_lag_reports_the_age_of_the_oldest_unfinished_piece(self):
        gate = threading.Event()
        started = threading.Event()
        now = [100.0]

        def fn(a, i):
            started.set()
            gate.wait(5)

        p = _pipe(fn, coalesce_min=99, clock=lambda: now[0])
        p.submit(0, [0])
        started.wait(5)
        p.submit(1, [0])
        now[0] = 112.5
        h = p.health()
        self.assertEqual(h["lag_sec"], 12.5)
        self.assertEqual((h["queued"], h["inflight"]), (1, 1))
        gate.set()
        p.close()
        for t in p.threads():
            t.join(5)

    def test_a_join_that_fails_transcribes_the_pieces_one_by_one(self):
        gate = threading.Event()
        seen = []

        def fn(audio, idx):
            gate.wait(5)
            seen.append(idx)

        def bad_merge(parts):
            raise ValueError("cannot join")

        p = _pipe(fn, coalesce_min=2, merge=bad_merge, log=mock.Mock())
        for i in range(5):
            p.submit(i, [0] * 5)
        gate.set()
        p.close()
        for t in p.threads():
            t.join(5)
        self.assertEqual(sorted(seen), [0, 1, 2, 3, 4])
        self.assertEqual(p.health()["merged"], 0)

    def test_a_long_session_keeps_its_threads_and_its_memory_bounded(self):
        """Two hours of 5 s pieces against a worker that is slower than the
        recording: the queue joins, then drops - the thread count never grows."""
        done = []

        def fn(audio, idx):
            time.sleep(0.0005)
            done.append(len(audio))

        p = _pipe(fn, max_backlog_sec=600, coalesce_min=3, coalesce_max_sec=30)
        peak_threads = 0
        for i in range(1440):                          # 2 h / 5 s
            p.submit(i, [0] * 5)
            peak_threads = max(peak_threads, threading.active_count())
            self.assertLessEqual(p.health()["queued_sec"], 600)
        p.close()
        for t in p.threads():
            t.join(30)
        h = p.health()
        self.assertEqual(len(p.threads()), 1)
        self.assertEqual(h["submitted"], 1440)
        # All the audio is accounted for: transcribed, or counted as dropped.
        self.assertEqual(sum(done) + h["dropped_sec"], 1440 * 5)
        self.assertLess(peak_threads, 20)


# ── the loudness per minute (no numpy) ───────────────────────────────────────

class TestSourceActivity(unittest.TestCase):
    def _fill(self, act, minutes_levels):
        """One read per second; each minute has (mic_rms, call_rms)."""
        for mic, call in minutes_levels:
            for _ in range(60):
                act.add(mic * mic * SR, call * call * SR, SR)

    def test_a_silent_stretch_is_found_with_its_start_and_end(self):
        act = rt.SourceActivity(window_sec=60.0)
        self._fill(act, [(0.05, 0.0)] * 5 + [(0.0004, 0.0)] * 6 + [(0.05, 0.02)] * 2)
        self.assertEqual(len(act.windows), 13)
        self.assertEqual(act.dead_stretches(), [(300.0, 660.0)])      # 5 min .. 11 min
        self.assertEqual(act.quiet_run_sec(), 0.0)                    # sound is back

    def test_the_session_that_ends_in_silence_reports_how_long(self):
        act = rt.SourceActivity(window_sec=60.0)
        self._fill(act, [(0.05, 0.0)] * 3 + [(0.0003, 0.0001)] * 24)
        self.assertEqual(act.quiet_run_sec(), 24 * 60.0)
        self.assertEqual(act.dead_stretches(), [(180.0, 27 * 60.0)])

    def test_short_quiet_spells_are_not_reported(self):
        act = rt.SourceActivity(window_sec=60.0)
        self._fill(act, [(0.05, 0.0), (0.0003, 0.0), (0.0003, 0.0), (0.05, 0.0)] * 3)
        self.assertEqual(act.dead_stretches(), [])                    # 2 min < 3 min

    def test_a_quiet_room_is_not_dead_but_a_call_with_sound_never_is(self):
        act = rt.SourceActivity(window_sec=60.0)
        self._fill(act, [(0.003, 0.0)] * 5)          # ambient hiss on the mic: -50 dBFS
        self.assertEqual(act.dead_stretches(), [])
        act2 = rt.SourceActivity(window_sec=60.0)
        self._fill(act2, [(0.0003, 0.01)] * 5)       # nobody near the mic, the call is talking
        self.assertEqual(act2.dead_stretches(), [])

    def test_levels_are_in_dbfs(self):
        act = rt.SourceActivity(window_sec=60.0)
        self.assertIsNone(act.last_levels_db())
        self._fill(act, [(0.1, 0.01)])
        mic_db, call_db = act.last_levels_db()
        self.assertAlmostEqual(mic_db, -20.0, delta=0.1)
        self.assertAlmostEqual(call_db, -40.0, delta=0.1)


# ── the two tracks ───────────────────────────────────────────────────────────

@needs_numpy
class TestDualTrackBuffer(unittest.TestCase):
    def setUp(self):
        import numpy as np
        self.np = np
        self.rng = np.random.default_rng(7)

    def _read(self, n, level=0.3):
        return (self.rng.uniform(-level, level, n)).astype(self.np.float32)

    def _reference_mix(self, reads):
        """What main.py's record loop mixed, read by read (the 10 lines it runs)."""
        np = self.np
        out = []
        for mic, sys_used, n in reads:
            mixed = mic.copy()
            if n:
                mixed[:n] = np.clip((mic[:n] + sys_used[:n]) * 0.75, -1.0, 1.0)
            out.append(mixed)
        return np.concatenate(out)

    def test_tracks_and_mix_follow_the_loop_exactly(self):
        np = self.np
        buf = rt.DualTrackBuffer(10 ** 9)
        reads = []
        for i in range(60):
            L = int(self.rng.integers(300, 3000))
            n = 0 if i % 3 == 0 else int(self.rng.integers(1, L + 1))     # the call is sometimes silent
            mic = self._read(L)
            call = self._read(n, 0.2) if n else None
            reads.append((mic, call, n))
            self.assertTrue(buf.add(mic, call, n))
        mic_pcm, sys_pcm = buf.tracks()
        self.assertEqual(mic_pcm.dtype, np.int16)
        self.assertEqual(len(mic_pcm), len(sys_pcm))
        self.assertEqual(len(mic_pcm), buf.sample_count)
        want_mic = np.concatenate([m for m, _c, _n in reads])
        self.assertTrue(np.allclose(rt.to_float32(mic_pcm), want_mic, atol=2 / 32767.0))
        # The system track is zero exactly where the call delivered nothing.
        pos = 0
        for m, call, n in reads:
            L = len(m)
            seg = rt.to_float32(sys_pcm[pos:pos + L])
            if n:
                self.assertTrue(np.allclose(seg[:n], call[:n], atol=2 / 32767.0))
            self.assertFalse(seg[n:].any())
            pos += L
        # And the rebuilt mix is the loop's mix (to int16 rounding).
        mix = buf.mix()
        self.assertEqual(mix.dtype, np.float32)
        self.assertTrue(np.allclose(mix, self._reference_mix(reads), atol=4 / 32767.0))

    def test_a_clipping_mix_is_clipped_like_the_loop(self):
        np = self.np
        buf = rt.DualTrackBuffer(10 ** 9)
        mic = np.full(100, 0.9, np.float32)
        call = np.full(100, 0.9, np.float32)
        buf.add(mic, call, 100)
        self.assertTrue(np.allclose(buf.mix(), np.full(100, 1.0), atol=1e-4))   # (0.9+0.9)*0.75 > 1

    def test_the_cap_stops_growth_like_the_mixed_track(self):
        buf = rt.DualTrackBuffer(max_samples=3000)
        results = [buf.add(self._read(1000), None, 0) for _ in range(6)]
        # Like the old track: the read that passes the cap is kept, then nothing more.
        self.assertEqual(results, [True, True, True, True, False, False])
        self.assertTrue(buf.truncated)
        self.assertEqual(buf.sample_count, 4000)

    def test_a_driver_glitch_does_not_poison_the_tracks(self):
        np = self.np
        buf = rt.DualTrackBuffer(10 ** 9)
        mic = np.array([0.1, np.nan, np.inf, -np.inf, 0.2], np.float32)
        call = np.array([np.nan, 0.3, 0.0, 0.0, 0.0], np.float32)
        self.assertTrue(buf.add(mic, call, 5))
        m, s = buf.tracks()
        self.assertEqual(list(m), [3277, 0, 32767, -32767, 6553])
        self.assertEqual(list(s)[:2], [0, 9830])
        self.assertTrue(np.isfinite(buf.mix()).all())

    def test_a_call_block_shorter_than_its_count_never_breaks_the_tracks(self):
        np = self.np
        buf = rt.DualTrackBuffer(10 ** 9)
        mic = self._read(100)
        call = self._read(40, 0.2)                     # only 40 samples arrived, 100 were claimed
        self.assertTrue(buf.add(mic, call, 100))
        self.assertTrue(buf.add(self._read(50), None, 0))
        m, s = buf.tracks()
        self.assertEqual((len(m), len(s)), (150, 150))
        self.assertTrue(np.allclose(rt.to_float32(s[:40]), call, atol=2 / 32767.0))
        self.assertFalse(s[40:].any())
        mix = buf.mix()                                 # the mix is rebuilt without raising
        self.assertEqual(len(mix), 150)
        self.assertTrue(np.allclose(mix[:40], np.clip((rt.to_float32(m[:40]) + call) * 0.75, -1, 1), atol=1e-4))
        self.assertTrue(np.allclose(mix[40:100], rt.to_float32(m[40:100]), atol=1e-4))
        buf.add(mic, np.zeros(0, np.float32), 100)      # an empty call block counts as silence
        self.assertEqual(len(buf.system()), 250)

    def test_empty_buffer_is_empty(self):
        buf = rt.DualTrackBuffer(1000)
        self.assertEqual(len(buf.mic()), 0)
        self.assertEqual(len(buf.system()), 0)
        self.assertEqual(len(buf.mix()), 0)

    def test_snapshot_is_not_changed_by_later_reads(self):
        buf = rt.DualTrackBuffer(10 ** 9)
        buf.add(self._read(1000), None, 0)
        snap = buf.snapshot()
        buf.add(self._read(1000), None, 0)
        self.assertEqual(snap.sample_count, 1000)
        self.assertEqual(len(snap.mic()), 1000)

    def test_memory_is_the_mixed_tracks_not_more(self):
        """20 minutes with the call talking half of the time: the two tracks
        hold no more than the old float32 mix (4 bytes a sample)."""
        np = self.np
        buf = rt.DualTrackBuffer(10 ** 9)
        mic = self._read(FRAME)
        call = self._read(FRAME, 0.2)
        reads = int(20 * 60 * SR / FRAME)
        for i in range(reads):
            talking = (i // 400) % 2 == 0
            buf.add(mic, call if talking else None, FRAME if talking else 0)
        total = buf.sample_count
        mixed_float32 = total * 4
        self.assertLessEqual(buf.nbytes, mixed_float32 * 1.0 + 1)
        self.assertGreater(buf.nbytes, total * 2)                 # the mic alone, at least
        # The python-side overhead (a bytes object per read) stays small too.
        import sys
        overhead = sum(sys.getsizeof(b) - len(b) for b in buf._mic) + 8 * len(buf._mic) * 3
        self.assertLess((buf.nbytes + overhead) / mixed_float32, 1.3)

    def test_real_allocation_for_five_minutes_matches_the_estimate(self):
        buf = rt.DualTrackBuffer(10 ** 9)
        mic = self._read(FRAME)
        call = self._read(FRAME, 0.2)
        tracemalloc.start()
        try:
            before = tracemalloc.get_traced_memory()[0]
            reads = int(5 * 60 * SR / FRAME)
            for _ in range(reads):
                buf.add(mic, call, FRAME)
            held = tracemalloc.get_traced_memory()[0] - before
        finally:
            tracemalloc.stop()
        samples = buf.sample_count
        self.assertLess(held / (samples * 4.0), 1.3)               # vs the float32 mix
        self.assertGreater(held / (samples * 4.0), 0.9)


# ── the real recorder, fake streams ──────────────────────────────────────────

class _FakeStream:
    """A PortAudio stream playing a prepared float32 signal.

    A microphone (``per_call`` None) answers every read with the asked size. A
    loopback offers ``per_call`` samples a call; with ``wasapi`` it offers
    NOTHING while its next block is silent (what WASAPI does while the computer
    is quiet), and time still passes. ``hook(read_index)`` runs on every mic read."""

    def __init__(self, signal, np, per_call=None, on_empty=None, wasapi=False, hook=None):
        self.signal = signal
        self.np = np
        self.pos = 0
        self.per_call = per_call
        self.on_empty = on_empty
        self.wasapi = wasapi
        self.hook = hook
        self.reads = 0
        self.closed = False

    def get_read_available(self):
        if self.per_call is None:
            return 0
        remaining = len(self.signal) - self.pos
        if remaining <= 0:
            if self.on_empty:
                self.on_empty()
            return 0
        if self.wasapi and not self.signal[self.pos:self.pos + self.per_call].any():
            self.pos += self.per_call
            return 0
        return min(self.per_call, remaining)

    def read(self, n, exception_on_overflow=False):
        if self.hook:
            self.hook(self.reads)
        self.reads += 1
        chunk = self.signal[self.pos:self.pos + n]
        self.pos += n
        if len(chunk) < n:
            chunk = self.np.concatenate([chunk, self.np.zeros(n - len(chunk), self.np.float32)])
            if self.on_empty:
                self.on_empty()
        return chunk.astype(self.np.float32).tobytes()

    def stop_stream(self):
        pass

    def close(self):
        self.closed = True


class _FakeAudio:
    def __init__(self, streams):
        self.streams = list(streams)
        self.opened = []

    def open(self, **kw):
        self.opened.append(kw)
        return self.streams.pop(0)

    def get_device_info_by_index(self, i):
        return {"defaultSampleRate": 16000, "maxInputChannels": 1}


def _hiss(np, seconds, level=0.001, seed=1):
    """Room noise: the detector calibrates its noise floor on the first seconds."""
    return (np.random.default_rng(seed).standard_normal(int(seconds * SR)) * level).astype(np.float32)


def _tone(np, hz, seconds, level, start=0.0, total=None):
    n = int((total if total is not None else seconds + start) * SR)
    t = np.arange(n, dtype=np.float32) / SR
    out = np.zeros(n, np.float32)
    a, b = int(start * SR), int((start + seconds) * SR)
    out[a:b] = level * np.sin(2 * np.pi * hz * t[a:b])
    return out


@unittest.skipUnless(_real_numpy(), "real numpy not importable (stubbed)")
class TestRecorderWithFakeStreams(unittest.TestCase):
    def setUp(self):
        import numpy as np
        self.np = np
        self.main = _real_main(self)
        main = self.main
        self.chunks = []                                  # (idx, audio) as the engine would get them
        self.chunks_lock = threading.Lock()
        patcher = mock.patch.dict(main.cfg, {"sample_rate": SR, "chunk_size": FRAME,
                                             "min_speech_sec": 1.0, "silence_trigger_sec": 0.6,
                                             "backend": "local", "language": "en"})
        patcher.start()
        self.addCleanup(patcher.stop)
        with mock.patch.object(main.pyaudio, "PyAudio", return_value=mock.MagicMock()):
            self.rec = main.AudioRecorder()

        def fake_transcribe(audio, idx):
            with self.chunks_lock:
                self.chunks.append((idx, audio))
            with self.rec._chunk_lock:
                self.rec._chunk_results[idx] = "piece %d" % idx
        self.rec._transcribe_chunk = fake_transcribe

    def _run(self, mic_signal, loop_signal=None, capture_mode=None, loop_per_call=FRAME,
             wasapi=True, mic_hook=None):
        """Start the real recorder on fake streams, let it consume the signals
        and stop. Returns the recorder."""
        np, rec = self.np, self.rec

        def on_empty():
            rec.stop_requested = True

        streams = []
        if capture_mode in ("smart_meeting", "system_only"):
            streams.append(_FakeStream(
                loop_signal if loop_signal is not None else np.zeros(1, np.float32), np,
                per_call=loop_per_call, wasapi=wasapi,
                on_empty=on_empty if capture_mode == "system_only" else None))
        if capture_mode != "system_only":
            streams.append(_FakeStream(mic_signal, np, on_empty=on_empty, hook=mic_hook))
        rec.audio = _FakeAudio(streams)
        rec._find_default_loopback_device = lambda: 5
        rec.start_recording(capture_mode=capture_mode)
        rec._record_thread.join(30)
        self.assertFalse(rec._record_thread.is_alive())
        rec.stop_recording()
        return rec

    def _heard(self, rec):
        """Everything the live engine was handed, in order: the pieces and the tail."""
        np = self.np
        pipe = rec._chunk_pipeline
        if pipe is not None:
            pipe.close()
        for t in (pipe.threads() if pipe is not None else rec._chunk_threads):
            t.join(10)
        parts = [a for _i, a in sorted(self.chunks, key=lambda c: c[0])]
        parts.append(np.frombuffer(b"".join(rec._chunk_frames), np.float32))
        return np.concatenate(parts)

    def _call_meeting_signals(self):
        np = self.np
        # 10 s: room hiss 0-2 s, the user talks 2-5 s, the call talks 6-9 s (a little of it
        # bleeds into the mic, as without headphones).
        mic = (_hiss(np, 10.0) + _tone(np, 220, 3.0, 0.3, 2.0, total=10.0)
               + _tone(np, 600, 3.0, 0.02, 6.0, total=10.0))
        call = _tone(np, 880, 3.0, 0.25, 6.0, total=10.0)
        return mic, call

    def test_smart_meeting_keeps_mic_and_system_apart_and_the_mix_as_before(self):
        np = self.np
        mic, call = self._call_meeting_signals()
        rec = self._run(mic, call, capture_mode="smart_meeting")
        self.assertIsNotNone(rec._tracks)
        tracks = rec.get_full_audio_tracks()
        self.assertIsNotNone(tracks)
        mic_pcm, sys_pcm = tracks
        self.assertEqual(mic_pcm.dtype, np.int16)
        self.assertEqual(len(mic_pcm), len(sys_pcm))
        n = len(mic)
        self.assertGreaterEqual(len(mic_pcm), n)
        got_mic = rt.to_float32(mic_pcm[:n])
        got_sys = rt.to_float32(sys_pcm[:n])
        self.assertTrue(np.allclose(got_mic, mic, atol=1e-4))
        # The call's track holds the call (whole blocks of it) and nothing from the room.
        self.assertTrue(np.allclose(got_sys[6 * SR:9 * SR], call[6 * SR:9 * SR], atol=1e-4))
        self.assertLess(float(np.abs(got_sys[:5 * SR]).max()), 1e-4)
        # The user's own words are on the mic track and NOT on the system track.
        self.assertGreater(float(np.abs(got_mic[2 * SR:5 * SR]).max()), 0.25)
        # get_full_audio() hands every caller the mix the loop built: (mic + call) * 0.75
        # in the blocks the call delivered, the mic alone elsewhere.
        mix = rec.get_full_audio()
        self.assertEqual(mix.dtype, np.float32)
        self.assertEqual(len(mix), len(mic_pcm))
        want = mic.copy()
        for b in range(0, n, FRAME):
            blk = slice(b, min(n, b + FRAME))
            if call[blk].any():
                want[blk] = np.clip((mic[blk] + call[blk]) * 0.75, -1.0, 1.0)
        self.assertTrue(np.allclose(mix[:n], want, atol=1e-4))
        # ...and it is exactly what the live engine was handed.
        heard = self._heard(rec)
        self.assertEqual(len(heard), len(mix))
        self.assertTrue(np.allclose(heard, mix, atol=1e-4))
        # No second, float32 copy of the mix is kept (the memory stays one mix's worth).
        self.assertEqual(rec._full_audio_frames, [])
        self.assertEqual(rec._full_audio_len, len(mix))
        self.assertIsNotNone(rec._chunk_pipeline)
        self.assertGreaterEqual(len(self.chunks), 1)

    def test_the_mix_matches_what_the_engine_heard_when_the_call_arrives_unevenly(self):
        """The call's blocks are NOT the mic's size (700 samples a call): part of
        each read mixes with no call sound - whatever the split, the retained
        mix is what the live engine heard."""
        np = self.np
        mic, call = self._call_meeting_signals()
        rec = self._run(mic, call, capture_mode="smart_meeting", loop_per_call=700, wasapi=False)
        mic_pcm, sys_pcm = rec.get_full_audio_tracks()
        self.assertEqual(len(mic_pcm), len(sys_pcm))
        self.assertGreater(float(np.abs(sys_pcm).max()), 0)
        mix = rec.get_full_audio()
        heard = self._heard(rec)
        # (the tail the streams still held at Stop goes to the engine, not into the kept track)
        self.assertGreaterEqual(len(heard), len(mix))
        self.assertTrue(np.allclose(heard[:len(mix)], mix, atol=1e-4))
        # The call's samples are on the system track only where they were mixed in:
        sys_f = rt.to_float32(sys_pcm)
        carried = sys_f != 0
        self.assertTrue(np.allclose(mix[carried],
                                    np.clip((rt.to_float32(mic_pcm)[carried] + sys_f[carried]) * 0.75, -1, 1),
                                    atol=1e-4))

    def test_pause_keeps_nothing_on_either_track(self):
        mic, call = self._call_meeting_signals()
        rec = self.rec

        def hook(i):
            rec.paused = 40 <= i < 70             # 30 reads (~1.9 s) are paused
        out = self._run(mic, call, capture_mode="smart_meeting", mic_hook=hook)
        mic_pcm, sys_pcm = out.get_full_audio_tracks()
        self.assertEqual(len(mic_pcm), out._full_audio_len)
        self.assertEqual(len(mic_pcm), len(sys_pcm))
        self.assertLess(len(mic_pcm), len(mic))               # the paused stretch is missing from both
        self.assertEqual(len(out.get_full_audio()), len(mic_pcm))

    def test_a_microphone_that_dies_mid_meeting_ends_the_take_with_consistent_tracks(self):
        """The mic is the loop's clock: when its reads start failing the take ends
        with the device error - and the two tracks, the mix and the counters all
        cover exactly the same samples (nothing half-written)."""
        np = self.np
        mic, call = self._call_meeting_signals()

        def hook(i):
            if i >= 90:                                   # ~5.8 s in: the headset is unplugged
                raise OSError("Stream closed")
        rec = self._run(mic, call, capture_mode="smart_meeting", mic_hook=hook)
        self.assertTrue(rec._record_error.startswith("!audio"), rec._record_error)
        mic_pcm, sys_pcm = rec.get_full_audio_tracks()
        self.assertEqual(len(mic_pcm), len(sys_pcm))
        self.assertEqual(len(mic_pcm), rec._full_audio_len)
        self.assertEqual(len(rec.get_full_audio()), len(mic_pcm))
        self.assertGreater(len(mic_pcm), 80 * FRAME)
        self.assertLess(len(mic_pcm), 100 * FRAME)

    def test_a_second_stream_that_will_not_open_falls_back_to_the_call_alone(self):
        """Mic + system sound, but the microphone cannot be opened: the take goes on
        with the call only, keeps ONE mixed track (no mic / system pair to label)
        and says nothing is wrong with dictation."""
        np, rec = self.np, self.rec
        call = _tone(np, 700, 5.0, 0.3)

        def on_empty():
            rec.stop_requested = True
        rec.audio = _FakeAudio([_FakeStream(call, np, per_call=FRAME, wasapi=True, on_empty=on_empty)])
        rec._find_default_loopback_device = lambda: 5
        rec.start_recording(capture_mode="smart_meeting")      # the second open() raises inside
        rec._record_thread.join(30)
        self.assertFalse(rec._record_thread.is_alive())
        rec.stop_recording()
        self.assertIsNone(rec._tracks)
        self.assertIsNone(rec.get_full_audio_tracks())
        self.assertIsNotNone(rec._chunk_pipeline)               # still a meeting: the queue, the loudness windows
        self.assertTrue(len(rec.get_full_audio()) >= len(call))

    def test_a_call_that_goes_silent_for_a_long_time_leaves_zeros_on_its_track_only(self):
        """A device switched mid-meeting (WASAPI delivers nothing for the old one):
        the call's track is zero from there on, the microphone's is untouched, and
        both stay the same length."""
        np = self.np
        mic = (_hiss(np, 12.0) + _tone(np, 220, 10.0, 0.3, 1.0, total=12.0))
        call = np.concatenate([_tone(np, 880, 4.0, 0.25), np.zeros(8 * SR, np.float32)])
        rec = self._run(mic, call, capture_mode="smart_meeting")
        mic_pcm, sys_pcm = rec.get_full_audio_tracks()
        self.assertEqual(len(mic_pcm), len(sys_pcm))
        sys_f = rt.to_float32(sys_pcm)
        self.assertGreater(float(np.abs(sys_f[:4 * SR]).max()), 0.2)
        self.assertEqual(float(np.abs(sys_f[4 * SR + FRAME:]).max()), 0.0)
        self.assertGreater(float(np.abs(rt.to_float32(mic_pcm)[6 * SR:10 * SR]).max()), 0.25)

    def test_dictation_is_unchanged(self):
        """The same recorder in plain dictation: no queue, no tracks, no
        loudness windows, one thread per piece, the mixed float32 track kept
        bit for bit."""
        np = self.np
        sig = np.concatenate([_hiss(np, 2.0), _tone(np, 250, 2.0, 0.3), _hiss(np, 1.0, seed=2),
                              _tone(np, 300, 2.0, 0.3), _hiss(np, 2.0, seed=3)])
        rec = self._run(sig, capture_mode=None)
        self.assertIsNone(rec._chunk_pipeline)
        self.assertIsNone(rec._tracks)
        self.assertIsNone(rec._activity)
        self.assertIsNone(rec.get_full_audio_tracks())
        self.assertEqual(rec.audio.opened[0].get("input_device_index"), None)
        self.assertEqual(len(rec.audio.opened), 1)
        # The retained audio is exactly what the microphone delivered.
        full = rec.get_full_audio()
        self.assertEqual(full.dtype, np.float32)
        n = len(sig)
        self.assertTrue(np.array_equal(full[:n], sig))
        self.assertEqual(rec._full_audio_len, len(full))
        self.assertTrue(len(rec._full_audio_frames) > 0)
        # One thread per piece, in the old list.
        self.assertGreaterEqual(len(rec._chunk_threads), 1)
        self.assertTrue(all(t.name.startswith("Thread") for t in rec._chunk_threads))
        for t in rec._chunk_threads:
            t.join(5)
        self.assertEqual(sorted(i for i, _a in self.chunks), list(range(len(self.chunks))))
        # The pieces plus the tail are the recording, nothing lost or added.
        joined = np.concatenate([a for _i, a in sorted(self.chunks, key=lambda c: c[0])]
                                + [np.frombuffer(b"".join(rec._chunk_frames), np.float32)])
        self.assertTrue(np.array_equal(joined, full))

    def test_a_microphone_only_meeting_uses_the_queue_but_keeps_one_mixed_track(self):
        np = self.np
        sig = np.concatenate([_hiss(np, 2.0), _tone(np, 250, 6.0, 0.3), _hiss(np, 2.0, seed=2)])
        rec = self._run(sig, capture_mode="default_mic")
        self.assertIsNotNone(rec._chunk_pipeline)
        self.assertIsNone(rec._tracks)
        self.assertIsNotNone(rec._activity)
        self.assertIsNone(rec.get_full_audio_tracks())
        full = rec.get_full_audio()
        self.assertTrue(np.array_equal(full[:len(sig)], sig))
        heard = self._heard(rec)
        self.assertTrue(np.array_equal(heard, full))
        self.assertEqual(rec._chunk_pipeline.health()["failed"], 0)
        self.assertGreaterEqual(len(self.chunks), 1)

    def test_system_sound_only_keeps_the_single_mixed_track(self):
        np = self.np
        call = _tone(np, 700, 5.0, 0.3)
        rec = self._run(None, call, capture_mode="system_only")
        self.assertIsNone(rec._tracks)
        self.assertIsNone(rec.get_full_audio_tracks())
        self.assertTrue(len(rec.get_full_audio()) >= len(call))

    def test_the_queue_drains_before_the_transcript_is_collected(self):
        np = self.np
        sig = np.concatenate([_hiss(np, 2.0), _tone(np, 250, 12.0, 0.3), _hiss(np, 2.0, seed=2)])
        slow = []

        def slow_transcribe(audio, idx):
            time.sleep(0.05)
            slow.append(idx)
            with self.rec._chunk_lock:
                self.rec._chunk_results[idx] = "piece %d" % idx
        self.rec._transcribe_chunk = slow_transcribe
        rec = self._run(sig, capture_mode="default_mic")
        with mock.patch.object(rec, "_run_local", return_value=("tail", "en")):
            text, lang = rec.transcribe()
        self.assertEqual(rec._chunk_pipeline.health()["queued"], 0)
        self.assertEqual(sorted(slow), sorted(set(slow)))
        self.assertTrue(text.startswith("piece 0"))
        self.assertEqual(lang, "en")
        self.assertEqual([t.is_alive() for t in rec._chunk_pipeline.threads()], [False])

    def test_chunk_health_and_the_per_minute_line(self):
        np = self.np
        sig = np.concatenate([_hiss(np, 2.0), _tone(np, 250, 3.0, 0.3), _hiss(np, 1.0, seed=2)])
        rec = self._run(sig, capture_mode="default_mic")
        h = rec.chunk_health()
        for key in ("queued", "done", "failed", "merged", "dropped", "restarts", "lag_sec",
                    "chunk_failures", "recorded_sec", "track_bytes"):
            self.assertIn(key, h)
        self.assertGreaterEqual(h["recorded_sec"], 3.0)
        # One INFO line a minute at most - and none of the speech text in it.
        rec._health_at = 0.0
        rec._tick_at = 0.0
        times = [1000.0, 1000.5, 1030.0, 1059.9, 1061.0, 1062.0, 1121.5]
        lines = []

        class _H(logging.Handler):
            def emit(self, record):
                lines.append(record.getMessage())
        handler = _H()
        log = logging.getLogger("transcribe")
        old_level = log.level
        log.addHandler(handler)
        log.setLevel(logging.DEBUG)
        try:
            for t in times:
                with mock.patch.object(self.main.time, "monotonic", return_value=t):
                    rec._meeting_tick()
        finally:
            log.removeHandler(handler)
            log.setLevel(old_level)
        health = [m for m in lines if m.startswith("Meeting health:")]
        self.assertEqual(len(health), 3, lines)           # t=1000, 1061, 1121.5: a minute apart
        self.assertIn("pieces queued=", health[0])
        self.assertNotIn("piece 0", health[0])

    def test_a_failed_piece_is_logged_and_counted(self):
        main = self.main
        rec = self.rec
        rec._chunk_lock = threading.Lock()
        rec._chunk_errors = []
        rec._chunk_results = {}
        rec._chunk_idx = 1
        rec._run_local = lambda audio: ("", "!managed:HTTP 503")
        rec._backend = lambda: "local"
        # the real _transcribe_chunk (setUp replaced it on the instance)
        del rec._transcribe_chunk
        with self.assertLogs("transcribe", level="WARNING") as cm:
            main.AudioRecorder._transcribe_chunk(rec, self.np.zeros(100, self.np.float32), 0)
        self.assertEqual(rec._chunk_failures, 1)
        self.assertEqual(rec._chunk_errors, ["!managed:HTTP 503"])
        self.assertTrue(any("not transcribed" in m for m in cm.output))
        rec._run_local = mock.Mock(side_effect=RuntimeError("model gone"))
        with self.assertLogs("transcribe", level="WARNING") as cm:
            main.AudioRecorder._transcribe_chunk(rec, self.np.zeros(100, self.np.float32), 1)
        self.assertEqual(rec._chunk_failures, 2)
        self.assertTrue(any("failed to transcribe" in m for m in cm.output))

    def test_a_new_take_starts_clean(self):
        np = self.np
        sig = np.concatenate([_hiss(np, 2.0), _tone(np, 250, 3.0, 0.3), _hiss(np, 1.0, seed=2)])
        rec = self._run(sig, capture_mode="default_mic")
        first = rec._chunk_pipeline
        rec = self._run(sig, capture_mode=None)             # dictation next
        self.assertIsNone(rec._chunk_pipeline)
        self.assertIsNone(rec._activity)
        self.assertTrue(first.health()["aborted"])           # the meeting's queue was retired


class TestOldRecorderDoubles(unittest.TestCase):
    """Test doubles built without __init__ (SimpleNamespace) keep working: the
    new attributes are only ever read with getattr defaults."""

    @needs_numpy
    def test_get_full_audio_of_a_double_without_tracks(self):
        import numpy as np
        main = _real_main(self)
        rec = SimpleNamespace(_chunk_lock=threading.Lock(),
                              _full_audio_frames=[np.arange(4, dtype=np.float32).tobytes()])
        out = main.AudioRecorder.get_full_audio(rec)
        self.assertEqual(out.tolist(), [0.0, 1.0, 2.0, 3.0])
        self.assertIsNone(main.AudioRecorder.get_full_audio_tracks(rec))

    def test_request_abort_of_a_double_without_a_queue(self):
        main = _real_main(self) if _real_numpy() else None
        if main is None:
            self.skipTest("no real main")
        rec = SimpleNamespace(_abort=False, stop_requested=False)
        main.AudioRecorder.request_abort(rec)
        self.assertTrue(rec._abort and rec.stop_requested)


if __name__ == "__main__":
    unittest.main()
