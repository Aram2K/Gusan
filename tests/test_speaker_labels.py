"""Who spoke each line of a mic + system sound meeting (speaker_labels.py): the
source-dominance labeller (echo-aware, with hysteresis), the diarization
clean-up for the call, and the whole attribution - on fabricated energy
envelopes, segments and turns (sherpa itself is never needed here).

Pure Python after the frame energies, so everything but frame_rms runs under
tests/test_core.py's numpy stub too.
"""
import random
import unittest

import diarization
import speaker_labels as sl

HOP = sl.HOP_SEC


def _real_numpy():
    try:
        import numpy as np
        return np.__name__ == "numpy" and tuple(np.zeros(2).shape) == (2,)
    except Exception:
        return False


def envelopes(spans, seconds, k=0.15, user=0.08, call=0.10, mic_noise=0.0008, sys_noise=0.0,
              rnd=None):
    """(mic_rms, sys_rms) per 50 ms frame for ``spans`` [(start, end, "you"|"them"|"both")].
    The call is heard on the mic as a quieter copy (k); the user is not on the system sound."""
    rnd = rnd or random.Random(0)
    n = int(seconds / HOP)
    mic = [mic_noise * (0.5 + rnd.random()) for _ in range(n)]
    sysm = [sys_noise * (0.5 + rnd.random()) for _ in range(n)]
    for start, end, who in spans:
        for i in range(int(start / HOP), min(n, int(end / HOP))):
            j = 0.8 + 0.4 * rnd.random()
            if who in ("you", "both"):
                mic[i] += user * j
            if who in ("them", "both"):
                sysm[i] += call * j
                mic[i] += k * call * j
    return mic, sysm


def segs_of(spans, jitter=0.0, rnd=None):
    rnd = rnd or random.Random(1)
    out = []
    for start, end, _who in spans:
        out.append({"start": max(0.0, start + rnd.uniform(-jitter, jitter)),
                    "end": end + rnd.uniform(-jitter, jitter), "text": "words"})
    return out


class TestFrameRms(unittest.TestCase):
    @unittest.skipUnless(_real_numpy(), "real numpy not importable (stubbed)")
    def test_int16_and_float_tracks_give_the_same_levels(self):
        import numpy as np
        t = np.arange(16000 * 2, dtype=np.float32) / 16000
        x = (0.5 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
        a = sl.frame_rms(x)
        b = sl.frame_rms(np.rint(x * 32767).astype(np.int16))
        self.assertEqual(len(a), 40)
        self.assertEqual(len(a), len(b))
        self.assertAlmostEqual(a[3], 0.5 / 2 ** 0.5, places=3)
        self.assertTrue(all(abs(p - q) < 1e-3 for p, q in zip(a, b)))

    @unittest.skipUnless(_real_numpy(), "real numpy not importable (stubbed)")
    def test_a_short_tail_and_empty_input(self):
        import numpy as np
        out = sl.frame_rms(np.full(800 * 3 + 100, 0.2, np.float32))
        self.assertEqual(len(out), 4)                         # the last, shorter frame counts too
        self.assertAlmostEqual(out[-1], 0.2, places=4)
        self.assertEqual(sl.frame_rms(np.zeros(0, np.int16)), [])


class TestFrames(unittest.TestCase):
    def test_threshold_follows_the_tracks_own_floor_and_never_drops_below_the_minimum(self):
        self.assertEqual(sl.activity_threshold([0.0] * 100), sl.ABS_MIN)
        noisy = [0.02] * 100
        self.assertAlmostEqual(sl.activity_threshold(noisy), 0.08)

    def test_the_user_and_the_call_are_told_apart_with_no_echo(self):
        spans = [(1, 5, "you"), (6, 10, "them"), (11, 14, "you")]
        mic, sysm = envelopes(spans, 15, k=0.0)
        labels, info = sl.classify_frames(mic, sysm)
        got = lambda a, b: set(labels[int(a / HOP):int(b / HOP)])
        self.assertEqual(got(1.2, 4.8), {sl.YOU_F})
        self.assertEqual(got(6.2, 9.8), {sl.THEM_F})
        self.assertEqual(got(11.2, 13.8), {sl.YOU_F})
        self.assertEqual(got(0.0, 0.8), {sl.NONE})

    def test_the_echo_of_the_call_on_the_microphone_is_not_the_user(self):
        for k in (0.05, 0.15, 0.4, 0.8):
            spans = [(1, 6, "them"), (7, 11, "you"), (12, 18, "them")]
            mic, sysm = envelopes(spans, 20, k=k, user=0.08, call=0.10)
            labels, info = sl.classify_frames(mic, sysm)
            self.assertEqual(set(labels[int(1.2 / HOP):int(5.8 / HOP)]), {sl.THEM_F}, k)
            self.assertEqual(set(labels[int(7.2 / HOP):int(10.8 / HOP)]), {sl.YOU_F}, k)
            self.assertEqual(set(labels[int(12.2 / HOP):int(17.8 / HOP)]), {sl.THEM_F}, k)
            # The coupling it learned is about the true one.
            self.assertAlmostEqual(info["k"], k, delta=max(0.05, k * 0.35))

    def test_the_user_talking_over_the_call_is_heard_as_you_when_clearly_louder(self):
        spans = [(1, 8, "them"), (3, 6, "you")]
        mic, sysm = envelopes(spans, 10, k=0.1, user=0.2, call=0.1)
        labels, _ = sl.classify_frames(mic, sysm)
        self.assertEqual(set(labels[int(3.3 / HOP):int(5.7 / HOP)]), {sl.YOU_F})
        self.assertEqual(set(labels[int(1.2 / HOP):int(2.8 / HOP)]), {sl.THEM_F})

    def test_a_label_does_not_flicker_between_the_two_thresholds(self):
        n = 200
        sysm = [0.1] * n
        # mic sits between "echo" and "you": 1.5x..2.5x the expected echo (k=0.15 -> 0.015)
        mic = [0.015 * (1.6 + 0.8 * (i % 2)) for i in range(n)]
        labels, info = sl.classify_frames(mic, sysm, k=0.15)
        changes = sum(1 for a, b in zip(labels, labels[1:]) if a != b)
        self.assertLessEqual(changes, 1)

    def test_smoothing_drops_a_single_frame_blip_but_keeps_a_real_change(self):
        labels = [sl.THEM_F] * 20 + [sl.YOU_F] + [sl.THEM_F] * 20 + [sl.YOU_F] * 12
        out = sl.smooth_labels(labels)
        self.assertEqual(out[20], sl.THEM_F)
        self.assertEqual(set(out[44:53]), {sl.YOU_F})
        self.assertEqual(sl.smooth_labels([sl.NONE] * 5), [sl.NONE] * 5)

    def test_unequal_track_lengths_and_empty_tracks_do_not_raise(self):
        labels, _ = sl.classify_frames([0.1] * 10, [0.1] * 4)
        self.assertEqual(len(labels), 10)
        labels, _ = sl.classify_frames([], [])
        self.assertEqual(labels, [])


class TestSegments(unittest.TestCase):
    def test_each_segment_goes_to_the_source_that_carried_it(self):
        spans = [(1, 4, "you"), (5, 9, "them"), (10, 12, "you"), (13, 20, "them")]
        mic, sysm = envelopes(spans, 22, k=0.2)
        segs = segs_of(spans, jitter=0.25)
        labs = sl.label_segments(segs, mic, sysm)
        self.assertEqual([l["source"] for l in labs], ["you", "them", "you", "them"])
        self.assertTrue(all(l["basis"] == "frames" for l in labs))

    def test_a_tie_keeps_the_previous_speaker(self):
        # 3 s of you then a segment that is half you / half them
        spans = [(1, 4, "you"), (5, 7, "you"), (7, 9, "them")]
        mic, sysm = envelopes(spans, 11, k=0.1)
        segs = [{"start": 1, "end": 4, "text": "a"}, {"start": 5, "end": 9, "text": "b"}]
        labs = sl.label_segments(segs, mic, sysm)
        self.assertEqual(labs[0]["source"], "you")
        self.assertEqual(labs[1]["basis"], "hysteresis")
        self.assertEqual(labs[1]["source"], "you")

    def test_a_segment_over_silence_takes_the_previous_label_or_the_call(self):
        spans = [(1, 3, "them")]
        mic, sysm = envelopes(spans, 10)
        segs = [{"start": 1, "end": 3, "text": "a"}, {"start": 6, "end": 8, "text": "b"}]
        labs = sl.label_segments(segs, mic, sysm)
        self.assertEqual((labs[0]["source"], labs[1]["source"]), ("them", "them"))
        self.assertEqual(labs[1]["basis"], "inherit")
        first = sl.label_segments([{"start": 6, "end": 8, "text": "b"}], mic, sysm)
        self.assertEqual((first[0]["source"], first[0]["basis"]), ("them", "none"))

    def test_very_quiet_speech_is_compared_against_each_tracks_own_floor(self):
        n = 400
        mic = [0.0005] * n
        sysm = [0.0] * n
        for i in range(100, 160):
            mic[i] = 0.0035            # whispered: under the activity threshold
        for i in range(250, 300):
            sysm[i] = 0.0015           # a quiet call, under the call's own activity level
        segs = [{"start": 5.0, "end": 8.0, "text": "a"}, {"start": 12.5, "end": 15.0, "text": "b"}]
        labs = sl.label_segments(segs, mic, sysm)
        self.assertEqual([l["basis"] for l in labs], ["level", "level"])
        self.assertEqual([l["source"] for l in labs], ["you", "them"])

    def test_nothing_to_read_gives_the_call(self):
        labs = sl.label_segments([{"start": 0, "end": 1, "text": "a"}], [], [])
        self.assertEqual(labs[0]["source"], "them")
        self.assertEqual(sl.label_segments([], [0.1], [0.1]), [])

    def test_malformed_segments_never_raise(self):
        mic, sysm = envelopes([(1, 3, "you")], 5)
        bad = [{}, {"start": None}, {"start": "x", "end": "y"}, {"start": float("nan"), "end": 1.0},
               {"start": 5, "end": 2}, {"start": -3, "end": -1}, {"start": 1e9, "end": 2e9},
               {"start": 1, "end": 3, "text": None}]
        labs = sl.label_segments(bad, mic, sysm)
        self.assertEqual(len(labs), len(bad))
        self.assertTrue(all(l["source"] in ("you", "them") for l in labs))

    def test_nan_and_infinite_levels_never_raise(self):
        nan, inf = float("nan"), float("inf")
        mic = [0.1, nan, 0.1, inf, 0.0, nan] * 20
        sysm = [nan, 0.2, inf, 0.0, 0.2, 0.2] * 20
        segs = [{"start": 0.0, "end": 3.0, "text": "a"}, {"start": 2.0, "end": 6.0, "text": "b"}]
        labs = sl.label_segments(segs, mic, sysm)
        self.assertEqual(len(labs), 2)
        self.assertTrue(all(l["source"] in ("you", "them") for l in labs))
        self.assertEqual(len(sl.active_spans(sysm)) >= 0, True)

    def test_fuzz_random_envelopes_and_segments_never_raise(self):
        rnd = random.Random(2026)
        for trial in range(150):
            n = rnd.choice([0, 1, 5, 40, 400, 1500])
            kind = trial % 5
            if kind == 0:
                mic = [rnd.random() * 0.3 for _ in range(n)]
                sysm = [rnd.random() * 0.3 for _ in range(n)]
            elif kind == 1:
                mic = [0.0] * n
                sysm = [0.0] * n
            elif kind == 2:
                mic = [rnd.choice([0.0, 0.0, 0.2, 1.0]) for _ in range(n)]
                sysm = [rnd.choice([0.0, 0.3, 0.0, 1.0]) for _ in range(max(0, n - rnd.randint(0, 5)))]
            elif kind == 3:
                mic = [10 ** rnd.uniform(-9, 0) for _ in range(n)]
                sysm = [10 ** rnd.uniform(-9, 0) for _ in range(n)]
            else:
                mic = [rnd.random() * 0.01 for _ in range(n)]
                sysm = []
            segs = []
            for _ in range(rnd.randint(0, 40)):
                a = rnd.uniform(-5, n * HOP + 10)
                segs.append({"start": a, "end": a + rnd.uniform(-1, 15), "text": "t"})
            labs = sl.label_segments(segs, mic, sysm)
            self.assertEqual(len(labs), len(segs))
            for lab in labs:
                self.assertIn(lab["source"], ("you", "them"))
                self.assertTrue(0.0 <= lab["you_share"] <= 1.0)
            turns = [(rnd.uniform(0, 60), rnd.uniform(0, 60), rnd.randint(0, 5)) for _ in range(rnd.randint(0, 12))]
            out = sl.attribute(segs, mic, sysm, turns)
            self.assertEqual(len(out), len(segs))
            for row in out:
                self.assertTrue(row["speaker"] == sl.YOU or row["speaker"] == sl.THEM
                                or (isinstance(row["speaker"], int) and row["speaker"] >= 0))


class TestCallOnlyDiarization(unittest.TestCase):
    """The diarizer is given only the stretches where the call made sound."""

    def test_active_spans_pad_bridge_and_drop_blips(self):
        n = 400                                                   # 20 s
        rms = [0.0] * n
        for i in range(20, 60):                                   # 1.0 - 3.0 s
            rms[i] = 0.1
        for i in range(70, 100):                                  # 3.5 - 5.0 s: 0.5 s after the first
            rms[i] = 0.1
        rms[200] = 0.1                                            # a 50 ms click
        for i in range(300, 340):                                 # 15.0 - 17.0 s
            rms[i] = 0.1
        spans = sl.active_spans(rms)
        self.assertEqual(len(spans), 2)                           # the first two are one stretch
        self.assertAlmostEqual(spans[0][0], 0.6)                  # 1.0 s less the 0.4 s margin
        self.assertAlmostEqual(spans[0][1], 5.4)
        self.assertAlmostEqual(spans[1][0], 14.6)
        self.assertEqual(sl.active_spans([]), [])
        self.assertEqual(sl.active_spans([0.0] * 50), [])

    @unittest.skipUnless(_real_numpy(), "real numpy not importable (stubbed)")
    def test_the_stretches_join_into_one_stream_and_turns_map_back(self):
        import numpy as np
        sr = 16000
        n = sr * 60
        system = np.zeros(n, np.int16)
        system[sr * 10:sr * 14] = 8000                            # call talks 10-14 s
        system[sr * 40:sr * 45] = 8000                            # and 40-45 s
        rms = sl.frame_rms(system)
        audio, mapping = sl.compact_system([(system, rms, 0.0)])
        self.assertEqual(audio.dtype, np.float32)
        self.assertLess(len(audio) / sr, 15.0)                     # not the whole minute
        self.assertEqual(len(mapping), 2)
        # A "turn" the diarizer finds in the joined stream is back on the meeting's timeline.
        c0, c1, origin = mapping[1]
        turns = [(c0 + 0.5, c0 + 3.0, 7)]
        back = sl.expand_turns(turns, mapping)
        self.assertEqual(len(back), 1)
        self.assertAlmostEqual(back[0][0], origin + 0.5, places=3)
        self.assertAlmostEqual(origin, 39.6, places=1)             # the second stretch, with its margin
        self.assertEqual(back[0][2], 7)
        # A turn across the join is cut there; one in the gap between is dropped.
        both = sl.expand_turns([(mapping[0][1] - 1.0, mapping[1][0] + 1.0, 3)], mapping)
        self.assertEqual(len(both), 2)
        self.assertEqual(sl.expand_turns([(mapping[0][1] + 0.05, mapping[0][1] + 0.1, 3)], mapping), [])

    @unittest.skipUnless(_real_numpy(), "real numpy not importable (stubbed)")
    def test_parts_keep_their_place_on_the_meetings_timeline(self):
        import numpy as np
        sr = 16000
        a = np.zeros(sr * 20, np.int16)
        a[sr * 5:sr * 8] = 9000
        b = np.zeros(sr * 20, np.int16)
        b[sr * 2:sr * 6] = 9000
        audio, mapping = sl.compact_system([(a, sl.frame_rms(a), 0.0), (b, sl.frame_rms(b), 100.0)])
        self.assertEqual(len(mapping), 2)
        self.assertAlmostEqual(mapping[1][2], 100.0 + 1.6, places=2)     # part two starts at 100 s
        self.assertIsNone(sl.compact_system([(np.zeros(sr, np.int16), [0.0] * 20, 0.0)])[0])
        self.assertIsNone(sl.compact_system([(None, [], 0.0)])[0])


class TestMinorSpeakers(unittest.TestCase):
    def test_a_one_person_call_split_into_noise_speakers_becomes_one(self):
        turns = [(0, 40, 0), (40, 41.2, 1), (41.2, 90, 0), (60, 61.0, 2), (95, 96.1, 3), (96.1, 150, 0)]
        out = sl.merge_minor_speakers(turns)
        self.assertEqual({i for _s, _e, i in out}, {0})
        self.assertEqual(len(out), len(turns))

    def test_two_real_speakers_are_kept(self):
        turns = [(0, 30, 0), (30, 55, 1), (55, 90, 0), (90, 120, 1)]
        self.assertEqual(sl.merge_minor_speakers(turns), sorted(turns))

    def test_a_short_interjection_takes_the_neighbours_voice(self):
        turns = [(0, 20, 0), (20, 22, 1), (22, 60, 2), (60, 100, 0), (100, 140, 2)]
        out = sl.merge_minor_speakers(turns)
        self.assertEqual({i for _s, _e, i in out}, {0, 2})
        # the 2 s turn sits next to speaker 0 (before) and 2 (after, adjacent): ties go to the earlier one
        self.assertEqual(out[1][2], 0)

    def test_everybody_minor_or_nobody_minor_is_left_alone(self):
        tiny = [(0, 1, 0), (2, 3, 1)]
        self.assertEqual(sl.merge_minor_speakers(tiny), tiny)
        self.assertEqual(sl.merge_minor_speakers([]), [])
        self.assertEqual(sl.merge_minor_speakers([(5, 5, 0), (3, 2, 1)]), [])     # empty turns dropped

    def test_a_speaker_with_a_fair_share_survives_in_a_long_call(self):
        turns = [(0, 1500, 0), (1500, 1540, 1)]       # 40 s of 1540: over the 30 s cap
        self.assertEqual({i for _s, _e, i in sl.merge_minor_speakers(turns)}, {0, 1})


class TestAttribution(unittest.TestCase):
    def _transcript(self, spans, turns, **kw):
        mic, sysm = envelopes(spans, max(e for _s, e, _w in spans) + 2, **kw)
        segs = segs_of(spans)
        for i, s in enumerate(segs):
            s["text"] = "line %d" % i
        labeled = sl.attribute(segs, mic, sysm, turns)
        return diarization.build_speaker_transcript(labeled, names=sl.speaker_names()), labeled

    def test_a_call_with_one_person_on_the_other_side_is_you_and_speaker_1(self):
        spans = [(1, 4, "you"), (5, 9, "them"), (10, 12, "you"), (13, 18, "them"), (19, 21, "you")]
        # the diarizer splits the one remote person into 3 "speakers" (two of them noise)
        turns = [(5, 9, 4), (13, 14, 7), (14, 18, 4), (16, 16.5, 9)]
        text, labeled = self._transcript(spans, turns)
        lines = text.split("\n")
        self.assertEqual([l.split(":")[0] for l in lines],
                         ["You", "Speaker 1", "You", "Speaker 1", "You"])
        self.assertEqual(len({l.split(":")[0] for l in lines}), 2)

    def test_two_people_on_the_call_and_the_user_between_them(self):
        spans = [(1, 10, "them"), (11, 13, "you"), (14, 30, "them"), (31, 40, "them")]
        turns = [(1, 10, 3), (14, 30, 8), (31, 40, 3)]
        text, _ = self._transcript(spans, turns)
        self.assertEqual([l.split(":")[0] for l in text.split("\n")],
                         ["Speaker 1", "You", "Speaker 2", "Speaker 1"])

    def test_consecutive_lines_of_the_same_person_are_merged_as_before(self):
        spans = [(1, 3, "them"), (3.2, 6, "them"), (7, 9, "you")]
        text, _ = self._transcript(spans, [(1, 6, 0)])
        self.assertEqual(text, "Speaker 1: line 0 line 1\nYou: line 2")

    def test_without_turns_the_call_stays_them_and_the_user_stays_you(self):
        spans = [(1, 4, "you"), (5, 9, "them")]
        text, _ = self._transcript(spans, [])
        self.assertEqual([l.split(":")[0] for l in text.split("\n")], ["You", "Them"])

    def test_segments_of_a_later_part_match_the_turns_of_the_joined_audio(self):
        spans = [(1, 4, "them")]
        mic, sysm = envelopes(spans, 6)
        segs = [{"start": 1.0, "end": 4.0, "text": "x"}]
        # the second part starts 100 s into the joined audio
        turns = [(1, 4, 0), (101, 104, 1)]
        first = sl.attribute(segs, mic, sysm, turns, offset=0.0)
        second = sl.attribute(segs, mic, sysm, turns, offset=100.0)
        self.assertEqual((first[0]["speaker"], second[0]["speaker"]), (0, 1))

    def test_names_can_replace_the_numbering_later(self):
        spans = [(1, 4, "them"), (5, 8, "you"), (9, 12, "them")]
        mic, sysm = envelopes(spans, 14)
        segs = segs_of(spans)
        for i, s in enumerate(segs):
            s["text"] = "t%d" % i
        labeled = sl.attribute(segs, mic, sysm, [(1, 4, 5), (9, 12, 6)])
        label_map = diarization.speaker_label_map(labeled, names=sl.speaker_names())
        self.assertEqual(label_map, {5: "Speaker 1", 6: "Speaker 2"})
        names = sl.speaker_names({5: "Anna"})
        self.assertEqual(diarization.build_speaker_transcript(labeled, names=names),
                         "Anna: t0\nYou: t1\nSpeaker 1: t2")

    def test_has_labels(self):
        self.assertTrue(sl.has_labels("Speaker 1: hi\nYou: yes"))
        self.assertTrue(sl.has_labels("intro\nThem: hello"))
        self.assertFalse(sl.has_labels("So here's the thing: we ship"))
        self.assertFalse(sl.has_labels(""))
        self.assertFalse(sl.has_labels(None))

    def test_the_line_format_is_what_the_transcript_view_parses(self):
        import rich_text
        spans = [(1, 4, "you"), (5, 9, "them")]
        text, _ = self._transcript(spans, [(5, 9, 2)])
        for line in text.split("\n"):
            label, body = rich_text.split_speaker(line)
            self.assertIn(label, ("You", "Speaker 1"))
            self.assertTrue(body.startswith("line"))


# ── tricky cases: speech-like levels, an echo that arrives late, odd call sound ──

class _Scene:
    """Per-50 ms levels of a mic + system sound meeting, built from utterances
    [(start, end, "you" | "them" | "music", level)]. Speech levels jump about
    (a syllable is not a steady tone) and dip between words; the call reaches the
    microphone as an echo of strength ``k`` that is ``delay`` frames late and
    rings on for two more (speakers in a room); ``music`` is a steady call sound
    with no words. Segments are what a speech model would return: timestamps a
    little off, ending a little late."""

    def __init__(self, seconds, k=0.0, delay=3, seed=0, mic_noise=0.0012):
        self.n = int(seconds / HOP)
        self.k, self.delay, self.mic_noise = k, delay, mic_noise
        self.rnd = random.Random(seed)
        self.utts = []

    def add(self, who, start, end, level):
        self.utts.append((start, end, who, level))

    def build(self):
        n, rnd = self.n, self.rnd
        user_sq, sys_amp = [0.0] * n, [0.0] * n
        for start, end, who, level in self.utts:
            for i in range(int(start / HOP), min(n, int(end / HOP))):
                if who == "music":
                    v = level * rnd.lognormvariate(0, 0.12)
                else:
                    v = level * rnd.lognormvariate(0, 0.45) * (0.08 if rnd.random() < 0.12 else 1.0)
                if who == "you":
                    user_sq[i] += v * v
                else:
                    sys_amp[i] = (sys_amp[i] ** 2 + v * v) ** 0.5
        echo_sq = [0.0] * n
        if self.k > 0:
            for i, s in enumerate(sys_amp):
                for off, gain in ((self.delay, 1.0), (self.delay + 1, 0.5), (self.delay + 2, 0.25)):
                    if s > 0 and i + off < n:
                        echo_sq[i + off] += (self.k * s * gain) ** 2
        mic = [(self.mic_noise ** 2 * (0.6 + 0.8 * rnd.random()) ** 2 + user_sq[i] + echo_sq[i]) ** 0.5
               for i in range(n)]
        segments, truth = [], []
        for start, end, who, _lvl in sorted(self.utts):
            if who == "music":
                continue
            segments.append({"start": max(0.0, start + rnd.uniform(-0.15, 0.15)),
                             "end": end + 0.2 + rnd.uniform(-0.15, 0.15), "text": "words"})
            truth.append(who)
        return mic, sys_amp, segments, truth


def _conversation(scene, seconds, you_level, them_level, short=False, seed=1):
    """People taking turns: the user about a third of the time."""
    rnd = random.Random(seed)
    t, last = 1.0, None
    while t < seconds - 6:
        who = "you" if rnd.random() < 0.3 else "them"
        if who == last and rnd.random() < 0.5:
            who = "you" if who == "them" else "them"
        dur = rnd.uniform(0.35, 0.8) if short else rnd.uniform(1.5, 9.0)
        scene.add(who, t, t + dur, (you_level if who == "you" else them_level) * rnd.uniform(0.7, 1.3))
        t += dur + rnd.uniform(0.25, 1.5)
        last = who
    return scene


def _accuracy(scene):
    mic, sysm, segs, truth = scene.build()
    got = [lab["source"] for lab in sl.label_segments(segs, mic, sysm)]
    right = {"you": 0, "them": 0}
    total = {"you": 0, "them": 0}
    for g, t in zip(got, truth):
        total[t] += 1
        right[t] += (g == t)
    return {t: (right[t] / float(total[t]) if total[t] else 1.0) for t in total}, total


class TestTrickyCases(unittest.TestCase):
    """The source-dominance labeller on the situations that break it: speakers
    instead of headphones, a user who is quiet for minutes or quieter than the
    other side, sound that never stops, one-word answers, talking over each other."""

    def _check(self, scene, you_min=0.95, them_min=0.95):
        acc, total = _accuracy(scene)
        self.assertGreater(total["you"] + total["them"], 0)
        if total["you"]:
            self.assertGreaterEqual(acc["you"], you_min, "you: %r" % (acc,))
        if total["them"]:
            self.assertGreaterEqual(acc["them"], them_min, "them: %r" % (acc,))

    def test_headphones_and_speakers_with_the_call_arriving_late_on_the_microphone(self):
        for k in (0.0, 0.1, 0.4, 0.8, 1.0):
            for seed in (1, 2, 3):
                self._check(_conversation(_Scene(300, k=k, seed=seed), 300, 0.06, 0.12, seed=seed))

    def test_short_answers_on_speakers_are_not_swallowed_by_the_echo_tail(self):
        # 0.35 - 0.8 s utterances: the echo of the last one rings on for as long
        # as a whole "yes" - it must not become a "You" line of its own.
        for k in (0.4, 0.8):
            for seed in (1, 2, 3):
                self._check(_conversation(_Scene(300, k=k, seed=seed), 300, 0.05, 0.12, short=True, seed=seed))
        self._check(_conversation(_Scene(300, k=0.0, seed=4), 300, 0.05, 0.12, short=True, seed=4))

    def test_a_quiet_user_next_to_a_much_louder_remote_person(self):
        for k in (0.0, 0.3):
            self._check(_conversation(_Scene(300, k=k, seed=5), 300, 0.025, 0.3, seed=5))

    def test_the_user_silent_for_minutes_is_then_recognised_at_once(self):
        for k in (0.0, 0.4):
            scene = _Scene(600, k=k, seed=6)
            rnd = random.Random(6)
            t = 0.5
            while t < 540:                                      # nine minutes: the call only
                dur = rnd.uniform(2, 9)
                scene.add("them", t, t + dur, 0.1 * rnd.uniform(0.7, 1.3))
                t += dur + rnd.uniform(0.3, 1.5)
            t = 545.0
            while t < 590:
                dur = rnd.uniform(1, 4)
                scene.add("you", t, t + dur, 0.03 * rnd.uniform(0.7, 1.3))
                t += dur + rnd.uniform(0.5, 2)
            self._check(scene)

    def test_sound_that_never_stops_does_not_hide_the_call_or_the_user(self):
        # A video's sound / music runs under everything: the call's track has no
        # quiet stretch to learn a "floor" from. It used to take its own level
        # for the floor, never see the call as active, and call every echo on
        # the microphone "You".
        for k in (0.0, 0.3):
            scene = _Scene(240, k=k, seed=7)
            scene.add("music", 0.0, 240.0, 0.1)
            rnd = random.Random(7)
            t = 3.0
            while t < 230:                      # the user and a voice on the call, over the music
                dur = rnd.uniform(1.5, 6.0)
                who = "you" if rnd.random() < 0.4 else "them"
                scene.add(who, t, t + dur, (0.09 if who == "you" else 0.12) * rnd.uniform(0.8, 1.2))
                t += dur + rnd.uniform(1.0, 4.0)
            self._check(scene, you_min=0.8)
        # ... and a presenter whose words never pause long: all of it is the call.
        scene = _Scene(240, k=0.3, seed=8)
        rnd = random.Random(8)
        t = 0.5
        while t < 230:
            dur = rnd.uniform(3, 8)
            scene.add("them", t, t + dur, 0.1 * rnd.uniform(0.8, 1.2))
            t += dur + rnd.uniform(0.1, 0.3)
        self._check(scene)

    def test_the_user_commenting_over_a_continuous_call_on_headphones(self):
        scene = _Scene(240, k=0.0, seed=9)
        scene.add("music", 0.0, 240.0, 0.1)
        rnd = random.Random(9)
        t = 3.0
        while t < 230:
            dur = rnd.uniform(1.0, 3.0)
            scene.add("you", t, t + dur, 0.04 * rnd.uniform(0.7, 1.3))
            t += dur + rnd.uniform(3.0, 12.0)
        self._check(scene, you_min=0.95)

    def test_both_talking_at_once_goes_to_the_clearly_louder_one(self):
        # 6 s of the call; the user comes in over the middle 2 s, 3x louder than the echo.
        mic, sysm = envelopes([(1, 7, "them"), (3, 5, "you")], 9, k=0.2, user=0.25, call=0.1)
        segs = [{"start": 1.0, "end": 3.0, "text": "a"}, {"start": 3.1, "end": 4.9, "text": "b"},
                {"start": 5.1, "end": 7.0, "text": "c"}]
        got = [lab["source"] for lab in sl.label_segments(segs, mic, sysm)]
        self.assertEqual(got, ["them", "you", "them"])

    def test_an_echo_only_microphone_is_never_you(self):
        # Only the call plays; the user says nothing. Whatever the room does to it,
        # not one line may become "You".
        for k in (0.2, 0.7, 1.2):
            scene = _Scene(240, k=k, seed=10)
            rnd = random.Random(10)
            t = 1.0
            while t < 230:
                dur = rnd.uniform(0.4, 8.0)
                scene.add("them", t, t + dur, 0.12 * rnd.uniform(0.6, 1.4))
                t += dur + rnd.uniform(0.2, 2.0)
            acc, total = _accuracy(scene)
            self.assertGreaterEqual(acc["them"], 0.97, (k, acc))

    def test_with_nothing_on_the_call_every_sound_on_the_microphone_is_you(self):
        # The call's sound never reached the recorder (a different output device):
        # the system track is all zeros. The microphone's words are the user's.
        scene = _conversation(_Scene(120, k=0.0, seed=11), 120, 0.06, 0.0, seed=11)
        mic, _sysm, segs, truth = scene.build()
        labs = sl.label_segments(segs, mic, [0.0] * len(mic))
        for lab, who in zip(labs, truth):
            if who == "you":
                self.assertEqual(lab["source"], "you")

    def test_peak_before_is_the_loudest_of_the_last_frames(self):
        self.assertEqual(sl.peak_before([0, 0, 5, 0, 0, 0, 0, 1], lag=2), [0, 0, 5, 5, 5, 0, 0, 1])
        self.assertEqual(sl.peak_before([3, 1, 2], lag=0), [3, 1, 2])
        self.assertEqual(sl.peak_before([], lag=4), [])
        rnd = random.Random(3)
        values = [rnd.random() for _ in range(200)]
        for lag in (1, 6, 40):
            want = [max(values[max(0, i - lag):i + 1]) for i in range(len(values))]
            self.assertEqual(sl.peak_before(values, lag=lag), want)

    def test_the_microphones_floor_is_learned_where_the_call_is_silent(self):
        n = 400
        system = [0.0] * 100 + [0.1] * 300
        quiet_mic = [0.001] * 100 + [0.03] * 300                  # the echo is 30x the room noise
        self.assertAlmostEqual(sl.mic_threshold(quiet_mic, system), sl.ABS_MIN)   # 4 x 0.001 < the minimum
        noisy = [0.01] * 100 + [0.03] * 300
        self.assertAlmostEqual(sl.mic_threshold(noisy, system), 0.04)
        # Not enough silence to learn from: only the minimum, never the echo.
        self.assertEqual(sl.mic_threshold([0.03] * n, [0.1] * n), sl.ABS_MIN)
        # The call's own threshold is fixed, whatever the track holds.
        self.assertEqual(sl.system_threshold([0.1] * 500), sl.system_threshold([0.0] * 500))

    def test_a_call_that_never_pauses_is_still_given_to_the_diarizer(self):
        n = 1200                                                   # 60 s of steady call sound
        rms = [0.1] * n
        self.assertEqual(sl.active_spans(rms, thr=sl.system_threshold()), [(0.0, 60.0)])
        # (a percentile "floor" of this track would be its own level and drop all of it)


if __name__ == "__main__":
    unittest.main()
