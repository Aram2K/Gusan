"""audio_export's helpers for the in-app meeting player (ui/components/audio_player.py):
part lengths from WAV headers, where a position falls across parts, and the
waveform's peaks. The WAV files are written with the stdlib here, so the
length and position tests run under tests/test_core.py's numpy stub too."""
import array
import math
import os
import tempfile
import time
import unittest
import wave
from pathlib import Path

import audio_export as ax


def _real_numpy(np=None):
    try:
        if np is None:
            import numpy as np
        return np.__name__ == "numpy" and tuple(np.zeros(2).shape) == (2,)
    except Exception:
        return False


def _wav(path, seconds=0.0, rate=16000, level=0.5, loud=None, width=2, channels=1):
    """A sine WAV; ``loud`` = (start s, end s) keeps the tone there only (silence elsewhere)."""
    n = int(round(seconds * rate))
    peak = (2 ** (8 * width - 1) - 1) * level
    samples = []
    for i in range(n):
        t = i / float(rate)
        on = loud is None or loud[0] <= t < loud[1]
        v = int(peak * math.sin(2 * math.pi * 440.0 * t)) if on else 0
        samples.extend([v] * channels)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(width)
        w.setframerate(rate)
        if width == 2:
            w.writeframes(array.array("h", samples).tobytes())
        elif width == 1:
            w.writeframes(bytes((v + 128) & 0xFF for v in samples))
        else:
            w.writeframes(array.array("i", samples).tobytes())
    return Path(path)


class TestPartDurations(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_lengths_in_order_with_bad_parts_as_zero(self):
        a = _wav(self.dir / "audio_part1.wav", 1.5)
        b = _wav(self.dir / "audio_part2.wav", 0.25, rate=8000)
        empty = _wav(self.dir / "audio_part3.wav", 0)
        junk = self.dir / "audio_part4.wav"
        junk.write_bytes(b"not a wav")
        missing = self.dir / "audio_part5.wav"
        self.assertEqual(ax.part_durations_ms([a, b, empty, junk, missing]), [1500, 250, 0, 0, 0])
        self.assertEqual(ax.part_durations_ms([]), [])
        self.assertEqual(ax.part_durations_ms(None), [])

    def test_a_part_cut_short_counts_what_it_holds(self):
        # A crash mid-write: the header claims 2 s, the file holds about 0.5 s.
        p = _wav(self.dir / "audio_part1.wav", 2.0)
        data = p.read_bytes()
        p.write_bytes(data[: 44 + 16000 * 2 // 2])
        self.assertEqual(ax.part_durations_ms([p]), [500])

    def test_stereo_and_other_rates(self):
        p = _wav(self.dir / "s.wav", 1.0, rate=44100, channels=2)
        self.assertEqual(ax.part_durations_ms([p]), [1000])

    def test_matches_duration_seconds(self):
        a = _wav(self.dir / "a.wav", 0.75)
        b = _wav(self.dir / "b.wav", 1.25)
        self.assertEqual(sum(ax.part_durations_ms([a, b])) / 1000.0, ax.duration_seconds([a, b]))


class TestLocate(unittest.TestCase):

    def test_positions_map_to_their_part(self):
        durs = [60000, 30000]
        self.assertEqual(ax.locate(durs, 0), (0, 0))
        self.assertEqual(ax.locate(durs, 59999), (0, 59999))
        self.assertEqual(ax.locate(durs, 60000), (1, 0))       # the boundary is the next part's start
        self.assertEqual(ax.locate(durs, 75000), (1, 15000))
        self.assertEqual(ax.locate(durs, 90000), (1, 30000))   # the very end
        self.assertEqual(ax.locate(durs, 10 ** 9), (1, 30000))
        self.assertEqual(ax.locate(durs, -5), (0, 0))

    def test_empty_parts_are_skipped(self):
        self.assertEqual(ax.locate([0, 5000, 0, 2000], 0), (1, 0))
        self.assertEqual(ax.locate([0, 5000, 0, 2000], 5000), (3, 0))
        self.assertEqual(ax.locate([0, 5000, 0, 2000], 7000), (3, 2000))
        self.assertEqual(ax.locate([5000, 0], 9999), (0, 5000))

    def test_nothing_playable(self):
        self.assertEqual(ax.locate([], 100), (-1, 0))
        self.assertEqual(ax.locate([0, 0], 100), (-1, 0))
        self.assertEqual(ax.locate(None, 0), (-1, 0))


@unittest.skipUnless(_real_numpy(), "real numpy not importable (stubbed)")
class TestPeaks(unittest.TestCase):

    def setUp(self):
        # tests/test_core.py may stub numpy after this file was collected:
        # use the numpy audio_export itself holds.
        if not _real_numpy(getattr(ax, "np", None)):
            self.skipTest("audio_export holds a numpy stub")
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_loud_and_quiet_bars(self):
        # 4 s: the tone plays in the 2nd second only.
        p = _wav(self.dir / "a.wav", 4.0, loud=(1.0, 2.0))
        pk = ax.peaks([p], 8)
        self.assertEqual(len(pk), 8)
        self.assertTrue(all(0.0 <= v <= 1.0 for v in pk))
        self.assertEqual(max(pk), 1.0)
        self.assertGreater(min(pk[2], pk[3]), 0.9)               # 1.0-2.0 s
        for i in (0, 1, 4, 5, 6, 7):
            self.assertLess(pk[i], 0.05, i)

    def test_parts_are_one_recording(self):
        a = _wav(self.dir / "audio_part1.wav", 2.0, level=0.0)   # silence
        b = _wav(self.dir / "audio_part2.wav", 2.0, level=0.5)   # the tone
        pk = ax.peaks([a, b], 4)
        self.assertEqual(pk[:2], [0.0, 0.0])
        self.assertGreater(min(pk[2:]), 0.9)

    def test_bad_parts_and_edges(self):
        junk = self.dir / "junk.wav"
        junk.write_bytes(b"RIFF")
        self.assertEqual(ax.peaks([junk], 5), [0.0] * 5)
        self.assertEqual(ax.peaks([], 3), [0.0] * 3)
        self.assertEqual(ax.peaks([self.dir / "a.wav"], 0), [])
        silent = _wav(self.dir / "s.wav", 1.0, level=0.0)
        self.assertEqual(ax.peaks([silent], 4), [0.0] * 4)
        # A junk part next to a good one is left out.
        good = _wav(self.dir / "g.wav", 1.0)
        self.assertEqual(len(ax.peaks([junk, good], 6)), 6)
        self.assertGreater(min(ax.peaks([junk, good], 6)), 0.9)

    def test_more_bars_than_windows_and_other_widths(self):
        short = _wav(self.dir / "short.wav", 0.05)                # shorter than one window
        self.assertEqual(len(ax.peaks([short], 34)), 34)
        self.assertGreater(min(ax.peaks([short], 34)), 0.9)
        for width in (1, 4):
            p = _wav(self.dir / ("w%d.wav" % width), 1.0, width=width)
            pk = ax.peaks([p], 4)
            self.assertGreater(min(pk), 0.9, width)
        stereo = _wav(self.dir / "st.wav", 1.0, channels=2)
        self.assertGreater(min(ax.peaks([stereo], 4)), 0.9)

    def test_cost_does_not_grow_with_the_length(self):
        # One ~100 ms window per bar: a 5 min part reads in a blink.
        np = ax.np
        p = self.dir / "long.wav"
        ax.write_wav(p, np.full(16000 * 60 * 5, 0.25, dtype=np.float32))
        t0 = time.perf_counter()
        pk = ax.peaks([p], 34)
        took = time.perf_counter() - t0
        self.assertEqual(pk, [1.0] * 34)
        self.assertLess(took, 0.5)
        self.assertEqual(ax.part_durations_ms([p]), [5 * 60 * 1000])
        os.remove(p)

    def test_write_wav_and_export_unchanged(self):
        np = ax.np
        a = self.dir / "a.wav"
        b = self.dir / "b.wav"
        ax.write_wav(a, np.zeros(16000, dtype=np.float32))
        ax.write_wav(b, np.zeros(8000, dtype=np.float32))
        out = ax.export_recording([a, b], self.dir / "out.wav")
        self.assertEqual(ax.part_durations_ms([out]), [1500])


if __name__ == "__main__":
    unittest.main()
