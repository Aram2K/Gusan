"""Tests for diarization speaker/segment alignment + a guarded model smoke test.

The alignment tests are pure logic (no numpy/models) so they always run. The
smoke test runs the real sherpa-onnx pipeline only when the models are present
AND numpy is the real one (it's skipped under the suite-wide stubs in
test_core, but runs when this file is executed on its own).
"""
import unittest
from unittest import mock

import diarization


def _real_numpy():
    try:
        import numpy as np
        return float(np.asarray([1.0, 2.0, 3.0]).mean()) == 2.0
    except Exception:
        return False


class TestAttributeSegments(unittest.TestCase):
    def test_max_overlap_assignment(self):
        turns = [(0.0, 5.0, 0), (5.0, 10.0, 1)]
        segs = [{"start": 0.5, "end": 4.0, "text": "a"},
                {"start": 6.0, "end": 9.0, "text": "b"}]
        out = diarization.attribute_segments(segs, turns)
        self.assertEqual(out[0]["speaker"], 0)
        self.assertEqual(out[1]["speaker"], 1)

    def test_straddle_picks_dominant_speaker(self):
        turns = [(0.0, 5.0, 0), (5.0, 10.0, 1)]
        # 4.0-10.0 overlaps spk0 by 1s and spk1 by 5s -> spk1 wins.
        out = diarization.attribute_segments(
            [{"start": 4.0, "end": 10.0, "text": "x"}], turns)
        self.assertEqual(out[0]["speaker"], 1)

    def test_no_overlap_snaps_to_nearest(self):
        # 5-6 doesn't overlap the 0-2 turn, but snaps to its speaker (nearest).
        out = diarization.attribute_segments(
            [{"start": 5.0, "end": 6.0, "text": "x"}], [(0.0, 2.0, 7)])
        self.assertEqual(out[0]["speaker"], 7)

    def test_picks_nearest_of_several(self):
        turns = [(0.0, 2.0, 0), (100.0, 102.0, 1)]
        out = diarization.attribute_segments(
            [{"start": 3.0, "end": 4.0, "text": "x"}], turns)  # closer to turn 0
        self.assertEqual(out[0]["speaker"], 0)

    def test_empty_turns_is_none(self):
        out = diarization.attribute_segments(
            [{"start": 5.0, "end": 6.0, "text": "x"}], [])
        self.assertIsNone(out[0]["speaker"])

    def test_offset_shifts_matching(self):
        # 1-3 shifted by +10 -> 11-13 overlaps the 10-15 turn.
        out = diarization.attribute_segments(
            [{"start": 1.0, "end": 3.0, "text": "x"}], [(10.0, 15.0, 3)], offset=10.0)
        self.assertEqual(out[0]["speaker"], 3)

    def test_preserves_original_keys(self):
        out = diarization.attribute_segments(
            [{"start": 0.0, "end": 1.0, "text": "hello", "language": "en"}], [])
        self.assertIsNone(out[0]["speaker"])
        self.assertEqual(out[0]["text"], "hello")
        self.assertEqual(out[0]["language"], "en")


class TestBuildSpeakerTranscript(unittest.TestCase):
    def test_merges_consecutive_same_speaker(self):
        segs = [
            {"text": "hello there", "speaker": 0},
            {"text": "how are you", "speaker": 0},
            {"text": "i am good", "speaker": 1},
        ]
        out = diarization.build_speaker_transcript(segs)
        self.assertEqual(
            out, "Speaker 1: hello there how are you\nSpeaker 2: i am good")

    def test_labels_are_first_appearance_order(self):
        # raw speaker ids 7 then 3 -> renumbered 1 then 2
        segs = [{"text": "a", "speaker": 7}, {"text": "b", "speaker": 3},
                {"text": "c", "speaker": 7}]
        out = diarization.build_speaker_transcript(segs)
        self.assertEqual(out, "Speaker 1: a\nSpeaker 2: b\nSpeaker 1: c")

    def test_names_override(self):
        segs = [{"text": "hi", "speaker": 0}, {"text": "yo", "speaker": 1}]
        out = diarization.build_speaker_transcript(segs, names={0: "You"})
        self.assertEqual(out, "You: hi\nSpeaker 1: yo")

    def test_none_speaker_and_blank_segments(self):
        segs = [{"text": "  ", "speaker": 0}, {"text": "lonely", "speaker": None}]
        out = diarization.build_speaker_transcript(segs)
        self.assertEqual(out, "Speaker ?: lonely")

    def test_empty(self):
        self.assertEqual(diarization.build_speaker_transcript([]), "")


@unittest.skipUnless(_real_numpy() and diarization.is_available(),
                     "diarization models / real numpy not available")
class TestDiarizeSmoke(unittest.TestCase):
    def test_diarize_sample_returns_turns(self):
        import os
        import wave
        import numpy as np
        wav = os.path.join(str(diarization._models_dir()), "sample.wav")
        if not os.path.exists(wav):
            self.skipTest("sample.wav not present")
        with wave.open(wav) as w:
            ch, n = w.getnchannels(), w.getnframes()
            raw = w.readframes(n)
        audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
        if ch > 1:
            audio = audio.reshape(-1, ch).mean(axis=1)
        turns = diarization.diarize(audio)
        self.assertTrue(turns, "expected non-empty speaker turns")
        for (s, e, spk) in turns:
            self.assertLessEqual(s, e)
            self.assertIsInstance(spk, int)


# ── Gusan 2.0.1: numbering across parts, reasons, download progress ──────────

class TestSpeakerLabelMap(unittest.TestCase):
    def test_it_is_the_numbering_build_speaker_transcript_uses(self):
        segs = [{"text": "a", "speaker": 7}, {"text": "b", "speaker": 3}, {"text": "c", "speaker": 7},
                {"text": "d", "speaker": None}, {"text": "  ", "speaker": 9}]
        self.assertEqual(diarization.speaker_label_map(segs), {7: "Speaker 1", 3: "Speaker 2"})
        out = diarization.build_speaker_transcript(segs, names=diarization.speaker_label_map(segs))
        self.assertEqual(out, "Speaker 1: a\nSpeaker 2: b\nSpeaker 1: c\nSpeaker ?: d")
        self.assertEqual(out, diarization.build_speaker_transcript(segs))

    def test_named_speakers_are_left_out_of_the_numbering(self):
        segs = [{"text": "a", "speaker": -1}, {"text": "b", "speaker": 4}, {"text": "c", "speaker": 5}]
        self.assertEqual(diarization.speaker_label_map(segs, names={-1: "You"}),
                         {4: "Speaker 1", 5: "Speaker 2"})

    def test_two_parts_of_a_meeting_share_one_numbering(self):
        part1 = [{"text": "a", "speaker": 8}, {"text": "b", "speaker": 2}]
        part2 = [{"text": "c", "speaker": 2}, {"text": "d", "speaker": 6}]
        names = diarization.speaker_label_map(part1 + part2)
        self.assertEqual(diarization.build_speaker_transcript(part1, names=names),
                         "Speaker 1: a\nSpeaker 2: b")
        self.assertEqual(diarization.build_speaker_transcript(part2, names=names),
                         "Speaker 2: c\nSpeaker 3: d")        # Speaker 2 is still the same person
        # ...whereas numbering each part alone would call the first voice of part 2 "Speaker 1".
        self.assertEqual(diarization.build_speaker_transcript(part2), "Speaker 1: c\nSpeaker 2: d")

    def test_a_name_can_replace_a_label_later(self):
        segs = [{"text": "a", "speaker": 8}, {"text": "b", "speaker": 2}]
        names = diarization.speaker_label_map(segs)
        names[2] = "Anna"
        self.assertEqual(diarization.build_speaker_transcript(segs, names=names), "Speaker 1: a\nAnna: b")


class TestDiarizeSaysWhy(unittest.TestCase):
    def setUp(self):
        self._saved = diarization.last_error
        self.addCleanup(setattr, diarization, "last_error", self._saved)

    def test_unavailable(self):
        with mock.patch.object(diarization, "is_available", return_value=False):
            self.assertEqual(diarization.diarize([0.0] * 32000), [])
        self.assertEqual(diarization.last_error, "unavailable")

    @unittest.skipUnless(_real_numpy(), "real numpy not importable (stubbed)")
    def test_too_short(self):
        import numpy as np
        with mock.patch.object(diarization, "is_available", return_value=True):
            self.assertEqual(diarization.diarize(np.zeros(1000, np.float32)), [])
        self.assertEqual(diarization.last_error, "too short")

    @unittest.skipUnless(_real_numpy(), "real numpy not importable (stubbed)")
    def test_a_failure_in_the_engine_is_kept_as_the_reason(self):
        import numpy as np
        with mock.patch.object(diarization, "is_available", return_value=True), \
                mock.patch.dict("os.environ", {"GUSAN_DIARIZE_IN_PROCESS": "1"}), \
                mock.patch.object(diarization, "_build_diarizer",
                                           side_effect=RuntimeError("model file is damaged")), \
                mock.patch.object(diarization, "_diarizer", None):
            self.assertEqual(diarization.diarize(np.zeros(32000, np.float32)), [])
        self.assertIn("model file is damaged", diarization.last_error)


class _Response:
    """What urlopen gives back: a Content-Length, ``read(n)`` in pieces, and a context manager."""

    def __init__(self, total=1000, sent=None, fail_after=None, error=None):
        self.headers = {"Content-Length": str(total)} if total is not None else {}
        self.left = total if sent is None else sent
        self.reads = 0
        self.fail_after = fail_after
        self.error = error

    def read(self, n=-1):
        self.reads += 1
        if self.fail_after is not None and self.reads > self.fail_after:
            raise self.error or OSError("timed out")
        n = self.left if n is None or n < 0 else min(n, self.left)
        self.left -= n
        return b"m" * n

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class TestDownloadProgress(unittest.TestCase):
    def setUp(self):
        import tempfile
        from pathlib import Path
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.d = Path(self._tmp.name)
        seg = self.d / diarization._SEG_REL
        seg.parent.mkdir(parents=True)
        seg.write_bytes(b"x")                                      # only the speaker model is missing
        for patcher in (mock.patch.object(diarization, "_models_dir", return_value=self.d),
                        mock.patch.object(diarization, "DOWNLOAD_CHUNK", 10)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _download(self, response=None, **kw):
        response = response or _Response()
        opened = []

        def fake_urlopen(url, timeout=None):
            opened.append((url, timeout))
            return response
        with mock.patch.object(diarization.urllib.request, "urlopen", fake_urlopen):
            ok = diarization.download_models(**kw)
        return ok, opened, response

    def test_the_status_line_follows_the_download_in_steps_of_five_percent(self):
        messages = []
        ok, _opened, _r = self._download(on_progress=messages.append)
        self.assertTrue(ok)
        self.assertEqual(messages[0], "Downloading speaker model (40 MB)...")
        pct = [m for m in messages if m.endswith("%")]
        self.assertTrue(all(m.startswith("Downloading speaker model (40 MB)") for m in pct))
        self.assertLessEqual(len(pct), 21)                         # not a message per block
        self.assertTrue(pct[-1].endswith("100%"))
        self.assertTrue((self.d / diarization._EMB_REL).is_file())
        self.assertEqual((self.d / diarization._EMB_REL).stat().st_size, 1000)
        self.assertFalse((self.d / "embedding.onnx.part").exists())

    def test_the_bytes_come_as_they_arrive_for_a_progress_bar(self):
        seen = []
        ok, _opened, _r = self._download(on_bytes=lambda done, total: seen.append((done, total)))
        self.assertTrue(ok)
        self.assertEqual(seen[0], (10, 1000))
        self.assertEqual(seen[-1], (1000, 1000))
        self.assertEqual([d for d, _t in seen], sorted(d for d, _t in seen))

    def test_a_download_that_fails_returns_false_and_never_raises(self):
        with mock.patch.object(diarization.urllib.request, "urlopen", side_effect=OSError("no network")):
            self.assertFalse(diarization.download_models())
        self.assertFalse((self.d / "embedding.onnx.part").exists())

    def test_a_connection_that_goes_quiet_ends_with_an_error_and_never_hangs(self):
        ok, opened, _r = self._download(_Response(fail_after=3, error=OSError("the read timed out")))
        self.assertFalse(ok)                                       # (the pass says "check your internet connection")
        self.assertEqual(opened[0][1], diarization.DOWNLOAD_TIMEOUT_S)         # a timeout is always asked for
        self.assertEqual(diarization.DOWNLOAD_TIMEOUT_S, 15)
        self.assertFalse((self.d / "embedding.onnx.part").exists())            # no half file left
        self.assertFalse((self.d / diarization._EMB_REL).exists())

    def test_a_download_that_ends_early_is_a_failure_not_a_model(self):
        ok, _opened, _r = self._download(_Response(total=1000, sent=400))
        self.assertFalse(ok)
        self.assertFalse((self.d / diarization._EMB_REL).exists())
        self.assertFalse((self.d / "embedding.onnx.part").exists())

    def test_cancel_stops_it_between_two_chunks_and_leaves_nothing(self):
        asked = []

        def cancel():
            asked.append(1)
            return len(asked) > 4                                  # said yes at the fifth look
        ok, _opened, response = self._download(should_cancel=cancel)
        self.assertFalse(ok)
        self.assertEqual(response.reads, 4)                        # four chunks came, the fifth was never asked for
        self.assertFalse((self.d / "embedding.onnx.part").exists())
        self.assertFalse((self.d / diarization._EMB_REL).exists())

    def test_a_cancel_before_the_first_chunk_asks_for_nothing(self):
        ok, _opened, response = self._download(should_cancel=lambda: True)
        self.assertFalse(ok)
        self.assertEqual(response.reads, 0)


@unittest.skipUnless(_real_numpy() and diarization.is_available(),
                     "diarization models / real numpy not available")
class TestSherpaOnACallSmoke(unittest.TestCase):
    """Real sherpa on a synthetic two-track call: the whole pass runs and ends in
    a clean Outcome (sherpa may or may not hear voices in noise - labels are
    judged on fabricated turns elsewhere)."""

    def test_the_pass_ends_cleanly_with_the_real_speaker_tool(self):
        import numpy as np
        import meeting_speakers as msp

        class Rec:
            def transcribe_segments(self, audio, language=None, model_name=None, on_progress=None,
                                    should_cancel=None):
                return [{"start": 2.0, "end": 6.0, "text": "me"}, {"start": 8.0, "end": 14.0, "text": "call"}]
        rng = np.random.default_rng(1)
        n = 16000 * 20
        mic = (rng.standard_normal(n) * 0.0008).astype(np.float32)
        system = np.zeros(n, np.float32)
        mic[32000:96000] += rng.standard_normal(64000).astype(np.float32) * 0.08
        system[128000:224000] += rng.standard_normal(96000).astype(np.float32) * 0.1
        pcm = lambda x: np.rint(np.clip(x, -1, 1) * 32767).astype(np.int16)
        part = msp.Part(audio=(mic + system) * 0.75, mic=pcm(mic), system=pcm(system))
        out = msp.build_transcript(Rec(), [part])
        self.assertIn(out.reason, ("", msp.R_NO_VOICES))
        self.assertTrue(out.texts)
        self.assertTrue(out.text.startswith("You: me"))

# ── one voice print per stretch (embed_spans), with a stand-in for the speaker model ─────────────────────

class FakeExtractor:
    """create_stream / accept_waveform / compute like sherpa's: a voice print that tells what it was given."""
    dim = 3

    def __init__(self):
        self.streams = 0

    def create_stream(self):
        self.streams += 1
        return FakeStream()

    def compute(self, stream):
        x = stream.x
        return [float(x.mean()), float(len(x)), 1.0]


class FakeStream:
    def accept_waveform(self, rate, samples):
        self.rate, self.x = rate, samples

    def input_finished(self):
        pass


@unittest.skipUnless(_real_numpy(), "real numpy not importable (stubbed)")
class TestEmbedSpans(unittest.TestCase):
    def setUp(self):
        import numpy as np
        self.np = np
        for patcher in (mock.patch.object(diarization, "is_available", return_value=True),
                        mock.patch.dict("os.environ", {"GUSAN_DIARIZE_IN_PROCESS": "1"}),
                        mock.patch.object(diarization, "build_extractor", side_effect=lambda threads=None: self.extractor)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.extractor = FakeExtractor()
        self._saved = diarization.last_error
        self.addCleanup(setattr, diarization, "last_error", self._saved)
        # 0-2 s: 0.1, 2-4 s: 0.2, 4-6 s: silence, 6-6.5 s: 0.3
        self.audio = np.concatenate([np.full(32000, 0.1, np.float32), np.full(32000, 0.2, np.float32),
                                     np.zeros(32000, np.float32), np.full(8000, 0.3, np.float32)])

    def test_each_stretch_gets_the_voice_print_of_its_own_sound(self):
        vectors, err = diarization.embed_spans(self.audio, [(0.0, 2.0), (2.0, 4.0)])
        self.assertEqual(err, "")
        self.assertEqual([round(v[0], 3) for v in vectors], [0.1, 0.2])
        self.assertEqual([v[1] for v in vectors], [32000.0, 32000.0])
        self.assertEqual(self.extractor.streams, 2)
        self.assertEqual(diarization.last_error, "")

    def test_a_stretch_with_nothing_to_embed_is_none_and_costs_no_work(self):
        spans = [(0.0, 2.0), (4.0, 6.0), (6.0, 6.5), (7.0, 9.0), (3.0, 3.0), (5.0, 4.0)]
        vectors, err = diarization.embed_spans(self.audio, spans)
        self.assertEqual(err, "")
        self.assertEqual([v is None for v in vectors], [False, True, True, True, True, True])   # silent / too short / past the end
        self.assertEqual(self.extractor.streams, 1)

    def test_only_silence_never_loads_the_speaker_model(self):
        with mock.patch.object(diarization, "build_extractor", side_effect=AssertionError("no model needed")):
            vectors, err = diarization.embed_spans(self.audio, [(4.0, 6.0), (10.0, 12.0)])
        self.assertEqual((vectors, err), ([None, None], ""))

    def test_int16_sound_is_read_as_the_same_voice_as_float(self):
        pcm = self.np.rint(self.audio * 32767).astype(self.np.int16)
        a, _ = diarization.embed_spans(self.audio, [(0.0, 2.0)])
        b, _ = diarization.embed_spans(pcm, [(0.0, 2.0)])
        self.assertAlmostEqual(a[0][0], b[0][0], places=3)

    def test_stereo_sound_is_mixed_down(self):
        stereo = self.np.stack([self.audio, self.audio], axis=1)
        vectors, err = diarization.embed_spans(stereo, [(0.0, 2.0)])
        self.assertEqual(err, "")
        self.assertAlmostEqual(vectors[0][0], 0.1, places=3)

    def test_progress_counts_every_stretch_and_a_failing_callback_changes_nothing(self):
        marks = []
        diarization.embed_spans(self.audio, [(0.0, 2.0), (4.0, 6.0), (2.0, 4.0)],
                                on_progress=lambda d, t: marks.append((d, t)))
        self.assertEqual(marks, [(1, 3), (2, 3), (3, 3)])

        def broken(d, t):
            raise RuntimeError("the page went away")
        vectors, err = diarization.embed_spans(self.audio, [(0.0, 2.0), (2.0, 4.0)], on_progress=broken)
        self.assertEqual((err, len(vectors)), ("", 2))

    def test_a_cancel_hands_back_what_was_made(self):
        answers = iter([False, False, True])
        vectors, err = diarization.embed_spans(self.audio, [(0.0, 2.0), (2.0, 4.0), (0.0, 2.0), (2.0, 4.0)],
                                               should_cancel=lambda: next(answers, True))
        self.assertEqual(err, "cancelled")
        self.assertEqual(len(vectors), 2)
        self.assertEqual(diarization.last_error, "cancelled")

    def test_quitting_the_app_stops_it_too(self):
        import diarization_proc
        diarization_proc._cancel_all.set()
        self.addCleanup(diarization_proc._cancel_all.clear)
        vectors, err = diarization.embed_spans(self.audio, [(0.0, 2.0)])
        self.assertEqual((vectors, err), ([], "app quitting"))

    def test_a_model_that_cannot_be_built_is_an_error_with_a_reason(self):
        with mock.patch.object(diarization, "build_extractor", side_effect=RuntimeError("model file is damaged")):
            vectors, err = diarization.embed_spans(self.audio, [(0.0, 2.0)])
        self.assertEqual(vectors, [])
        self.assertIn("model file is damaged", err)
        self.assertEqual(diarization.last_error, err)

    def test_a_model_that_fails_midway_keeps_the_prefix(self):
        real = self.extractor.compute
        calls = {"n": 0}

        def flaky(stream):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("out of memory")
            return real(stream)
        self.extractor.compute = flaky
        vectors, err = diarization.embed_spans(self.audio, [(0.0, 2.0), (2.0, 4.0), (0.0, 2.0)])
        self.assertEqual(len(vectors), 1)
        self.assertIn("out of memory", err)

    def test_nothing_asked_nothing_done(self):
        self.assertEqual(diarization.embed_spans(self.audio, []), ([], ""))
        self.assertEqual(diarization.embed_spans(self.audio, None), ([], ""))

    def test_without_the_speaker_tool_it_says_so(self):
        with mock.patch.object(diarization, "is_available", return_value=False):
            self.assertEqual(diarization.embed_spans(self.audio, [(0.0, 2.0)]), ([], "unavailable"))
        self.assertEqual(diarization.last_error, "unavailable")

    def test_sound_that_cannot_be_read_is_bad_audio_not_a_crash(self):
        vectors, err = diarization.embed_spans(object(), [(0.0, 2.0)])
        self.assertEqual((vectors, err), ([], "bad audio"))
        vectors, err = diarization.embed_spans(self.audio, [("x", "y")])
        self.assertEqual((vectors, err), ([], "bad audio"))

    def test_the_child_process_is_used_unless_told_otherwise(self):
        import os
        with mock.patch.dict("os.environ", {}, clear=False):
            os.environ.pop("GUSAN_DIARIZE_IN_PROCESS", None)
            with mock.patch.object(diarization.diarization_proc, "run_voices",
                                   return_value=([[1.0, 2.0]], "")) as run:
                vectors, err = diarization.embed_spans(self.audio, [(0.0, 2.0), (4.0, 6.0)])
        self.assertEqual((vectors, err), ([[1.0, 2.0]], ""))
        audio, lengths = run.call_args[0][:2]
        self.assertEqual(lengths, [32000, 0])                       # the silent stretch is not sent
        self.assertEqual(len(audio), 32000)
        self.assertEqual(self.extractor.streams, 0)                  # nothing was made in this process

    def test_the_worker_count_is_the_childs_own_rule(self):
        self.assertEqual(diarization.voice_threads(), diarization.diarization_proc.voice_threads())


@unittest.skipUnless(_real_numpy() and diarization.is_available(),
                     "diarization models / real numpy not available")
class TestVoicePrintSmoke(unittest.TestCase):
    """The real speaker model: the same sound gives the same voice print, 512 finite numbers."""

    def test_the_real_model_gives_finite_repeatable_voice_prints(self):
        import numpy as np
        rng = np.random.default_rng(3)
        audio = (rng.standard_normal(16000 * 6) * 0.05).astype(np.float32)
        with mock.patch.dict("os.environ", {"GUSAN_DIARIZE_IN_PROCESS": "1"}):
            a, err_a = diarization.embed_spans(audio, [(0.0, 3.0), (3.0, 6.0), (0.0, 3.0)])
        self.assertEqual(err_a, "")
        self.assertEqual([len(v) for v in a], [512, 512, 512])
        self.assertTrue(all(np.isfinite(v).all() for v in a))
        self.assertEqual(a[0], a[2])                                  # identical sound, identical voice print
        self.assertNotEqual(a[0], a[1])


if __name__ == "__main__":
    unittest.main()
