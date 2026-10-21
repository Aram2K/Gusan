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


class TestDownloadProgress(unittest.TestCase):
    def test_the_status_line_follows_the_download_in_steps_of_five_percent(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            seg = d / diarization._SEG_REL
            seg.parent.mkdir(parents=True)
            seg.write_bytes(b"x")                                 # only the speaker model is missing

            def fake_retrieve(url, filename, reporthook=None):
                total = 1000
                for blocks in range(0, 101):
                    if reporthook:
                        reporthook(blocks, 10, total)
                Path(filename).write_bytes(b"model")
                return filename, None
            messages = []
            with mock.patch.object(diarization, "_models_dir", return_value=d), \
                    mock.patch.object(diarization.urllib.request, "urlretrieve", fake_retrieve):
                ok = diarization.download_models(on_progress=messages.append)
            self.assertTrue(ok)
            self.assertEqual(messages[0], "Downloading speaker model (40 MB)...")
            pct = [m for m in messages if m.endswith("%")]
            self.assertTrue(all(m.startswith("Downloading speaker model (40 MB)") for m in pct))
            self.assertLessEqual(len(pct), 21)                     # not a message per block
            self.assertTrue(pct[-1].endswith("100%"))
            self.assertTrue((d / diarization._EMB_REL).is_file())
            self.assertFalse((d / "embedding.onnx.part").exists())

    def test_a_download_that_fails_returns_false_and_never_raises(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(diarization, "_models_dir", return_value=Path(tmp)), \
                    mock.patch.object(diarization.urllib.request, "urlretrieve",
                                               side_effect=OSError("no network")):
                self.assertFalse(diarization.download_models())


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


if __name__ == "__main__":
    unittest.main()
