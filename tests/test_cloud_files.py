"""cloud_files: a file cut into pieces at pauses, sent a few at a time, and
the text back in file order with estimated times.

Every send, sleep and clock here is a fake, so nothing goes to the network.
The planner, sentence and timing tests are plain Python; the ones that
encode audio need the real numpy (skipped under test_core's stubs).
"""
import collections
import dataclasses
import io
import os
import random
import struct
import subprocess
import sys
import threading
import time
import unittest
import wave
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import numpy as np  # noqa: E402

try:
    import numpy.random  # noqa: F401,E402  (loaded now: numpy is stubbed later in a full run)
except Exception:
    pass

import cloud_files as cf  # noqa: E402
import cloud_speech as cs  # noqa: E402

SR = 16000
WIN = cf.WIN_S
GUSAN = cf.PROFILES["gusan"]
OWN = cf.PROFILES["google"]
ONE = dataclasses.replace(GUSAN, workers=1)


def _real_numpy():
    # tests/test_core.py stubs numpy for the whole discover run.
    try:
        return np.__name__ == "numpy" and tuple(np.zeros(2).shape) == (2,)
    except Exception:
        return False


def _real_vad():
    try:
        import onnxruntime  # noqa: F401
        from faster_whisper.vad import get_vad_model
        return _real_numpy() and callable(get_vad_model)
    except Exception:
        return False


def spec(engine="gusan", **kw):
    kw.setdefault("model_id", {"gusan": "gemini-2.5-flash", "google": "gemini-2.5-flash",
                               "mistral": "voxtral-mini-latest"}[engine])
    return cs.Spec(engine=engine, **kw)


def err(kind, retry_after=None, name="Gusan Cloud"):
    return cs.CloudSpeechError(kind, cs.error_message(kind, name), retry_after)


class FakeTime:
    """clock() and sleep(): sleeping only moves the clock (and is recorded)."""

    def __init__(self):
        self.now = 0.0
        self.sleeps = []
        self._lock = threading.Lock()

    def clock(self):
        return self.now

    def advance(self, seconds):
        with self._lock:
            self.now += seconds

    def sleep(self, seconds):
        with self._lock:
            self.sleeps.append(seconds)
            self.now += seconds
        return False


def pieces(n, length=8.0, step=10.0):
    """Audio with n pieces; piece i is a constant level (i + 1) / 100, so a
    fake send can tell which piece a WAV is (piece_of)."""
    audio = np.zeros(int(n * step * SR), dtype=np.float32)
    chunks = []
    for i in range(n):
        s, e = i * step, i * step + length
        audio[int(s * SR):int(e * SR)] = (i + 1) / 100.0
        chunks.append(cf.Chunk(i, s, e, ((s, e),)))
    return audio, chunks


def piece_of(wav):
    with wave.open(io.BytesIO(wav)) as w:
        first = struct.unpack("<h", w.readframes(1))[0]
    return int(round(first / 327.67)) - 1


def talk(seconds, seed=1, long_gap_at=None):
    """Speech-like audio: noise bursts with short pauses (a long one at
    ``long_gap_at`` seconds), near-silence between."""
    rng = np.random.default_rng(seed)
    audio = (rng.standard_normal(int(seconds * SR)) * 0.0005).astype(np.float32)
    t = 0.5
    while t < seconds - 1:
        n = rng.uniform(2.0, 6.0)
        e = min(t + n, seconds - 0.5)
        a, b = int(t * SR), int(e * SR)
        audio[a:b] += (rng.standard_normal(b - a) * 0.2).astype(np.float32)
        t = e + rng.uniform(0.4, 1.2)
        if long_gap_at is not None and t <= long_gap_at < t + 8:
            t += 20.0
    return audio


def plan(regions, profile=GUSAN, total=None):
    planner = cf.ChunkPlanner(profile)
    out = []
    for s, e in regions:
        out += planner.add(s, e)
    if total is None:
        total = regions[-1][1] + 1.0 if regions else 0.0
    out += planner.finish(total)
    return out, planner


def utterances(start, end, length=4.0, pause=0.5):
    out, t = [], start
    while t < end:
        out.append((t, min(t + length, end)))
        t += length + pause
    return out


# ── profiles ─────────────────────────────────────────────────────────────────
class TestProfiles(unittest.TestCase):
    def test_piece_sizes_and_workers(self):
        self.assertEqual(cf.PROFILES["gusan"], cf.Profile(60, 45, 75, 30, 90, 4))
        for engine in ("google", "mistral"):
            self.assertEqual(cf.PROFILES[engine], cf.Profile(120, 90, 150, 60, 240, 2))

    def test_profile_for_the_engine(self):
        self.assertIs(cf.profile_for(spec("gusan")), cf.PROFILES["gusan"])
        self.assertIs(cf.profile_for(spec("google")), cf.PROFILES["google"])
        self.assertIs(cf.profile_for(spec("mistral")), cf.PROFILES["mistral"])
        self.assertIs(cf.profile_for(object()), cf.PROFILES["gusan"])

    def test_back_off_schedule(self):
        for tries, base in ((1, 2), (2, 6), (3, 20), (9, 20)):
            for _ in range(20):
                self.assertTrue(base * 0.7 <= cf.backoff(tries) <= base * 1.3)
        self.assertEqual(cf.backoff(1, retry_after=12), 12)
        self.assertEqual(cf.backoff(1, retry_after=600), 60)
        self.assertTrue(1.4 <= cf.backoff(1, retry_after="soon") <= 2.6)
        self.assertTrue(1.4 <= cf.backoff(1, retry_after=0) <= 2.6)

    def test_light_imports(self):
        code = ("import sys; sys.path.insert(0, %r); import cloud_files; "
                "print(sorted(m for m in ('PySide6', 'main', 'requests', 'faster_whisper', 'ui') "
                "if m in sys.modules))" % ROOT)
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120)
        if out.returncode != 0 and "numpy" in out.stderr:
            self.skipTest("numpy not installed")
        self.assertEqual(out.stdout.strip(), "[]", out.stderr)


# ── sentences ────────────────────────────────────────────────────────────────
class TestSplitSentences(unittest.TestCase):
    def test_stops(self):
        self.assertEqual(cf.split_sentences("Hello there, my friend. How are you today? I am fine, thanks!"),
                         ["Hello there, my friend.", "How are you today?", "I am fine, thanks!"])

    def test_armenian_full_stop(self):
        self.assertEqual(cf.split_sentences("Բարեւ, ինչպե՞ս ես։ Ես լավ եմ, շնորհակալություն։"),
                         ["Բարեւ, ինչպե՞ս ես։", "Ես լավ եմ, շնորհակալություն։"])

    def test_cjk_stops_need_no_space(self):
        self.assertEqual(cf.split_sentences("今日はとても良い天気ですね。明日は雨が降るでしょう。"),
                         ["今日はとても良い天気ですね。", "明日は雨が降るでしょう。"])

    def test_line_breaks_always_split(self):
        self.assertEqual(cf.split_sentences("First line of the text\n\n  Second line of the text"),
                         ["First line of the text", "Second line of the text"])
        # A short line is often a new speaker: it stays on its own.
        self.assertEqual(cf.split_sentences("Are you coming tonight?\nYes.\nGreat, see you then."),
                         ["Are you coming tonight?", "Yes.", "Great, see you then."])

    def test_short_parts_join_a_neighbour(self):
        self.assertEqual(cf.split_sentences("Dr. Smith arrived late. Yes. We started anyway."),
                         ["Dr. Smith arrived late. Yes.", "We started anyway."])

    def test_numbers_and_addresses_stay_whole(self):
        self.assertEqual(cf.split_sentences("It costs 3.5 dollars at example.com today."),
                         ["It costs 3.5 dollars at example.com today."])

    def test_closing_quote_stays_with_its_sentence(self):
        self.assertEqual(cf.split_sentences('She said "we are done." Then everyone left.'),
                         ['She said "we are done."', "Then everyone left."])

    def test_long_sentence_is_halved_at_a_comma(self):
        words = ", ".join(f"item number {i} on the list" for i in range(20)) + "."
        parts = cf.split_sentences(words)
        self.assertGreater(len(parts), 1)
        self.assertTrue(all(len(p) <= cf.MAX_SENTENCE for p in parts))
        self.assertEqual(" ".join(parts).split(), words.split())
        self.assertTrue(all(p.endswith((",", ".")) for p in parts))

    def test_long_unpunctuated_text_is_halved_at_a_space(self):
        words = " ".join(["word"] * 120)
        parts = cf.split_sentences(words)
        self.assertTrue(all(len(p) <= cf.MAX_SENTENCE for p in parts))
        self.assertEqual(" ".join(parts), words)

    def test_nothing(self):
        for text in ("", "   ", None, "\n\n"):
            self.assertEqual(cf.split_sentences(text), [])


class TestSpreadSegments(unittest.TestCase):
    def check(self, chunk, segs):
        lo, hi = chunk.start, chunk.end
        prev = lo
        for s in segs:
            self.assertLessEqual(prev, s["start"])
            self.assertLessEqual(s["start"], s["end"])
            self.assertLessEqual(s["end"], hi)
            self.assertGreaterEqual(s["start"], lo)
            prev = s["end"]

    def test_boundary_moves_into_a_pause(self):
        chunk = cf.Chunk(0, 0.0, 20.0, ((0.0, 10.0), (11.0, 20.0)))
        segs = cf.spread_segments(chunk, "One two three four five. Six seven eight nine ten.")
        self.assertEqual([s["text"] for s in segs],
                         ["One two three four five.", "Six seven eight nine ten."])
        self.assertEqual((segs[0]["start"], segs[0]["end"]), (0.0, 10.0))
        self.assertEqual((segs[1]["start"], segs[1]["end"]), (11.0, 20.0))

    def test_no_pause_nearby_splits_by_length(self):
        chunk = cf.Chunk(0, 5.0, 35.0, ((5.0, 35.0),))
        segs = cf.spread_segments(chunk, "Aaaa bbbb cccc. Dddd eeee ffff.")
        self.assertAlmostEqual(segs[0]["end"], 20.0)
        self.assertAlmostEqual(segs[1]["start"], 20.0)
        self.assertEqual((segs[0]["start"], segs[1]["end"]), (5.0, 35.0))

    def test_a_short_pause_is_not_used(self):
        chunk = cf.Chunk(0, 0.0, 20.1, ((0.0, 10.0), (10.1, 20.1)))
        segs = cf.spread_segments(chunk, "One two three four five. Six seven eight nine ten.")
        self.assertEqual(segs[0]["end"], segs[1]["start"])

    def test_longer_sentences_get_more_time(self):
        chunk = cf.Chunk(0, 0.0, 60.0, ((0.0, 60.0),))
        longer = "This one is a great deal longer than the first. " * 3
        segs = cf.spread_segments(chunk, "Short one here. " + longer)
        self.assertLess(segs[0]["end"] - segs[0]["start"], segs[1]["end"] - segs[1]["start"])

    def test_one_sentence_covers_the_speech(self):
        chunk = cf.Chunk(3, 100.0, 160.0, ((100.4, 120.0), (121.0, 159.5)))
        self.assertEqual(cf.spread_segments(chunk, "Just one sentence here"),
                         [{"start": 100.4, "end": 159.5, "text": "Just one sentence here"}])

    def test_without_speech_regions_the_whole_piece(self):
        segs = cf.spread_segments(cf.Chunk(0, 10.0, 20.0), "Hello there everyone. Nice to see you all.")
        self.assertEqual((segs[0]["start"], segs[-1]["end"]), (10.0, 20.0))

    def test_no_text_no_segments(self):
        self.assertEqual(cf.spread_segments(cf.Chunk(0, 0.0, 5.0), ""), [])

    def test_always_monotonic_and_inside_the_piece(self):
        rng = random.Random(7)
        vocab = ["alpha", "beta", "gamma", "delta", "Ok.", "yes?", "Բարեւ։", "今日は。", "\n"]
        for _ in range(300):
            t = rng.uniform(0, 1000)
            start, speech = t, []
            for _ in range(rng.randint(1, 12)):
                s = t + rng.uniform(0, 3)
                e = s + rng.uniform(0.1, 9)
                speech.append((s, e))
                t = e
            chunk = cf.Chunk(0, start, t + rng.uniform(0, 1), tuple(speech))
            text = " ".join(rng.choice(vocab) for _ in range(rng.randint(1, 80))) + "."
            segs = cf.spread_segments(chunk, text)
            self.check(chunk, segs)
            self.assertEqual([s["text"] for s in segs], cf.split_sentences(text))


# ── finding speech ───────────────────────────────────────────────────────────
class TestRegionTracker(unittest.TestCase):
    def regions(self, probs, blocks=None, **kw):
        tr = cf.RegionTracker(**kw)
        out = []
        if blocks is None:
            out += tr.feed(probs)
        else:
            at = 0
            for size in blocks:
                out += tr.feed(probs[at:at + size])
                at += size
            out += tr.feed(probs[at:])
        return out + tr.finish(len(probs) * WIN)

    def test_padded_region(self):
        probs = [0.0] * 50 + [0.9] * 100 + [0.0] * 50
        (s, e), = self.regions(probs)
        self.assertAlmostEqual(s, 50 * WIN - 0.2)
        self.assertAlmostEqual(e, 150 * WIN + 0.2)

    def test_a_short_dip_does_not_end_speech(self):
        probs = [0.9] * 100 + [0.1] * 5 + [0.9] * 100 + [0.0] * 30
        self.assertEqual(len(self.regions(probs)), 1)
        # Between off and thr counts as speech once it has started.
        probs = [0.9] * 100 + [0.3] * 40 + [0.9] * 100
        self.assertEqual(len(self.regions(probs)), 1)

    def test_close_regions_share_the_gap(self):
        probs = [0.0] * 20 + [0.9] * 100 + [0.0] * 10 + [0.9] * 100 + [0.0] * 20
        (s1, e1), (s2, e2) = self.regions(probs)
        self.assertAlmostEqual(e1, s2)
        self.assertAlmostEqual(e1, 125 * WIN)

    def test_block_edges_change_nothing(self):
        rng = random.Random(3)
        probs = []
        while len(probs) < 20000:
            probs += [rng.choice((0.05, 0.2, 0.3, 0.5, 0.9))] * rng.randint(1, 60)
        whole = self.regions(probs, max_len=30)
        for _ in range(5):
            blocks = [rng.randint(1, 3000) for _ in range(8)]
            self.assertEqual(self.regions(probs, blocks, max_len=30), whole)

    def test_hard_max_cut_at_the_quietest_window(self):
        probs = [0.9] * 5000
        probs[1500] = 0.45
        regs = self.regions(probs, max_len=60)
        self.assertTrue(all(e - s <= 60 + 1e-9 for s, e in regs))
        self.assertAlmostEqual(regs[0][1], 1500 * WIN)
        for (_, e1), (s2, _) in zip(regs, regs[1:]):
            self.assertAlmostEqual(e1, s2)                  # the halves touch
        self.assertAlmostEqual(regs[-1][1], 5000 * WIN)     # clamped to the file

    def test_hard_max_ends_at_a_pause_in_progress(self):
        # 60 s = 1862 windows after the pads; a 6-window pause starts at 1858.
        probs = [0.9] * 1858 + [0.1] * 6 + [0.9] * 3000
        regs = self.regions(probs, max_len=60)
        self.assertTrue(all(e - s <= 60 + 1e-9 for s, e in regs))
        self.assertAlmostEqual(regs[0][1], 1858 * WIN + 3 * WIN)     # half the gap each
        self.assertAlmostEqual(regs[1][0], 1864 * WIN - 3 * WIN)

    def test_finish_clamps_to_the_file(self):
        probs = [0.9] * 100
        self.assertEqual(self.regions(probs), [(0.0, 100 * WIN)])
        self.assertEqual(cf.RegionTracker().finish(10.0), [])


class TestChunkPlanner(unittest.TestCase):
    def check(self, chunks, regions, profile=GUSAN):
        self.assertEqual([c.index for c in chunks], list(range(len(chunks))))
        starts = {s for s, _ in regions}
        ends = {e for _, e in regions}
        for c in chunks:
            self.assertIn(c.start, starts)                   # cut only in pauses
            self.assertIn(c.end, ends)
            self.assertLessEqual(c.end - c.start, profile.hard_max + 1e-9)
            self.assertEqual((c.speech[0][0], c.speech[-1][1]), (c.start, c.end))
        for a, b in zip(chunks, chunks[1:]):
            self.assertLessEqual(a.end, b.start)

    def test_pieces_near_the_target(self):
        regs = utterances(0.0, 600.0)
        chunks, _ = plan(regs)
        self.check(chunks, regs)
        for c in chunks[:-1]:
            self.assertTrue(GUSAN.cut_lo <= c.end - c.start <= GUSAN.cut_hi, c)

    def test_own_key_pieces_are_longer(self):
        regs = utterances(0.0, 1200.0)
        chunks, _ = plan(regs, OWN)
        self.check(chunks, regs, OWN)
        for c in chunks[:-1]:
            self.assertTrue(OWN.cut_lo <= c.end - c.start <= OWN.cut_hi, c)

    def test_the_longest_pause_in_the_window_wins(self):
        regs = utterances(0.0, 49.0, pause=0.4) + utterances(50.2, 58.0, pause=0.4) \
            + utterances(59.8, 120.0, pause=0.4)
        chunks, _ = plan(regs)
        self.check(chunks, regs)
        self.assertEqual(chunks[0].end, regs[[s for s, _ in regs].index(59.8) - 1][1])

    def test_long_silence_is_never_sent(self):
        regs = utterances(0.0, 40.0) + utterances(640.0, 700.0)
        chunks, planner = plan(regs, total=720.0)
        self.check(chunks, regs)
        self.assertFalse(any(c.start < 600 < c.end for c in chunks))
        self.assertEqual([round(c.end, 3) for c in chunks if c.end <= 600], [40.0])

    def test_a_long_pause_closes_a_long_enough_piece(self):
        regs = utterances(0.0, 35.0) + utterances(38.0, 80.0)
        chunks, _ = plan(regs)
        self.check(chunks, regs)
        self.assertEqual((chunks[0].start, chunks[0].end), (0.0, 35.0))
        self.assertEqual(chunks[1].start, 38.0)

    def test_a_short_piece_keeps_a_long_pause(self):
        regs = utterances(0.0, 10.0) + utterances(13.0, 50.0)
        chunks, _ = plan(regs)
        self.check(chunks, regs)
        self.assertEqual(len(chunks), 1)
        self.assertEqual((chunks[0].start, chunks[0].end), (0.0, 50.0))

    def test_a_short_piece_ends_at_a_silence_reaching_min_s(self):
        regs = utterances(0.0, 10.0) + utterances(35.0, 80.0)
        chunks, _ = plan(regs)
        self.check(chunks, regs)
        self.assertEqual([(c.start, c.end) for c in chunks], [(0.0, 10.0), (35.0, regs[-1][1])])

    def test_continuous_speech_is_cut_where_regions_touch(self):
        regs = [(i * 54.0, (i + 1) * 54.0) for i in range(10)]   # RegionTracker's hard-max cuts
        chunks, _ = plan(regs)
        self.check(chunks, regs)
        self.assertEqual(len(chunks), 10)

    def test_a_short_tail_joins_the_piece_before(self):
        regs = utterances(0.0, 80.0)
        chunks, _ = plan(regs)
        self.check(chunks, regs)
        self.assertEqual([(c.start, c.end) for c in chunks], [(0.0, 80.0)])

    def test_a_tail_that_would_be_too_long_stays_apart(self):
        regs = utterances(0.0, 74.0, pause=0.4) + utterances(75.5, 96.0, pause=0.4)
        chunks, _ = plan(regs)
        self.check(chunks, regs)
        self.assertEqual(len(chunks), 2)
        self.assertLess(chunks[1].end - chunks[1].start, GUSAN.min_s)

    def test_pieces_and_skipped_cover_the_file(self):
        rng = random.Random(5)
        for _ in range(30):
            t, regs = rng.uniform(0, 20), []
            for _ in range(rng.randint(1, 200)):
                s = t + rng.choice((0.0, rng.uniform(0, 1.5), rng.uniform(2, 40)))
                e = s + rng.uniform(0.2, 30)
                regs.append((s, e))
                t = e
            total = t + rng.uniform(0, 30)
            chunks, planner = plan(regs, total=total)
            self.check(chunks, regs)
            covered = sum(c.end - c.start for c in chunks)
            self.assertAlmostEqual(covered + planner.skipped, total, places=6)

    def test_five_hours_stay_within_600_pieces(self):
        rng = random.Random(11)
        regs, t, total = [], 1.0, 5 * 3600.0
        while t < total - 30:
            s = t
            e = s + rng.uniform(1.0, 20.0)
            regs.append((s, e))
            r = rng.random()
            t = e + (rng.uniform(0.0, 1.2) if r < 0.85 else rng.uniform(2.5, 15) if r < 0.98
                     else rng.uniform(60, 600))
        chunks, _ = plan(regs, total=total)
        self.check(chunks, regs)
        self.assertLessEqual(len(chunks), 600)
        # Silences of min_s or more are never sent.
        for c in chunks:
            for (_, e1), (s2, _) in zip(c.speech, c.speech[1:]):
                self.assertLess(s2 - e1, GUSAN.min_s)

    def test_sparse_five_hours_stay_within_600_pieces(self):
        regs = [(i * 3.5, i * 3.5 + 1.0) for i in range(int(5 * 3600 / 3.5))]
        chunks, _ = plan(regs, total=5 * 3600.0)
        self.check(chunks, regs)
        self.assertLessEqual(len(chunks), 600)

    def test_five_hours_through_the_tracker(self):
        rng = random.Random(13)
        total_win = int(5 * 3600 / WIN)
        probs = []
        while len(probs) < total_win:
            probs += [0.9] * rng.randint(30, 600)              # 1-19 s of speech
            r = rng.random()
            quiet = (rng.randint(5, 40) if r < 0.9 else rng.randint(80, 500) if r < 0.99
                     else rng.randint(2000, 20000))
            probs += [0.05] * quiet
        probs = probs[:total_win]
        tracker = cf.RegionTracker(max_len=GUSAN.hard_max)
        planner = cf.ChunkPlanner(GUSAN)
        chunks, regs = [], []
        block = int(cf.BLOCK_S / WIN)
        for at in range(0, total_win, block):
            for s, e in tracker.feed(probs[at:at + block]):
                regs.append((s, e))
                chunks += planner.add(s, e)
        for s, e in tracker.finish(total_win * WIN):
            regs.append((s, e))
            chunks += planner.add(s, e)
        chunks += planner.finish(total_win * WIN)
        self.check(chunks, regs)
        self.assertLessEqual(len(chunks), 600)
        self.assertGreater(len(chunks), 100)


# ── audio ────────────────────────────────────────────────────────────────────
@unittest.skipUnless(_real_numpy(), "real numpy not importable (stubbed)")
class TestAudio(unittest.TestCase):
    def test_encode_wav_clips_and_slices(self):
        audio = np.zeros(SR, dtype=np.float32)
        audio[4000:4004] = [2.0, -3.0, 0.5, -0.5]
        wav = cf.encode_wav(audio, 0.25, 0.75)
        with wave.open(io.BytesIO(wav)) as w:
            self.assertEqual((w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()),
                             (1, 2, SR, SR // 2))
            first = struct.unpack("<4h", w.readframes(4))
        self.assertEqual(first, (32767, -32767, 16384, -16384))
        self.assertEqual(audio[4000], 2.0)                    # the file is untouched

    def test_vad_runs_on_five_minute_blocks_of_a_copy(self):
        seen = []

        def fake_vad(block):
            seen.append(len(block))
            block[:] = 0.0                                    # the real one writes into its input
            return np.full(len(block) // cf.WINDOW, 0.9, dtype=np.float32)

        audio = (np.random.default_rng(0).standard_normal(11 * 60 * SR + 100) * 0.1).astype(np.float32)
        before = audio.copy()
        with mock.patch.object(cf, "_vad_model", return_value=fake_vad):
            blocks = list(cf.speech_probs(audio))
        self.assertEqual(seen[:2], [300 * SR, 300 * SR])
        self.assertTrue(all(n % cf.WINDOW == 0 and n <= 300 * SR for n in seen))
        self.assertEqual(sum(len(b) for b in blocks), -(-len(audio) // cf.WINDOW))
        self.assertTrue(np.array_equal(audio, before))

    def test_loudness_when_the_vad_is_missing_or_fails(self):
        audio = np.zeros(20 * SR, dtype=np.float32)
        audio[5 * SR:10 * SR] = (np.random.default_rng(1).standard_normal(5 * SR) * 0.2)

        def broken(block):
            raise RuntimeError("onnx")

        for model in (None, broken):
            with mock.patch.object(cf, "_vad_model", return_value=model):
                probs = np.concatenate(list(cf.speech_probs(audio)))
            loud = probs[int(6 / WIN):int(9 / WIN)]
            quiet = np.concatenate([probs[:int(4 / WIN)], probs[int(11 / WIN):]])
            self.assertTrue((loud >= cf.SPEECH_ON).all())
            self.assertTrue((quiet < cf.SPEECH_OFF).all())

    def test_stop_ends_the_blocks(self):
        stop = threading.Event()
        stop.set()
        with mock.patch.object(cf, "_vad_model", return_value=None):
            self.assertEqual(list(cf.speech_probs(np.zeros(600 * SR, dtype=np.float32), stop=stop)), [])

    @unittest.skipUnless(_real_vad(), "faster-whisper's VAD not importable")
    def test_real_vad(self):
        saved = dict(cf._VAD)
        cf._VAD.update(model=None, tried=False)
        try:
            model = cf._vad_model()
            blocks = list(cf.speech_probs(np.zeros(10 * SR, dtype=np.float32)))
        finally:
            cf._VAD.clear()
            cf._VAD.update(saved)
        if model is None:
            self.skipTest("faster-whisper is stubbed in this run")
        self.assertEqual(len(blocks), 1)
        self.assertEqual(len(blocks[0]), -(-10 * SR // cf.WINDOW))
        self.assertTrue((blocks[0] < cf.SPEECH_ON).all())


# ── sending ──────────────────────────────────────────────────────────────────
@unittest.skipUnless(_real_numpy(), "real numpy not importable (stubbed)")
class TestTranscribeChunks(unittest.TestCase):
    def run_chunks(self, n, send, profile=GUSAN, spec_=None, **kw):
        audio, chunks = pieces(n)
        t = FakeTime()
        kw.setdefault("sleep", t.sleep)
        kw.setdefault("clock", t.clock)
        res = cf.transcribe_chunks(audio, chunks, spec_ or spec(), profile, send=send, **kw)
        return res, t

    def test_answers_come_back_in_file_order(self):
        def send(spec_, wav):
            i = piece_of(wav)
            time.sleep(0.003 * (10 - i))                      # later pieces answer first
            return (f"Piece number {i}.", None)
        res, _ = self.run_chunks(10, send)
        self.assertEqual([c.index for c, _, _ in res["pieces"]], list(range(10)))
        self.assertEqual([t for _, t, _ in res["pieces"]], [f"Piece number {i}." for i in range(10)])
        self.assertEqual(res["stats"]["requests"], 10)
        self.assertEqual(res["stats"]["retries"], 0)

    def test_never_more_than_the_workers_at_once(self):
        lock, active, most = threading.Lock(), [0], [0]

        def send(spec_, wav):
            with lock:
                active[0] += 1
                most[0] = max(most[0], active[0])
            time.sleep(0.02)
            with lock:
                active[0] -= 1
            return "Words."
        self.run_chunks(12, send, OWN)
        self.assertLessEqual(most[0], OWN.workers)
        self.assertGreater(most[0], 0)

    def test_retries_back_off_then_succeed(self):
        calls = collections.Counter()

        def send(spec_, wav):
            i = piece_of(wav)
            calls[i] += 1
            if i == 0 and calls[i] <= 2:
                raise err("server" if calls[i] == 1 else "timeout")
            return "Some words."
        res, t = self.run_chunks(3, send, ONE)
        self.assertEqual(calls[0], 3)
        self.assertEqual(res["stats"]["retries"], 2)
        self.assertEqual(res["stats"]["requests"], 5)
        self.assertEqual(len(t.sleeps), 2)
        self.assertTrue(1.4 <= t.sleeps[0] <= 2.6, t.sleeps)
        self.assertTrue(4.2 <= t.sleeps[1] <= 7.8, t.sleeps)

    def test_retry_after_is_honoured(self):
        calls = collections.Counter()

        def send(spec_, wav):
            calls[piece_of(wav)] += 1
            if calls[0] == 1:
                raise err("server", retry_after=12)
            return "Words."
        _, t = self.run_chunks(1, send, ONE)
        self.assertEqual(t.sleeps, [12])

    def test_gives_up_after_four_tries(self):
        calls = collections.Counter()

        def send(spec_, wav):
            calls[piece_of(wav)] += 1
            raise err("timeout")
        with self.assertRaises(cf.CloudFileError) as ctx:
            self.run_chunks(2, send, ONE)
        self.assertNotIsInstance(ctx.exception, cf.CloudUnavailable)
        self.assertEqual(ctx.exception.kind, "timeout")
        self.assertEqual(ctx.exception.message, cs.error_message("timeout", "Gusan Cloud"))
        self.assertEqual(calls[0], cf.MAX_ATTEMPTS)
        self.assertEqual(calls[1], 0)

    # -- another script: kept for a capable model after the retry, else the piece fails
    @staticmethod
    def off_err(model, why="script", text="We will review it on Monday."):
        e = err("off_language")
        e.why, e.model, e.answer = why, model, (text, "en", model)
        return e

    def test_a_capable_model_that_writes_another_script_twice_is_kept(self):
        calls = collections.Counter()

        def send(spec_, wav):
            calls[piece_of(wav)] += 1
            if piece_of(wav) == 0:
                raise self.off_err("gpt-transcribe")
            return "Words."
        res, _ = self.run_chunks(2, send, ONE)
        self.assertEqual(calls[0], 2)                         # the first answer, one retry, then kept
        self.assertEqual([t for _, t, _ in res["pieces"]], ["We will review it on Monday.", "Words."])
        self.assertEqual(res["stats"]["retries"], 1)

    def test_another_script_once_then_a_good_answer_is_the_good_answer(self):
        calls = collections.Counter()

        def send(spec_, wav):
            calls[piece_of(wav)] += 1
            if calls[0] == 1:
                raise self.off_err("scribe_v2")
            return "Good words."
        res, _ = self.run_chunks(1, send, ONE)
        self.assertEqual(calls[0], 2)
        self.assertEqual([t for _, t, _ in res["pieces"]], ["Good words."])

    def test_an_engine_that_does_not_cover_the_language_still_fails_the_piece(self):
        for model, why in (("voxtral-mini-latest", "engine"), ("voxtral-mini-latest", "script"), ("", "script")):
            calls = collections.Counter()

            def send(spec_, wav, model=model, why=why):
                calls[piece_of(wav)] += 1
                raise self.off_err(model, why)
            with self.assertRaises(cf.CloudFileError) as ctx:
                self.run_chunks(1, send, ONE)
            self.assertEqual(ctx.exception.kind, "off_language", (model, why))
            self.assertEqual(calls[0], cf.MAX_ATTEMPTS, (model, why))

    def test_a_rate_limit_leaves_one_worker(self):
        lock, active, seen, limited = threading.Lock(), set(), [], threading.Event()

        def send(spec_, wav):
            i = piece_of(wav)
            with lock:
                if i == 0 and not limited.is_set():
                    limited.set()
                    raise err("rate_limit", name="Google Gemini")
                active.add(i)
                seen.append((i, frozenset(active - {i})))
            try:
                if 1 <= i <= 3:                               # in flight when the limit hits
                    limited.wait(5)
                    time.sleep(0.1)
                return "Words."
            finally:
                with lock:
                    active.discard(i)
        res, t = self.run_chunks(12, send, GUSAN, spec("google", api_key="k"))
        self.assertEqual(res["stats"]["retries"], 1)
        self.assertTrue(1.4 <= t.sleeps[0] <= 2.6, t.sleeps)
        later = [others for i, others in seen if i >= 4]
        self.assertEqual(len(later), 8)
        self.assertTrue(all(not others for others in later))

    def test_a_fatal_error_stops_the_rest(self):
        sent = []

        def send(spec_, wav):
            i = piece_of(wav)
            sent.append(i)
            if i == 3:
                raise err("bad_key", name="Google Gemini")
            time.sleep(0.01)
            return "Words."
        stop = threading.Event()
        with self.assertRaises(cf.CloudFileError) as ctx:
            self.run_chunks(20, send, OWN, spec("google", api_key="k"), stop=stop)
        self.assertEqual(ctx.exception.kind, "bad_key")
        self.assertEqual(ctx.exception.message, cs.error_message("bad_key", "Google Gemini"))
        self.assertTrue(stop.is_set())
        time.sleep(0.1)
        self.assertLess(len(sent), 20)

    def test_gusan_unusable_at_the_first_piece_goes_local(self):
        for kind in sorted(cs.LOCAL_FALLBACK_KINDS):
            def send(spec_, wav, kind=kind):
                raise err(kind)
            with self.assertRaises(cf.CloudUnavailable) as ctx:
                self.run_chunks(3, send, ONE)
            self.assertEqual(ctx.exception.kind, kind)

    def test_gusan_unusable_later_fails_the_job(self):
        def send(spec_, wav):
            if piece_of(wav) == 1:
                raise err("cap")
            return "Words."
        with self.assertRaises(cf.CloudFileError) as ctx:
            self.run_chunks(3, send, ONE)
        self.assertNotIsInstance(ctx.exception, cf.CloudUnavailable)
        self.assertEqual(ctx.exception.message, cs.error_message("cap", "Gusan Cloud"))

    def test_other_first_piece_errors_never_go_local(self):
        cases = (("gusan", "signed_out"), ("gusan", "bad_request"), ("google", "bad_key"),
                 ("mistral", "no_credit"))
        for engine, kind in cases:
            def send(spec_, wav, kind=kind):
                raise err(kind)
            with self.assertRaises(cf.CloudFileError) as ctx:
                self.run_chunks(2, send, ONE, spec(engine, api_key="k"))
            self.assertNotIsInstance(ctx.exception, cf.CloudUnavailable, kind)
            self.assertEqual(ctx.exception.kind, kind)

    def test_an_unexpected_error_gets_a_plain_sentence(self):
        def send(spec_, wav):
            raise ValueError("https://example.test/?key=secret")
        with self.assertLogs("transcribe.cloud_files", "ERROR"):
            with self.assertRaises(cf.CloudFileError) as ctx:
                self.run_chunks(1, send, ONE)
        self.assertEqual(ctx.exception.message, cs.error_message("error", "Gusan Cloud"))
        self.assertNotIn("secret", ctx.exception.message)

    def test_cancel_returns_quickly_and_drops_late_answers(self):
        gate = threading.Event()

        def send(spec_, wav):
            gate.wait(10)
            return "Late words."
        cancel = threading.Event()
        threading.Timer(0.3, cancel.set).start()
        progress, stop = [], threading.Event()
        t0 = time.monotonic()
        try:
            with self.assertRaises(cf.Cancelled):
                self.run_chunks(6, send, stop=stop, cancelled=cancel.is_set,
                                on_progress=lambda f, e: progress.append(f))
            self.assertLess(time.monotonic() - t0, 2.0)
            self.assertTrue(stop.is_set())
        finally:
            gate.set()
        time.sleep(0.2)
        self.assertEqual(progress, [])

    def test_cancel_ends_a_back_off_at_once(self):
        def send(spec_, wav):
            raise err("server", retry_after=30)
        cancel = threading.Event()
        threading.Timer(0.3, cancel.set).start()
        audio, chunks = pieces(2)
        t0 = time.monotonic()
        with self.assertRaises(cf.Cancelled):
            cf.transcribe_chunks(audio, chunks, spec(), ONE, send=send, cancelled=cancel.is_set)
        self.assertLess(time.monotonic() - t0, 2.0)

    def test_empty_pieces_are_counted(self):
        def send(spec_, wav):
            return "" if piece_of(wav) == 1 else ("Words here.", None)
        res, _ = self.run_chunks(3, send)
        self.assertEqual(res["stats"]["empty"], 1)
        self.assertEqual([t for _, t, _ in res["pieces"]], ["Words here.", "", "Words here."])

    def test_every_piece_empty_is_no_speech(self):
        with self.assertRaises(cf.NoSpeech):
            self.run_chunks(4, lambda spec_, wav: ("  ", None))

    def test_no_pieces_no_requests(self):
        calls = []
        with self.assertRaises(cf.NoSpeech):
            cf.transcribe_chunks(np.zeros(SR, dtype=np.float32), [], spec(), GUSAN,
                                 send=lambda s, w: calls.append(1))
        self.assertEqual(calls, [])

    def test_progress_rises_to_the_end(self):
        t = FakeTime()

        def send(spec_, wav):
            t.advance(3.0)                                    # each answer takes 3 s
            return "Words."
        rec = []
        audio, chunks = pieces(12)
        cf.transcribe_chunks(audio, chunks, spec(), GUSAN, send=send, sleep=t.sleep, clock=t.clock,
                             on_progress=lambda f, e: rec.append((f, e)))
        fracs = [f for f, _ in rec]
        self.assertEqual(fracs, sorted(fracs))
        self.assertEqual(fracs[-1], 1.0)
        self.assertTrue(all(e is None or e >= 0 for _, e in rec))

    def test_eta_waits_for_the_first_pieces_and_5_seconds(self):
        t = FakeTime()

        def send(spec_, wav):
            t.advance(2.0)
            return "Words."
        rec = []
        audio, chunks = pieces(8)
        cf.transcribe_chunks(audio, chunks, spec(), ONE, send=send, sleep=t.sleep, clock=t.clock,
                             on_progress=lambda f, e: rec.append((f, e)))
        self.assertEqual([e for _, e in rec[:2]], [None, None])     # 2 s and 4 s in
        self.assertIsNotNone(rec[2][1])
        self.assertEqual(rec[-1], (1.0, 0.0))

    def test_check_runs_before_every_send(self):
        cfg = {"privacy_mode": False}
        s = spec(cfg=cfg)
        sent = []

        def send(spec_, wav):
            sent.append(piece_of(wav))
            if len(sent) == 2:
                cfg["privacy_mode"] = True
            return "Words."
        with self.assertRaises(cf.CloudFileError) as ctx:
            self.run_chunks(6, send, ONE, s, check=cf._live_check(s, None))
        self.assertEqual(ctx.exception.kind, "privacy")
        self.assertEqual(sent, [0, 1])

    def test_answers_are_fixed_on_this_computer(self):
        res, _ = self.run_chunks(2, lambda s, w: ("We use pyside6 here.", None),
                                 fix_text=lambda text: text.replace("pyside6", "PySide6"))
        self.assertEqual([t for _, t, _ in res["pieces"]], ["We use PySide6 here."] * 2)


@unittest.skipUnless(_real_numpy(), "real numpy not importable (stubbed)")
class TestTranscribeFile(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(cf, "_vad_model", return_value=None)   # loudness, no model
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_no_speech_sends_nothing(self):
        calls = []
        with self.assertRaises(cf.NoSpeech):
            cf.transcribe_file(np.zeros(60 * SR, dtype=np.float32), spec(),
                               send=lambda s, w: calls.append(1))
        self.assertEqual(calls, [])

    def test_segments_progress_and_spellings(self):
        audio = talk(400, long_gap_at=200)
        emits, calls = [], []
        lock = threading.Lock()

        def send(spec_, wav):
            with lock:
                calls.append(len(wav))
            return ("We use pyside6 here. It works well for us, thanks. Another sentence follows now.",
                    None)
        res = cf.transcribe_file(audio, spec(spellings=("PySide6",)), send=send,
                                 emit=lambda pct, stage, eta: emits.append((pct, stage, eta)),
                                 start_pct=6, end_pct=78)
        self.assertGreaterEqual(len(calls), 4)
        self.assertTrue(all(n <= 44 + 2 * SR * GUSAN.hard_max for n in calls))
        segs = res["segments"]
        self.assertEqual(len(segs), 3 * len(calls))
        prev = 0.0
        for s in segs:
            self.assertLessEqual(prev, s["start"])
            self.assertLessEqual(s["start"], s["end"])
            prev = s["end"]
        self.assertLessEqual(prev, len(audio) / SR)
        self.assertTrue(segs[0]["text"].startswith("We use PySide6 here."))
        self.assertFalse(any("pyside6" in s["text"] for s in segs))
        pcts = [p for p, _, _ in emits]
        self.assertEqual(pcts, sorted(pcts))
        self.assertEqual((pcts[0], pcts[-1]), (6, 78))
        self.assertEqual({st for _, st, _ in emits}, {"Transcribing"})
        self.assertEqual(res["model_id"], "gemini-2.5-flash")
        self.assertIsNone(res["language"])
        self.assertEqual(res["stats"]["pieces"], len(calls))

    def test_language(self):
        audio = talk(600, seed=2)
        n = [0]

        def send(spec_, wav):
            n[0] += 1
            return ("Bonjour à tous, merci.", "en" if n[0] == 1 else "fr")
        res = cf.transcribe_file(audio, spec("mistral", api_key="k"), send=send)
        self.assertEqual(res["language"], "fr")
        res = cf.transcribe_file(audio, spec("mistral", api_key="k", language="de"), send=send)
        self.assertEqual(res["language"], "de")

    def test_privacy_mode_turned_on_stops_sending(self):
        cfg = {"privacy_mode": False, "mistral_api_key": "k"}
        calls = []

        def send(spec_, wav):
            calls.append(1)
            cfg["privacy_mode"] = True
            return "Words."
        with self.assertRaises(cf.CloudFileError) as ctx:
            cf.transcribe_file(talk(600, seed=3), spec("mistral", api_key="k", cfg=cfg), send=send)
        self.assertEqual(ctx.exception.kind, "privacy")
        self.assertEqual(ctx.exception.message, cs.error_message("privacy", "Mistral"))
        self.assertLessEqual(len(calls), cf.PROFILES["mistral"].workers)

    def test_removed_key_stops_sending(self):
        cfg = {"google_api_key": "k"}

        def send(spec_, wav):
            cfg["google_api_key"] = ""
            return "Words."
        with self.assertRaises(cf.CloudFileError) as ctx:
            cf.transcribe_file(talk(600, seed=4), spec("google", api_key="k"), send=send,
                               cfg_fn=lambda: cfg)
        self.assertEqual(ctx.exception.kind, "no_key")
        self.assertEqual(ctx.exception.message, cs.error_message("no_key", "Google Gemini"))

    def test_daily_cap_at_the_first_piece(self):
        def send(spec_, wav):
            raise err("cap")
        with self.assertRaises(cf.CloudUnavailable):
            cf.transcribe_file(talk(120, seed=5), spec(), send=send)

    def test_cancel(self):
        with self.assertRaises(cf.Cancelled):
            cf.transcribe_file(talk(120, seed=6), spec(), send=lambda s, w: "Words.",
                               cancelled=lambda: True)


if __name__ == "__main__":
    unittest.main()
