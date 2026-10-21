"""Speaker labels for a recorded meeting (meeting_speakers.py): the steps in
order, every way they end without labels (a reason, a log line, a plain notice -
never silence), the call-only diarization, parts of a continued meeting - with a
fake recorder and a fake diarizer (sherpa is only touched by the guarded smoke
test in test_diarization.py).

Needs real numpy for the audio; skipped under tests/test_core.py's numpy stub.

Notes first, speaker names after: the fast half at Stop (build_transcript(diarize=False):
"You" / "Them", no speaker tool), and the second half from the saved segments
(name_the_call, refine_meeting: "Them" -> "Speaker 1..N", no second reading).
"""
import logging
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import diarization
import meeting_speakers as msp
import meeting_store as ms
import speaker_labels as sl

SR = 16000


def _real_numpy():
    try:
        import numpy as np
        return np.__name__ == "numpy" and tuple(np.zeros(2).shape) == (2,)
    except Exception:
        return False


class FakeRec:
    """transcribe_segments() of a fixed list (a list per call for several parts)."""

    def __init__(self, segments, raises=None):
        self.segments = segments
        self.raises = raises
        self.calls = []

    def transcribe_segments(self, audio, language=None, model_name=None, on_progress=None,
                            should_cancel=None):
        self.calls.append({"seconds": len(audio) / float(SR), "language": language})
        if self.raises:
            raise self.raises
        if should_cancel is not None and should_cancel():
            return None
        if on_progress:
            on_progress(5.0, 10.0)
            on_progress(10.0, 10.0)
        nxt = self.segments
        if nxt and isinstance(nxt[0], list):
            nxt = nxt[len(self.calls) - 1]
        return [dict(s) for s in (nxt or [])]


class FakeDiarizer:
    """The speaker tool without sherpa: availability by switches, diarize() reads
    the audio it is given and returns one speaker's turns over every stretch with
    sound (optionally splitting off short "noise" speakers) - like a diarizer
    hearing one voice. The pure label functions are the real ones."""

    def __init__(self, installed=True, downloaded=True, download_ok=True, split_noise=False,
                 fail=""):
        self.installed, self.downloaded, self.download_ok = installed, downloaded, download_ok
        self.split_noise = split_noise
        self.fail = fail
        self.downloads = 0
        self.diarize_calls = []
        self.last_error = ""

    def sherpa_installed(self):
        return self.installed

    def models_downloaded(self):
        return self.downloaded

    def is_available(self):
        return self.installed and self.downloaded

    def download_models(self, on_progress=None):
        self.downloads += 1
        if on_progress:
            on_progress("Downloading speaker model (40 MB) 50%")
        if self.download_ok:
            self.downloaded = True
        return self.download_ok

    def diarize(self, audio, num_speakers=None, threshold=0.7, on_progress=None, should_cancel=None):
        import numpy as np
        self.diarize_calls.append(len(audio) / float(SR))
        self.last_error = self.fail
        if self.fail:
            return []
        if on_progress:
            on_progress(1, 2)
            on_progress(2, 2)
        a = np.asarray(audio, dtype=np.float32)
        hop = SR // 20
        n = len(a) // hop
        rms = np.sqrt((a[:n * hop].reshape(n, hop) ** 2).mean(axis=1)) if n else np.zeros(0)
        turns, start = [], None
        for i, v in enumerate(list(rms) + [0.0]):
            if v > 0.01:
                if start is None:
                    start = i
            elif start is not None:
                if (i - start) >= 6:
                    turns.append((start * 0.05, i * 0.05, 4))
                start = None
        if self.split_noise and len(turns) > 3:
            s, e, _ = turns[1]
            turns[1] = (s, min(e, s + 1.0), 9)
        return turns

    def __getattr__(self, name):                     # the pure functions are the real ones
        return getattr(diarization, name)


def burst(np, seconds, level, seed):
    return (np.random.default_rng(seed).standard_normal(int(seconds * SR)) * level).astype(np.float32)


def call_tracks(np, seconds, you, them, k=0.12, user=0.08, call=0.1):
    """int16 (mic, system) with the user speaking in ``you`` spans and the call in
    ``them`` spans (its echo on the mic at k)."""
    n = int(seconds * SR)
    mic = burst(np, seconds, 0.0008, 1)
    sysm = np.zeros(n, np.float32)
    for a, b in you:
        mic[int(a * SR):int(b * SR)] += burst(np, b - a, user, 2)[: int(b * SR) - int(a * SR)]
    for a, b in them:
        s = burst(np, b - a, call, 3)[: int(b * SR) - int(a * SR)]
        sysm[int(a * SR):int(b * SR)] += s
        mic[int(a * SR):int(b * SR)] += s * k
    pcm = lambda x: np.rint(np.clip(x, -1, 1) * 32767).astype(np.int16)
    return pcm(mic), pcm(sysm)


def seg(a, b, text):
    return {"start": float(a), "end": float(b), "text": text}


@unittest.skipUnless(_real_numpy(), "real numpy not importable (stubbed)")
class TestMicAndSystemSound(unittest.TestCase):
    def setUp(self):
        import numpy as np
        self.np = np

    def _part(self, seconds, you, them, **kw):
        mic, system = call_tracks(self.np, seconds, you, them, **kw)
        mix = (mic.astype(self.np.float32) / 32767 + system.astype(self.np.float32) / 32767) * 0.75
        return msp.Part(audio=mix.astype(self.np.float32), mic=mic, system=system)

    def test_one_person_on_the_other_side_is_you_and_speaker_1(self):
        you = [(2, 6), (12, 15), (24, 27)]
        them = [(7, 11), (16, 23), (28, 34)]
        part = self._part(36, you, them)
        segs = [seg(2, 6, "a"), seg(7, 11, "b"), seg(12, 15, "c"), seg(16, 23, "d"),
                seg(24, 27, "e"), seg(28, 34, "f")]
        rec = FakeRec(segs)
        d = FakeDiarizer(split_noise=True)
        status = []
        out = msp.build_transcript(rec, [part], language="en", status=status.append, diarizer=d)
        self.assertTrue(out.ok, (out.reason, out.notice))
        self.assertEqual(out.mode, "sources")
        self.assertEqual(out.text.split("\n"),
                         ["You: a", "Speaker 1: b", "You: c", "Speaker 1: d", "You: e", "Speaker 1: f"])
        self.assertEqual(out.speakers, 1)
        self.assertEqual(rec.calls[0]["language"], "en")
        self.assertTrue(any("Re-reading" in s for s in status))
        self.assertTrue(any("Telling the voices apart" in s for s in status))
        self.assertIn("reread", out.timings)

    def test_a_call_that_never_goes_quiet_is_still_the_call_and_the_user_still_you(self):
        # A video's sound / a presenter under everything: the call's track has no quiet
        # stretch, its echo is always on the microphone, the user comes in over it.
        you = [(10, 14), (30, 33)]
        part = self._part(60, you, [(0, 60)], k=0.3, user=0.12)
        segs = [seg(1, 9, "a"), seg(10, 14, "u1"), seg(15, 29, "b"), seg(30, 33, "u2"), seg(34, 58, "c")]
        out = msp.build_transcript(FakeRec(segs), [part], diarizer=FakeDiarizer())
        self.assertTrue(out.ok, (out.reason, out.notice))
        self.assertEqual(out.text.split("\n"),
                         ["Speaker 1: a", "You: u1", "Speaker 1: b", "You: u2", "Speaker 1: c"])

    def test_a_stale_diarizer_error_is_not_this_passes_reason(self):
        # The speaker tool's last_error belongs to its LAST run (a file job, an earlier
        # meeting). A pass that never ran it (no sound on the call's track to give it)
        # must not report that old "timed out" as its own.
        part = self._part(30, [(2, 12)], [])
        segs = [seg(0, 1.5, "z"), seg(2, 12, "a")]       # "z" is over silence: first line, the call by default
        d = FakeDiarizer()
        d.last_error = "timed out"
        out = msp.build_transcript(FakeRec(segs), [part], diarizer=d)
        self.assertEqual(d.diarize_calls, [])
        self.assertEqual(out.reason, msp.R_NO_VOICES)
        self.assertEqual(out.notice, msp.CALL_TOGETHER)
        self.assertEqual(out.text.split("\n"), ["Them: z", "You: a"])

    def test_the_call_track_is_not_held_for_the_whole_diarization(self):
        # Only the joined stretches go to the diarizer: the 16-bit track of the whole
        # meeting (two hours = 230 MB) is let go before it runs.
        import weakref
        np = self.np
        mic, system = call_tracks(np, 40, [(2, 6)], [(10, 20), (25, 35)])
        mix = ((mic.astype(np.float32) + system.astype(np.float32)) / 32767 * 0.75).astype(np.float32)
        alive = {}

        def loader():
            m, s = mic.copy(), system.copy()
            alive["system"] = weakref.ref(s)
            return mix, m, s

        class Probe(FakeDiarizer):
            def diarize(inner, audio, *a, **k):
                import gc
                gc.collect()
                alive["during"] = alive["system"]() is not None
                return FakeDiarizer.diarize(inner, audio, *a, **k)
        part = msp.Part(loader=loader, has_tracks=True)
        out = msp.build_transcript(FakeRec([seg(2, 6, "a"), seg(10, 20, "b"), seg(25, 35, "c")]), [part],
                                   diarizer=Probe())
        self.assertTrue(out.ok, (out.reason, out.notice))
        self.assertIs(alive["during"], False)

    def test_only_the_stretches_where_the_call_made_sound_are_diarized(self):
        you = [(2, 40), (100, 130)]
        them = [(42, 50), (80, 90)]                    # 18 s of call in 130 s
        part = self._part(132, you, them)
        segs = [seg(42, 50, "x"), seg(80, 90, "y")]
        d = FakeDiarizer()
        out = msp.build_transcript(FakeRec(segs), [part], diarizer=d)
        self.assertTrue(out.ok)
        self.assertEqual(len(d.diarize_calls), 1)
        self.assertLess(d.diarize_calls[0], 30.0)                  # not the 132 s
        self.assertGreater(d.diarize_calls[0], 18.0)
        self.assertEqual(out.text.split("\n"), ["Speaker 1: x y"])
        # The turns came back on the meeting's own timeline (both segments matched a turn).
        self.assertEqual(out.speakers, 1)

    def test_no_call_sound_means_no_diarization_and_only_you(self):
        part = self._part(30, [(2, 12), (15, 25)], [])
        d = FakeDiarizer()
        out = msp.build_transcript(FakeRec([seg(2, 12, "a"), seg(15, 25, "b")]), [part], diarizer=d)
        self.assertTrue(out.ok)
        self.assertEqual(out.text, "You: a b")
        self.assertEqual(d.diarize_calls, [])
        self.assertEqual(out.speakers, 0)

    def test_people_on_the_call_that_cannot_be_told_apart_are_them_with_a_notice(self):
        part = self._part(30, [(2, 8)], [(10, 20)])
        d = FakeDiarizer(fail="")                                   # works but...
        d.diarize = lambda audio, **kw: []                          # ...finds no voices
        out = msp.build_transcript(FakeRec([seg(2, 8, "a"), seg(10, 20, "b")]), [part], diarizer=d)
        self.assertEqual(out.text.split("\n"), ["You: a", "Them: b"])
        self.assertEqual(out.reason, msp.R_NO_VOICES)
        self.assertEqual(out.notice, msp.CALL_TOGETHER)
        self.assertFalse(out.ok)                    # a partial result: texts there, a notice to show
        self.assertTrue(out.texts)

    def test_a_call_that_took_too_long_to_tell_apart_keeps_you_and_them(self):
        part = self._part(30, [(2, 8)], [(10, 20)])
        out = msp.build_transcript(FakeRec([seg(2, 8, "a"), seg(10, 20, "b")]), [part],
                                   diarizer=FakeDiarizer(fail="timed out"))
        self.assertEqual(out.text.split("\n"), ["You: a", "Them: b"])
        self.assertEqual((out.reason, out.notice), (msp.R_TIMEOUT, msp.CALL_TOO_LONG))
        self.assertTrue(msp.is_speaker_notice(msp.CALL_TOO_LONG))

    def test_names_replace_the_numbering(self):
        part = self._part(30, [(2, 8)], [(10, 20)])
        out = msp.build_transcript(FakeRec([seg(2, 8, "a"), seg(10, 20, "b")]), [part],
                                   diarizer=FakeDiarizer(), names={4: "Anna"})
        self.assertEqual(out.text.split("\n"), ["You: a", "Anna: b"])

    def test_two_parts_keep_one_numbering_and_get_their_own_text(self):
        p1 = self._part(30, [(2, 8)], [(10, 20)])
        p2 = self._part(30, [], [(3, 12)])
        rec = FakeRec([[seg(2, 8, "one"), seg(10, 20, "two")], [seg(3, 12, "three")]])
        d = FakeDiarizer()
        out = msp.build_transcript(rec, [p1, p2], diarizer=d)
        self.assertTrue(out.ok)
        self.assertEqual(out.texts, ["You: one\nSpeaker 1: two", "Speaker 1: three"])
        self.assertEqual(len(d.diarize_calls), 1)                   # one diarization for the whole meeting
        self.assertEqual(len(rec.calls), 2)


class TestMixedAudio(unittest.TestCase):
    def setUp(self):
        if not _real_numpy():
            self.skipTest("real numpy not importable (stubbed)")
        import numpy as np
        self.np = np

    def test_without_the_two_tracks_the_mixed_audio_is_diarized_into_speakers(self):
        np = self.np
        mix = burst(np, 40, 0.05, 5)
        mix[int(10 * SR):int(15 * SR)] = 0
        mix[int(25 * SR):int(30 * SR)] = 0
        d = FakeDiarizer()
        segs = [seg(1, 9, "a"), seg(16, 24, "b"), seg(31, 39, "c")]
        out = msp.build_transcript(FakeRec(segs), [msp.Part(audio=mix)], diarizer=d)
        self.assertEqual(out.mode, "voices")
        self.assertTrue(out.ok)
        self.assertEqual(out.text, "Speaker 1: a b c")             # one voice in the fake
        self.assertEqual(d.diarize_calls, [40.0])                    # the whole mix is read

    def test_no_voices_in_the_mix_is_a_failure_with_a_notice(self):
        np = self.np
        d = FakeDiarizer()
        d.diarize = lambda audio, **kw: []
        out = msp.build_transcript(FakeRec([seg(1, 5, "a")]), [msp.Part(audio=burst(np, 10, 0.05, 1))],
                                   diarizer=d)
        self.assertEqual((out.texts, out.reason), ([], msp.R_NO_VOICES))
        self.assertIn("no separate voices were found", out.notice)


class TestEveryEndIsSaid(unittest.TestCase):
    """Every way to end without labels: a reason, one log line, a plain notice."""

    def setUp(self):
        if not _real_numpy():
            self.skipTest("real numpy not importable (stubbed)")
        import numpy as np
        self.np = np
        self.audio = burst(np, 10, 0.05, 1)

    def _run(self, rec=None, d=None, audio=None, **kw):
        with self.assertLogs("transcribe", level="INFO") as cm:
            out = msp.build_transcript(
                rec or FakeRec([seg(1, 5, "a")]), [msp.Part(audio=self.audio if audio is None else audio)],
                diarizer=d or FakeDiarizer(), **kw)
            logging.getLogger("transcribe").info("sentinel")          # assertLogs needs one record
        return out, [m for m in cm.output if "Speaker labels" in m]

    def test_the_speaker_tool_is_missing(self):
        out, log = self._run(d=FakeDiarizer(installed=False))
        self.assertEqual((out.reason, out.texts), (msp.R_NO_COMPONENT, []))
        self.assertIn("isn't part of this copy of Gusan", out.notice)
        self.assertEqual(len(log), 1)
        self.assertIn("no_component", log[0])

    def test_privacy_mode_never_downloads_and_says_so(self):
        d = FakeDiarizer(downloaded=False)
        out, log = self._run(d=d, privacy=True)
        self.assertEqual(out.reason, msp.R_PRIVACY)
        self.assertEqual(d.downloads, 0)
        self.assertIn("Privacy mode keeps Gusan offline", out.notice)
        self.assertEqual(len(log), 1)

    def test_first_use_downloads_the_model_and_shows_progress(self):
        d = FakeDiarizer(downloaded=False)
        status = []
        out, _ = self._run(d=d, status=status.append)
        self.assertEqual(d.downloads, 1)
        self.assertTrue(out.ok)
        self.assertIn("Downloading the speaker model…", status)
        self.assertTrue(any("40 MB" in s for s in status))           # its progress, in the status line

    def test_a_download_that_fails_says_check_the_connection(self):
        out, log = self._run(d=FakeDiarizer(downloaded=False, download_ok=False))
        self.assertEqual(out.reason, msp.R_DOWNLOAD)
        self.assertIn("check your internet connection", out.notice)
        self.assertEqual(len(log), 1)

    def test_a_recording_that_is_too_short(self):
        out, log = self._run(audio=self.np.zeros(8000, self.np.float32))
        self.assertEqual(out.reason, msp.R_TOO_SHORT)
        self.assertIn("too short", out.notice)
        self.assertEqual(len(log), 1)

    def test_a_recording_too_short_or_without_audio_is_info_not_a_warning(self):
        for label, parts, reason in (
                ("too short", [msp.Part(audio=self.np.zeros(8000, self.np.float32))], msp.R_TOO_SHORT),
                ("no parts", [], msp.R_NO_AUDIO)):
            with self.assertLogs("transcribe", level="INFO") as cm:
                out = msp.build_transcript(FakeRec([]), parts, diarizer=FakeDiarizer())
            skipped = [r for r in cm.records if "Speaker labels skipped" in r.getMessage()]
            self.assertEqual(len(skipped), 1, label)
            self.assertEqual(skipped[0].levelno, logging.INFO, label)          # expected: no warning
            self.assertEqual(out.reason, reason)
            self.assertTrue(msp.is_benign(reason))
        # ...while a real problem stays a warning
        with self.assertLogs("transcribe", level="INFO") as cm:
            msp.build_transcript(FakeRec([seg(1, 5, "a")]), [msp.Part(audio=self.audio)],
                                 diarizer=FakeDiarizer(installed=False))
        skipped = [r for r in cm.records if "Speaker labels skipped" in r.getMessage()]
        self.assertEqual(skipped[0].levelno, logging.WARNING)
        self.assertFalse(msp.is_benign(msp.R_NO_COMPONENT))
        self.assertFalse(msp.is_benign(msp.R_PRIVACY))

    def test_the_speech_model_finds_nothing(self):
        out, log = self._run(rec=FakeRec([]))
        self.assertEqual(out.reason, msp.R_NO_SEGMENTS)
        self.assertEqual(len(log), 1)

    def test_an_error_in_the_pass_is_caught_and_reported(self):
        out, log = self._run(rec=FakeRec([], raises=RuntimeError("model crashed")))
        self.assertEqual((out.reason, out.texts), (msp.R_ERROR, []))
        self.assertIn("something went wrong", out.notice)
        self.assertTrue(log)

    def test_a_diarizer_that_crashes_is_an_error_not_silence(self):
        out, log = self._run(d=FakeDiarizer(fail="worker exited with code 1"))
        self.assertEqual(out.reason, msp.R_ERROR)
        self.assertTrue(log)

    def test_a_diarizer_that_gives_up_is_a_timeout_with_its_own_words(self):
        out, log = self._run(d=FakeDiarizer(fail="timed out"))
        self.assertEqual((out.reason, out.texts), (msp.R_TIMEOUT, []))
        self.assertIn("took too long", out.notice)
        self.assertIn("Identify speakers", out.notice)
        self.assertTrue(log)

    def test_a_diarizer_that_found_nobody_is_no_voices_and_a_quit_or_cancel_is_quiet(self):
        out, _ = self._run(d=FakeDiarizer(fail="no speaker turns"))
        self.assertEqual(out.reason, msp.R_NO_VOICES)
        for why in ("cancelled", "app quitting"):
            out, _ = self._run(d=FakeDiarizer(fail=why))
            self.assertTrue(out.cancelled, why)
            self.assertEqual(out.notice, "")

    def test_every_notice_names_the_way_to_try_again(self):
        for reason in (msp.R_NO_AUDIO, msp.R_TOO_SHORT, msp.R_NO_COMPONENT, msp.R_PRIVACY,
                       msp.R_DOWNLOAD, msp.R_NO_SEGMENTS, msp.R_NO_VOICES, msp.R_ERROR):
            text = msp.notice_for(reason)
            self.assertTrue(text.startswith("Speaker labels weren't added"), reason)
            self.assertIn("Identify speakers", text)
            self.assertTrue(msp.is_speaker_notice(text))
        self.assertEqual(msp.notice_for(msp.R_CANCELLED), "")
        self.assertTrue(msp.is_speaker_notice(msp.CALL_TOGETHER))
        self.assertFalse(msp.is_speaker_notice("Gusan was closed during this meeting"))

    def test_cancel_is_a_quiet_end(self):
        flag = threading.Event()
        flag.set()
        out, _ = self._run(should_cancel=flag.is_set)
        self.assertTrue(out.cancelled)
        self.assertEqual(out.notice, "")
        self.assertEqual(out.texts, [])

    def test_no_parts_at_all(self):
        out = msp.build_transcript(FakeRec([]), [], diarizer=FakeDiarizer())
        self.assertEqual(out.reason, msp.R_NO_AUDIO)

    def test_nothing_is_ever_raised_whatever_the_parts_are(self):
        for bad in (None, "x", 5):
            out = msp.build_transcript(FakeRec([seg(1, 5, "a")]), [msp.Part(audio=bad)],
                                       diarizer=FakeDiarizer())
            self.assertIn(out.reason, (msp.R_ERROR, msp.R_TOO_SHORT, msp.R_NO_AUDIO))

    def test_status_callback_errors_do_not_break_the_pass(self):
        def bad(_text):
            raise RuntimeError("ui gone")
        out, _ = self._run(status=bad)
        self.assertTrue(out.ok)


class TestSavedParts(unittest.TestCase):
    def setUp(self):
        if not _real_numpy():
            self.skipTest("real numpy not importable (stubbed)")
        import numpy as np
        self.np = np

    def test_a_saved_part_is_read_from_its_files(self):
        import tempfile
        from pathlib import Path
        import audio_export
        import meeting_store
        np = self.np
        with tempfile.TemporaryDirectory() as tmp:
            mic, system = call_tracks(np, 20, [(2, 8)], [(10, 18)])
            part = Path(tmp) / "audio_part1.wav"
            audio_export.write_wav(part, (mic / 32767.0 + system / 32767.0) * 0.75)
            audio_export.write_wav_int16(meeting_store.track_path(part, "mic"), mic)
            audio_export.write_wav_int16(meeting_store.track_path(part, "system"), system)
            tracks = meeting_store.audio_tracks(tmp)
            self.assertEqual(len(tracks), 1)
            p, m, s = tracks[0]
            self.assertEqual((p.name, m.name, s.name),
                             ("audio_part1.wav", "audio_part1_mic.wav", "audio_part1_system.wav"))
            self.assertEqual(meeting_store.audio_parts(tmp), [part])    # tracks are not parts
            loaded = msp.Part.from_files(p, m, s)
            self.assertTrue(loaded.has_tracks)
            audio, mic2, sys2 = loaded.load()
            self.assertEqual(audio.dtype, np.float32)
            self.assertTrue(np.array_equal(mic2, mic))
            self.assertTrue(np.array_equal(sys2, system))
            out = msp.build_transcript(FakeRec([seg(2, 8, "a"), seg(10, 18, "b")]), [loaded],
                                       diarizer=FakeDiarizer())
            self.assertEqual(out.text.split("\n"), ["You: a", "Speaker 1: b"])
            # An old meeting: the mix only.
            only_mix = msp.Part.from_files(p)
            self.assertFalse(only_mix.has_tracks)
            self.assertEqual(only_mix.load()[1:], (None, None))


# ── notes first: the fast half at Stop, then "Them" becomes Speaker N from the saved segments ──

class TwoVoices(FakeDiarizer):
    """Hears two people on the call: the first half of what it is given is one voice,
    the second half another."""

    def diarize(self, audio, num_speakers=None, threshold=0.7, on_progress=None, should_cancel=None):
        turns = FakeDiarizer.diarize(self, audio, num_speakers, threshold, on_progress, should_cancel)
        total = len(audio) / float(SR)
        return [(s, e, 4 if (s + e) / 2 < total / 2 else 7) for s, e, _ in turns]


class Probe(FakeDiarizer):
    """A diarizer that runs ``hook(self)`` before it answers (a recording starts, a file changes...)."""

    def __init__(self, hook, **kw):
        FakeDiarizer.__init__(self, **kw)
        self.hook = hook

    def diarize(self, audio, num_speakers=None, threshold=0.7, on_progress=None, should_cancel=None):
        self.hook(self)
        return FakeDiarizer.diarize(self, audio, num_speakers, threshold, on_progress, should_cancel)


def _both_tracks_part(np, seconds, you, them, **kw):
    mic, system = call_tracks(np, seconds, you, them, **kw)
    mix = ((mic.astype(np.float32) + system.astype(np.float32)) / 32767 * 0.75).astype(np.float32)
    return msp.Part(audio=mix, mic=mic, system=system), mic, system


@unittest.skipUnless(_real_numpy(), "real numpy not importable (stubbed)")
class TestFastHalf(unittest.TestCase):
    """build_transcript(diarize=False): the re-read and the labels by source, nothing slow."""

    def setUp(self):
        import numpy as np
        self.np = np

    def test_you_and_them_without_the_speaker_tool(self):
        part, _m, _s = _both_tracks_part(self.np, 40, [(2, 6), (12, 15)], [(7, 11), (16, 36)])
        segs = [seg(2, 6, "a"), seg(7, 11, "b"), seg(12, 15, "c"), seg(16, 36, "d")]
        d = FakeDiarizer(installed=False, downloaded=False)        # nothing of it is needed
        status = []
        out = msp.build_transcript(FakeRec(segs), [part], language="en", status=status.append,
                                   diarizer=d, diarize=False)
        self.assertTrue(out.ok, (out.reason, out.notice))
        self.assertEqual(out.mode, "sources")
        self.assertEqual(out.text.split("\n"), ["You: a", "Them: b", "You: c", "Them: d"])
        self.assertEqual((d.diarize_calls, d.downloads), ([], 0))
        self.assertEqual(out.speakers, 0)
        self.assertFalse(any("Telling the voices apart" in t for t in status))
        # The labelled segments are handed back to be kept (segments.json).
        self.assertEqual([(x["text"], x["source"]) for x in out.parts[0]],
                         [("a", "you"), ("b", "them"), ("c", "you"), ("d", "them")])

    def test_privacy_mode_changes_nothing_for_the_fast_half(self):
        part, _m, _s = _both_tracks_part(self.np, 30, [(2, 8)], [(10, 20)])
        out = msp.build_transcript(FakeRec([seg(2, 8, "a"), seg(10, 20, "b")]), [part], privacy=True,
                                   diarizer=FakeDiarizer(downloaded=False), diarize=False)
        self.assertTrue(out.ok)
        self.assertEqual(out.text.split("\n"), ["You: a", "Them: b"])

    def test_a_meeting_recorded_from_the_system_sound_only_is_all_them(self):
        audio = burst(self.np, 30, 0.05, 4)
        out = msp.build_transcript(FakeRec([seg(1, 9, "one"), seg(10, 20, "two")]),
                                   [msp.Part(audio=audio, call_only=True)], diarize=False)
        self.assertTrue(out.ok, (out.reason, out.notice))
        self.assertEqual(out.mode, "call")
        self.assertEqual(out.text, "Them: one two")
        self.assertEqual({x["source"] for x in out.parts[0]}, {"them"})

    def test_without_both_tracks_there_is_nothing_to_label_by_source(self):
        out = msp.build_transcript(FakeRec([seg(1, 5, "a")]), [msp.Part(audio=burst(self.np, 10, 0.05, 1))],
                                   diarize=False)
        self.assertEqual((out.texts, out.reason), ([], msp.R_ERROR))

    def test_the_whole_pass_is_unchanged(self):
        part, _m, _s = _both_tracks_part(self.np, 40, [(2, 6)], [(7, 11), (16, 36)])
        out = msp.build_transcript(FakeRec([seg(2, 6, "a"), seg(7, 11, "b"), seg(16, 36, "d")]), [part],
                                   diarizer=FakeDiarizer())
        self.assertEqual(out.text.split("\n"), ["You: a", "Speaker 1: b d"])
        self.assertEqual(out.parts[0][1]["source"], "them")


def _call_part(np, seconds, you, them, segments, **kw):
    """(CallPart for name_the_call, the system track it reads)."""
    _mic, system = call_tracks(np, seconds, you, them, **kw)
    rows = [{"start": a, "end": b, "text": t, "source": src} for a, b, t, src in segments]
    return msp.CallPart(rows, lambda: system, seconds), system


@unittest.skipUnless(_real_numpy(), "real numpy not importable (stubbed)")
class TestNameTheCall(unittest.TestCase):
    YOU = [(2, 6), (12, 15)]
    THEM = [(7, 11), (16, 36)]
    SEGS = [(2, 6, "a", "you"), (7, 11, "b", "them"), (12, 15, "c", "you"), (16, 36, "d", "them")]

    def setUp(self):
        import numpy as np
        self.np = np

    def test_one_voice_on_the_call_is_speaker_1_and_you_stays_you(self):
        part, _ = _call_part(self.np, 40, self.YOU, self.THEM, self.SEGS)
        d = FakeDiarizer()
        out = msp.name_the_call([part], diarizer=d)          # no recorder: nothing is read a second time
        self.assertTrue(out.ok, (out.reason, out.notice))
        self.assertEqual(out.text.split("\n"), ["You: a", "Speaker 1: b", "You: c", "Speaker 1: d"])
        self.assertEqual(out.speakers, 1)
        self.assertEqual(len(d.diarize_calls), 1)
        self.assertLess(d.diarize_calls[0], 40.0)            # only the stretches the call made sound
        self.assertEqual(out.call_seconds, 24.0)

    def test_two_voices_are_speaker_1_and_speaker_2(self):
        you = [(80, 84)]
        them = [(2, 32), (40, 70)]
        segs = [(2, 32, "b", "them"), (40, 70, "d", "them"), (80, 84, "y", "you")]
        part, _ = _call_part(self.np, 90, you, them, segs)
        out = msp.name_the_call([part], diarizer=TwoVoices())
        self.assertTrue(out.ok, (out.reason, out.notice))
        self.assertEqual(out.text.split("\n"), ["Speaker 1: b", "Speaker 2: d", "You: y"])
        self.assertEqual(out.speakers, 2)

    def test_a_voice_that_talks_for_moments_is_folded_into_the_one_voice(self):
        them = [(7, 11), (16, 23), (28, 34), (40, 60)]
        segs = [(7, 11, "a", "them"), (16, 23, "b", "them"), (28, 34, "c", "them"), (40, 60, "d", "them")]
        part, _ = _call_part(self.np, 64, [], them, segs)
        d = FakeDiarizer(split_noise=True)                   # it splits a second off as a "speaker 9"
        out = msp.name_the_call([part], diarizer=d)
        self.assertEqual(out.text, "Speaker 1: a b c d")      # exactly one voice left: Speaker 1, no invented names
        self.assertEqual(out.speakers, 1)

    def test_the_call_track_is_let_go_before_the_names_are_worked_out(self):
        # The background pass reads the call's whole sound (two hours = 230 MB); only the joined
        # stretches go to the diarizer, so the track must be released before that runs - one
        # reading, not one that is held for the minutes the diarizer takes.
        import gc
        import weakref
        _mic, system = call_tracks(self.np, 40, self.YOU, self.THEM)
        alive = {}

        def loader():
            s = system.copy()
            alive["system"] = weakref.ref(s)
            return s

        class Probe2(FakeDiarizer):
            def diarize(inner, audio, *a, **k):
                gc.collect()
                alive["during"] = alive["system"]() is not None
                return FakeDiarizer.diarize(inner, audio, *a, **k)
        rows = [{"start": a, "end": b, "text": t, "source": src} for a, b, t, src in self.SEGS]
        out = msp.name_the_call([msp.CallPart(rows, loader, 40.0)], diarizer=Probe2())
        self.assertTrue(out.ok, (out.reason, out.notice))
        self.assertIs(alive["during"], False)

    def test_a_call_that_spoke_too_little_is_not_separated(self):
        part, _ = _call_part(self.np, 40, [(2, 6)], [(7, 11)], [(2, 6, "a", "you"), (7, 11, "b", "them")])
        d = FakeDiarizer(installed=False)                    # not even asked
        with self.assertLogs("transcribe", level="INFO") as cm:
            out = msp.name_the_call([part], diarizer=d)
        self.assertEqual((out.reason, out.texts, out.notice), (msp.R_BRIEF, [], msp.CALL_BRIEF))
        self.assertIn("One other voice at most is likely", out.notice)
        self.assertEqual(d.diarize_calls, [])
        self.assertTrue(msp.is_speaker_notice(msp.CALL_BRIEF))
        self.assertEqual([r.levelno for r in cm.records if "Speaker labels" in r.getMessage()], [logging.INFO])
        self.assertEqual(msp.MIN_CALL_SEC, 20.0)

    def test_nobody_on_the_call_spoke(self):
        part, _ = _call_part(self.np, 30, [(2, 12)], [], [(2, 12, "a", "you")])
        with self.assertLogs("transcribe", level="INFO") as cm:
            out = msp.name_the_call([part], diarizer=FakeDiarizer())
        self.assertEqual((out.reason, out.notice, out.texts), (msp.R_NO_CALL, "", []))
        self.assertTrue(msp.is_benign(out.reason))
        self.assertEqual([r.levelno for r in cm.records if "Speaker labels" in r.getMessage()], [logging.INFO])

    def test_when_no_voice_is_found_you_and_them_stay_and_the_notice_says_so(self):
        part, _ = _call_part(self.np, 40, self.YOU, self.THEM, self.SEGS)
        d = FakeDiarizer()
        d.diarize = lambda audio, **kw: []
        out = msp.name_the_call([part], diarizer=d)
        self.assertEqual(out.text.split("\n"), ["You: a", "Them: b", "You: c", "Them: d"])
        self.assertEqual((out.reason, out.notice), (msp.R_NO_VOICES, msp.CALL_TOGETHER))

    def test_the_speaker_tool_rules_are_the_same_as_before(self):
        part, _ = _call_part(self.np, 40, self.YOU, self.THEM, self.SEGS)
        d = FakeDiarizer(downloaded=False)
        out = msp.name_the_call([part], privacy=True, diarizer=d)         # Privacy mode: never downloads
        self.assertEqual((out.reason, out.texts, d.downloads), (msp.R_PRIVACY, [], 0))
        self.assertIn("Privacy mode keeps Gusan offline", out.notice)
        d2 = FakeDiarizer(downloaded=False)
        out2 = msp.name_the_call([part], diarizer=d2)                      # otherwise the model comes once
        self.assertEqual((d2.downloads, out2.ok), (1, True))
        out3 = msp.name_the_call([part], diarizer=FakeDiarizer(installed=False))
        self.assertEqual(out3.reason, msp.R_NO_COMPONENT)

    def test_cancel_and_quit_are_quiet_ends(self):
        part, _ = _call_part(self.np, 40, self.YOU, self.THEM, self.SEGS)
        for why in ("cancelled", "app quitting"):
            out = msp.name_the_call([part], diarizer=FakeDiarizer(fail=why))
            self.assertTrue(out.cancelled, why)
            self.assertEqual((out.notice, out.texts), ("", []))
        out = msp.name_the_call([part], should_cancel=lambda: True, diarizer=FakeDiarizer())
        self.assertTrue(out.cancelled)

    def test_a_timeout_keeps_you_and_them_with_its_own_notice(self):
        part, _ = _call_part(self.np, 40, self.YOU, self.THEM, self.SEGS)
        out = msp.name_the_call([part], diarizer=FakeDiarizer(fail="timed out"))
        self.assertEqual((out.reason, out.notice), (msp.R_TIMEOUT, msp.CALL_TOO_LONG))
        self.assertEqual(out.text.split("\n"), ["You: a", "Them: b", "You: c", "Them: d"])

    def test_a_call_sound_that_cannot_be_read_is_an_error_not_a_crash(self):
        part, _ = _call_part(self.np, 40, self.YOU, self.THEM, self.SEGS)
        part._loader = lambda: (_ for _ in ()).throw(RuntimeError("disk"))
        out = msp.name_the_call([part], diarizer=FakeDiarizer())
        self.assertEqual((out.reason, out.texts), (msp.R_ERROR, []))

    def test_two_parts_share_one_numbering(self):
        p1, _ = _call_part(self.np, 40, self.YOU, self.THEM, self.SEGS)
        p2, _ = _call_part(self.np, 40, [], [(2, 30)], [(2, 30, "z", "them")])
        d = FakeDiarizer()
        out = msp.name_the_call([p1, p2], diarizer=d)
        self.assertEqual(out.texts, ["You: a\nSpeaker 1: b\nYou: c\nSpeaker 1: d", "Speaker 1: z"])
        self.assertEqual(len(d.diarize_calls), 1)            # one diarization for the whole meeting


@unittest.skipUnless(_real_numpy(), "real numpy not importable (stubbed)")
class TestRefineMeeting(unittest.TestCase):
    """The background pass on a meeting folder: files only, atomic, never half-written."""

    YOU = [(2, 6), (12, 15)]
    THEM = [(7, 11), (16, 36)]
    OLD = "You: a\nThem: b\nYou: c\nThem: d"

    def setUp(self):
        import numpy as np
        self.np = np
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.folder = Path(self._tmp.name) / "20261021_103134"
        self.folder.mkdir()

    def _meeting(self, you=None, them=None, kind="sources", segments=None, text=None, seconds=40,
                 audio="audio_part1.wav", extra_parts=(), transcript=None, tracks=True):
        import audio_export
        np = self.np
        you = self.YOU if you is None else you
        them = self.THEM if them is None else them
        mic, system = call_tracks(np, seconds, you, them)
        mix = ((mic.astype(np.float32) + system.astype(np.float32)) / 32767 * 0.75).astype(np.float32)
        path = self.folder / audio
        if kind == "call":
            audio_export.write_wav(path, (system.astype(np.float32) / 32767.0))
        else:
            audio_export.write_wav(path, mix)
            if tracks:
                audio_export.write_wav_int16(ms.track_path(path, "mic"), mic)
                audio_export.write_wav_int16(ms.track_path(path, "system"), system)
        segs = segments or [(2, 6, "a", "you"), (7, 11, "b", "them"), (12, 15, "c", "you"), (16, 36, "d", "them")]
        entry = {"audio": audio, "kind": kind, "seconds": float(seconds), "done": False,
                 "text": text or self.OLD,
                 "segments": [{"s": a, "e": b, "t": t, "src": src} for a, b, t, src in segs]}
        data = {"version": 1, "parts": list(extra_parts) + [entry]}
        self.assertTrue(ms.write_segments(self.folder, data))
        ms.write_transcript(self.folder, transcript if transcript is not None else (text or self.OLD))
        ms.update_meta(self.folder, title="Call", notices=[])
        return entry

    def _read(self, name="transcript.txt"):
        return (self.folder / name).read_text(encoding="utf-8")

    def _no_temp(self):
        self.assertEqual([p.name for p in self.folder.iterdir() if p.name.endswith(".tmp")], [])

    def test_them_becomes_speaker_1_in_one_atomic_replace(self):
        seen = {}

        def hook(d):
            seen["backup"] = self._read(ms.BEFORE_SPEAKERS)         # kept while it works
            seen["current"] = self._read()                          # still the You / Them text
        self._meeting()
        res = msp.refine_meeting(self.folder, diarizer=Probe(hook))
        self.assertEqual((res.state, res.speakers), ("done", 1))
        self.assertEqual(seen, {"backup": self.OLD, "current": self.OLD})
        self.assertEqual(self._read(), "You: a\nSpeaker 1: b\nYou: c\nSpeaker 1: d")
        self.assertFalse((self.folder / ms.BEFORE_SPEAKERS).exists())     # success: the copy is gone
        self._no_temp()
        meta = ms.load_meta(self.folder)
        self.assertEqual(meta["notices"], [])
        self.assertIn("speakers_at", meta)
        data = ms.load_segments(self.folder)
        self.assertTrue(data["parts"][0]["done"])
        self.assertEqual(data["parts"][0]["text"], self._read())
        # Nothing left to do for it.
        self.assertEqual(msp.refine_meeting(self.folder, diarizer=FakeDiarizer()).state, "nothing")

    def test_a_short_call_stays_them_with_its_notice_and_no_pass_runs(self):
        self._meeting(them=[(7, 11)], segments=[(2, 6, "a", "you"), (7, 11, "b", "them")],
                      text="You: a\nThem: b")
        d = FakeDiarizer()
        res = msp.refine_meeting(self.folder, diarizer=d)
        self.assertEqual((res.state, res.reason), ("concluded", msp.R_BRIEF))
        self.assertEqual(d.diarize_calls, [])
        self.assertEqual(self._read(), "You: a\nThem: b")
        meta = ms.load_meta(self.folder)
        self.assertEqual(meta["notices"], [msp.CALL_BRIEF])
        self.assertIn("speakers_at", meta)
        self.assertFalse((self.folder / ms.BEFORE_SPEAKERS).exists())

    def test_a_cancel_changes_nothing(self):
        self._meeting()
        res = msp.refine_meeting(self.folder, should_cancel=lambda: True, diarizer=FakeDiarizer())
        self.assertEqual(res.state, "cancelled")
        self.assertEqual(self._read(), self.OLD)
        self.assertFalse((self.folder / ms.BEFORE_SPEAKERS).exists())
        self.assertNotIn("speakers_at", ms.load_meta(self.folder))
        self.assertFalse(ms.load_segments(self.folder)["parts"][0]["done"])       # it can run again
        self._no_temp()

    def test_a_pass_that_cannot_label_keeps_the_transcript_and_says_why(self):
        self._meeting()
        res = msp.refine_meeting(self.folder, privacy=True, diarizer=FakeDiarizer(downloaded=False))
        self.assertEqual((res.state, res.reason), ("failed", msp.R_PRIVACY))
        self.assertEqual(self._read(), self.OLD)
        meta = ms.load_meta(self.folder)
        self.assertTrue(meta["notices"][0].startswith("Speaker labels weren't added"))
        self.assertNotIn("speakers_at", meta)                  # not concluded: Identify speakers still offered
        self.assertFalse(ms.load_segments(self.folder)["parts"][0]["done"])
        # a later pass replaces that notice
        res2 = msp.refine_meeting(self.folder, diarizer=FakeDiarizer())
        self.assertEqual(res2.state, "done")
        self.assertEqual(ms.load_meta(self.folder)["notices"], [])

    def test_people_who_cannot_be_told_apart_keep_them_and_a_notice(self):
        self._meeting()
        d = FakeDiarizer()
        d.diarize = lambda audio, **kw: []
        res = msp.refine_meeting(self.folder, diarizer=d)
        self.assertEqual((res.state, res.reason), ("concluded", msp.R_NO_VOICES))
        self.assertEqual(self._read(), self.OLD)
        self.assertEqual(ms.load_meta(self.folder)["notices"], [msp.CALL_TOGETHER])

    def test_a_transcript_someone_else_rewrote_is_never_replaced(self):
        self._meeting()
        (self.folder / "transcript.txt").write_text("Speaker 1: someone's own rewrite", encoding="utf-8")
        d = FakeDiarizer()
        self.assertEqual(msp.refine_meeting(self.folder, diarizer=d).state, "nothing")
        self.assertEqual(d.diarize_calls, [])
        self.assertEqual(self._read(), "Speaker 1: someone's own rewrite")

        def hook(_d):                                          # ...or while the pass works
            (self.folder / "transcript.txt").write_text("You: a\nSpeaker 1: b\nYou: c\nSpeaker 2: d",
                                                        encoding="utf-8")
        self._meeting()
        res = msp.refine_meeting(self.folder, diarizer=Probe(hook))
        self.assertEqual(res.state, "concluded")
        self.assertEqual(self._read(), "You: a\nSpeaker 1: b\nYou: c\nSpeaker 2: d")

    def test_the_transcript_is_replaced_whole_or_not_at_all(self):
        self._meeting()
        real = ms.os.replace

        def fail_for_transcript(src, dst, *a, **k):
            if str(dst).endswith("transcript.txt"):
                raise PermissionError("locked by a virus scanner")
            return real(src, dst, *a, **k)
        with mock.patch.object(ms.os, "replace", side_effect=fail_for_transcript), \
                mock.patch.object(ms.time, "sleep"):
            res = msp.refine_meeting(self.folder, diarizer=FakeDiarizer())
        self.assertEqual((res.state, res.reason), ("failed", msp.R_ERROR))
        self.assertEqual(self._read(), self.OLD)               # the old file, complete
        self._no_temp()
        self.assertFalse(ms.load_segments(self.folder)["parts"][0]["done"])

    def test_a_crash_during_the_pass_leaves_the_you_and_them_transcript(self):
        class Killed(BaseException):
            pass

        def hook(_d):
            raise Killed()                                      # the process dies here
        self._meeting()
        with self.assertRaises(Killed):
            msp.refine_meeting(self.folder, diarizer=Probe(hook))
        self.assertEqual(self._read(), self.OLD)
        self.assertEqual(self._read(ms.BEFORE_SPEAKERS), self.OLD)   # kept: the run did not end
        self._no_temp()
        self.assertEqual(ms.load_transcript(self.folder), self.OLD)
        # the next pass starts clean from the same files
        self.assertEqual(msp.refine_meeting(self.folder, diarizer=FakeDiarizer()).state, "done")
        self.assertFalse((self.folder / ms.BEFORE_SPEAKERS).exists())

    def test_a_crash_at_every_step_of_the_finish_leaves_a_whole_transcript_and_nothing_to_redo(self):
        # The process dies right after each write (the transcript, segments.json) and right
        # before the last (the meeting's notice): the transcript is always one of the two whole
        # versions, a later pass never replaces it a second time, and nothing is left half done.
        class Killed(BaseException):
            pass
        named = "You: a\nSpeaker 1: b\nYou: c\nSpeaker 1: d"
        for dying, target, expected in (("before segments.json", "write_segments", named),
                                        ("before the notice", "update_meta", named),
                                        ("at the transcript", "write_transcript", self.OLD)):
            with self.subTest(dying):
                self.setUp()
                self._meeting()

                def die(*a, **k):
                    raise Killed()
                with mock.patch.object(ms, target, side_effect=die), self.assertRaises(Killed):
                    msp.refine_meeting(self.folder, diarizer=FakeDiarizer())
                self.assertEqual(self._read(), expected)
                self.assertEqual(ms.load_transcript(self.folder), expected)
                self._no_temp()
                if expected == named:
                    # the new text is there; a later pass finds nothing of the old text to replace
                    d = FakeDiarizer()
                    self.assertEqual(msp.refine_meeting(self.folder, diarizer=d).state, "nothing")
                    self.assertEqual(d.diarize_calls, [])
                    self.assertEqual(self._read(), named)
                else:
                    self.assertEqual(msp.refine_meeting(self.folder, diarizer=FakeDiarizer()).state, "done")
                    self.assertEqual(self._read(), named)

    def test_a_pass_that_raises_inside_is_a_failed_result_not_an_exception(self):
        self._meeting()
        d = FakeDiarizer()
        d.diarize = mock.Mock(side_effect=RuntimeError("model crashed"))
        res = msp.refine_meeting(self.folder, diarizer=d)
        self.assertEqual(res.state, "failed")
        self.assertEqual(self._read(), self.OLD)

    def test_nothing_to_do_without_segments_a_folder_or_the_calls_sound(self):
        d = FakeDiarizer()
        self.assertEqual(msp.refine_meeting(self.folder / "missing", diarizer=d).state, "nothing")
        self.assertEqual(msp.refine_meeting(self.folder, diarizer=d).state, "nothing")       # no segments.json
        self._meeting(tracks=False)                                                          # its tracks are gone
        with self.assertLogs("transcribe", level="INFO") as cm:
            res = msp.refine_meeting(self.folder, diarizer=d)
        self.assertEqual((res.state, res.reason), ("nothing", msp.R_NO_AUDIO))
        self.assertTrue(all(r.levelno == logging.INFO for r in cm.records))
        self.assertEqual(self._read(), self.OLD)
        self.assertEqual(d.diarize_calls, [])
        (self.folder / "segments.json").write_text("{not json", encoding="utf-8")
        self.assertEqual(msp.refine_meeting(self.folder, diarizer=d).state, "nothing")

    def test_a_meeting_recorded_from_the_system_sound_only_reads_the_recording_itself(self):
        text = "Them: b d"
        self._meeting(you=[], them=[(7, 11), (16, 36)], kind="call",
                      segments=[(7, 11, "b", "them"), (16, 36, "d", "them")], text=text)
        d = FakeDiarizer()
        res = msp.refine_meeting(self.folder, diarizer=d)
        self.assertEqual(res.state, "done")
        self.assertEqual(self._read(), "Speaker 1: b d")

    def test_only_the_part_still_waiting_is_changed_in_a_continued_meeting(self):
        done_part = {"audio": "audio_part1.wav", "kind": "sources", "seconds": 40.0, "done": True,
                     "text": "You: one\nSpeaker 1: two", "segments": [{"s": 1, "e": 2, "t": "x", "src": "you"}]}
        both = "You: one\nSpeaker 1: two\n\n— Resumed 2026-10-21 11:20 —\n\n" + self.OLD
        self._meeting(audio="audio_part2.wav", extra_parts=[done_part], transcript=both)
        res = msp.refine_meeting(self.folder, diarizer=FakeDiarizer())
        self.assertEqual(res.state, "done")
        self.assertEqual(self._read(), "You: one\nSpeaker 1: two\n\n— Resumed 2026-10-21 11:20 —\n\n"
                                       "You: a\nSpeaker 1: b\nYou: c\nSpeaker 1: d")

    def test_a_folder_deleted_while_it_works_is_not_an_error(self):
        import shutil
        self._meeting()

        def hook(_d):
            shutil.rmtree(self.folder)
        res = msp.refine_meeting(self.folder, diarizer=Probe(hook))
        self.assertIn(res.state, ("cancelled", "failed", "concluded", "nothing"))     # quietly: nothing raised
        self.assertFalse(self.folder.exists())                           # and nothing re-created it

    def test_a_meeting_deleted_while_it_works_stops_the_pass_at_once(self):
        import shutil
        self._meeting()
        asked = {}

        class Cancellable(FakeDiarizer):
            """Like the real child process: it ends when asked to."""

            def diarize(inner, audio, num_speakers=None, threshold=0.7, on_progress=None, should_cancel=None):
                shutil.rmtree(self.folder)                      # the meeting is deleted from the page
                asked["cancel"] = bool(should_cancel and should_cancel())
                inner.last_error = "cancelled" if asked["cancel"] else ""
                return []
        res = msp.refine_meeting(self.folder, diarizer=Cancellable())
        self.assertTrue(asked["cancel"])                         # told to stop - not left to finish the minutes
        self.assertEqual(res.state, "cancelled")
        self.assertFalse(self.folder.exists())                   # nothing re-created the folder or its files

    def test_a_delete_that_could_not_finish_leaves_nothing_of_the_text_written_back(self):
        self._meeting()
        segments_before = (self.folder / "segments.json").read_text(encoding="utf-8")

        def hook(_d):                       # the page deleted what it could; the held-open recording stays
            for name in ("transcript.txt", "notes.md", "meta.json", ms.BEFORE_SPEAKERS):
                p = self.folder / name
                if p.exists():
                    p.unlink()
        res = msp.refine_meeting(self.folder, diarizer=Probe(hook))
        self.assertEqual(res.state, "cancelled")
        self.assertFalse((self.folder / "transcript.txt").exists())          # not written back
        self.assertFalse((self.folder / "meta.json").exists())               # no notice, no speakers_at made up
        self.assertEqual((self.folder / "segments.json").read_text(encoding="utf-8"), segments_before)   # untouched
        self._no_temp()

    def test_the_callers_own_cancel_still_works_next_to_the_folder_check(self):
        self._meeting()
        calls = []
        res = msp.refine_meeting(self.folder, should_cancel=lambda: calls.append(1) or True,
                                 diarizer=FakeDiarizer())
        self.assertEqual(res.state, "cancelled")
        self.assertTrue(calls)
        self.assertEqual(self._read(), self.OLD)

    def test_a_transcript_held_for_a_moment_is_written_after_a_short_wait(self):
        # A virus scanner / a reader holds transcript.txt for longer than the file helper's own few
        # tries: the pass cost minutes, so it waits and tries again instead of throwing it away.
        self._meeting()
        real = ms.os.replace
        refusals = {"n": 0}

        def held(src, dst, *a, **k):
            if str(dst).endswith("transcript.txt") and refusals["n"] < 7:
                refusals["n"] += 1
                raise PermissionError("held by a virus scanner")
            return real(src, dst, *a, **k)
        with mock.patch.object(ms.os, "replace", side_effect=held), mock.patch.object(ms.time, "sleep"):
            res = msp.refine_meeting(self.folder, diarizer=FakeDiarizer())
        self.assertEqual(res.state, "done")
        self.assertGreater(refusals["n"], 5)                     # it really went beyond one write's tries
        self.assertEqual(self._read(), "You: a\nSpeaker 1: b\nYou: c\nSpeaker 1: d")
        self._no_temp()


if __name__ == "__main__":
    unittest.main()
