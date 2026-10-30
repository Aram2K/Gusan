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
    """The speaker tool without sherpa: availability by switches. embed_spans() (what the call's
    speaker pass uses) reads the audio it is given and returns one voice print per stretch that has
    sound - the voice is ``voices(start, end)`` (0 for everyone by default; ``split_noise`` gives the
    second stretch a voice of its own) - like a speaker model hearing what the test wants it to hear.
    diarize() (the old sliding-window engine, still used for a recording without its two tracks) returns
    one speaker's turns over every stretch with sound. The pure label functions are the real ones."""

    def __init__(self, installed=True, downloaded=True, download_ok=True, split_noise=False,
                 fail="", voices=None):
        self.installed, self.downloaded, self.download_ok = installed, downloaded, download_ok
        self.split_noise = split_noise
        self.fail = fail
        self.voices = voices
        self.downloads = 0
        self.diarize_calls = []
        self.embed_calls = []               # per embed_spans call: {"spans": [...], "audio_seconds": s}
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

    def embed_spans(self, audio, spans, on_progress=None, should_cancel=None):
        import numpy as np
        spans = [tuple(s) for s in spans]
        self.embed_calls.append({"spans": spans, "audio_seconds": len(audio) / float(SR)})
        self.last_error = self.fail
        if self.fail:
            return [], self.fail
        a = np.asarray(audio)
        scale = 1.0 / 32767.0 if a.dtype.kind in "iu" else 1.0
        vectors = []
        for i, (start, end) in enumerate(spans):
            if should_cancel is not None and should_cancel():
                self.last_error = "cancelled"
                return vectors, "cancelled"
            x = a[int(start * SR):int(end * SR)].astype(np.float32) * scale
            if len(x) < SR or float(np.sqrt(np.mean(x * x))) < 0.001:
                vectors.append(None)
            else:
                voice = 9 if (self.split_noise and i == 1) else (self.voices(start, end) if self.voices else 0)
                vectors.append([1.0 if k == voice else 0.0 for k in range(12)])
            if on_progress:
                on_progress(i + 1, len(spans))
        return vectors, ""

    def __getattr__(self, name):                     # the pure functions are the real ones
        return getattr(diarization, name)


def slow_download_diarizer(steps, **kw):
    """A diarizer whose model is not here yet; its download takes ``steps`` chunks and understands cancel
    and byte progress, like diarization.download_models."""
    class Down(FakeDiarizer):
        def download_models(inner, on_progress=None, should_cancel=None, on_bytes=None):
            inner.downloads += 1
            for i in range(steps):
                if should_cancel is not None and should_cancel():
                    inner.stopped_at = i
                    return False
                if on_bytes is not None:
                    on_bytes((i + 1) * 10, steps * 10)
            inner.downloaded = True
            return True
    return Down(downloaded=False, **kw)


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

    def test_cancel_during_the_first_download_at_stop_ends_it_at_once_and_quietly(self):
        part = self._part(30, [(2, 8)], [(10, 20)])
        d = slow_download_diarizer(50)
        state = {"on": False}
        d.download_models_orig = d.download_models

        def download(on_progress=None, should_cancel=None, on_bytes=None):
            state["on"] = True                               # the person stops it while the model comes down
            return d.download_models_orig(on_progress=on_progress, should_cancel=should_cancel, on_bytes=on_bytes)
        d.download_models = download
        out = msp.build_transcript(FakeRec([seg(2, 8, "a"), seg(10, 20, "b")]), [part], diarizer=d,
                                   should_cancel=lambda: state["on"])
        self.assertTrue(out.cancelled)
        self.assertEqual(d.stopped_at, 0)                    # before the first chunk, not after fifty
        self.assertEqual(out.texts, [])

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
    """Hears two people on the call. The voice prints: whoever speaks before 35 s is one voice, whoever
    speaks after another. The old sliding-window engine: the first half of what it is given is one voice,
    the second half another."""

    def __init__(self, **kw):
        FakeDiarizer.__init__(self, voices=lambda start, end: 0 if start < 35 else 1, **kw)

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

    def embed_spans(self, audio, spans, on_progress=None, should_cancel=None):
        self.hook(self)
        return FakeDiarizer.embed_spans(self, audio, spans, on_progress, should_cancel)


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


@unittest.skipUnless(_real_numpy(), "real numpy not importable (stubbed)")
class TestWordsThatAreAlreadyThere(unittest.TestCase):
    """build_transcript(reader=...): a cloud session's live pieces are the segments - the
    recording is NOT read again, so the cloud's words stay and only the labels are worked out."""

    def setUp(self):
        import numpy as np
        self.np = np
        self.part, _m, _s = _both_tracks_part(np, 40, [(2, 6), (12, 15)], [(7, 11), (16, 36)])
        # (Armenian, as a cloud model writes it)
        self.pieces = [seg(2, 6, "Բարև ձեզ"), seg(7, 11, "Ինչպե՞ս եք"), seg(12, 15, "Շնորհակալություն"),
                       seg(16, 36, "Եկեք սկսենք")]

    def test_the_pieces_are_labelled_by_source_and_no_whisper_runs(self):
        rec = FakeRec([seg(0, 1, "local whisper text that must never appear")])
        status = []
        asked = []

        def reader(audio, language=None, should_cancel=None, on_progress=None):
            asked.append((len(audio) / 16000.0, language))
            return [dict(s) for s in self.pieces]
        d = FakeDiarizer(installed=False, downloaded=False)
        out = msp.build_transcript(rec, [self.part], language="hy", status=status.append, diarizer=d,
                                   diarize=False, reader=reader)
        self.assertTrue(out.ok, (out.reason, out.notice))
        self.assertEqual(rec.calls, [])                              # transcribe_segments was never called
        self.assertEqual(asked, [(40.0, "hy")])
        self.assertEqual(out.text.split("\n"),
                         ["You: Բարև ձեզ", "Them: Ինչպե՞ս եք", "You: Շնորհակալություն", "Them: Եկեք սկսենք"])
        self.assertNotIn("local whisper", out.text)
        self.assertFalse(any("Re-reading" in s for s in status))     # nothing is read again
        self.assertEqual([(x["source"], x["start"], x["end"]) for x in out.parts[0]],
                         [("you", 2.0, 6.0), ("them", 7.0, 11.0), ("you", 12.0, 15.0), ("them", 16.0, 36.0)])

    def test_the_whole_pass_names_the_voices_on_those_words_too(self):
        out = msp.build_transcript(FakeRec([]), [self.part], diarizer=FakeDiarizer(),
                                   reader=lambda audio, **kw: [dict(s) for s in self.pieces])
        self.assertTrue(out.ok, (out.reason, out.notice))
        self.assertEqual(out.text.split("\n"),
                         ["You: Բարև ձեզ", "Speaker 1: Ինչպե՞ս եք", "You: Շնորհակալություն",
                          "Speaker 1: Եկեք սկսենք"])

    def test_a_reader_with_nothing_is_the_same_end_as_a_whisper_that_found_nothing(self):
        out = msp.build_transcript(FakeRec([]), [self.part], diarizer=FakeDiarizer(), diarize=False,
                                   reader=lambda audio, **kw: [])
        self.assertEqual((out.texts, out.reason), ([], msp.R_NO_SEGMENTS))

    def test_the_model_of_the_reread_is_passed_on_only_when_there_is_one(self):
        seen = []

        class Rec(FakeRec):
            def transcribe_segments(self, audio, language=None, model_name=None, on_progress=None,
                                    should_cancel=None):
                seen.append(model_name)
                return [seg(2, 6, "a")]
        for model, want in ((None, None), ("small", "small")):
            seen.clear()
            msp.build_transcript(Rec([]), [self.part], diarizer=FakeDiarizer(), diarize=False, model=model)
            self.assertEqual(seen, [want])

        class OldRec(FakeRec):                      # a recorder whose method has no model_name
            def transcribe_segments(self, audio, language=None, on_progress=None, should_cancel=None):
                return [seg(2, 6, "a")]
        out = msp.build_transcript(OldRec([]), [self.part], diarizer=FakeDiarizer(), diarize=False)
        self.assertTrue(out.ok)                     # Dictation's own re-read: the call is unchanged


def _call_part(np, seconds, you, them, segments, key="", **kw):
    """(CallPart for name_the_call, the system track it reads)."""
    _mic, system = call_tracks(np, seconds, you, them, **kw)
    rows = [{"start": a, "end": b, "text": t, "source": src} for a, b, t, src in segments]
    return msp.CallPart(rows, lambda: system, seconds, key=key), system


@unittest.skipUnless(_real_numpy(), "real numpy not importable (stubbed)")
class TestNameTheCall(unittest.TestCase):
    """The second half of the speaker pass, voice-print engine: one voice print per stretch of the call's
    speech (the fake speaker model hears what each test wants it to hear)."""
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
        self.assertEqual(len(d.embed_calls), 1)
        spans = d.embed_calls[0]["spans"]
        self.assertEqual(spans[0], (7.0, 11.0))              # only the stretches the call spoke: never the user's
        self.assertEqual(len(spans), 4)                      # 4 s, and 20 s cut into three pieces
        self.assertTrue(all(16.0 <= a and b <= 36.0 for a, b in spans[1:]))
        self.assertEqual(out.call_seconds, 24.0)
        self.assertEqual(out.units, 4)
        self.assertEqual(out.shares, [1.0])
        self.assertEqual(d.diarize_calls, [])                # the sliding-window engine is not used

    def test_two_voices_are_speaker_1_and_speaker_2(self):
        you = [(80, 84)]
        them = [(2, 32), (40, 70)]
        segs = [(2, 32, "b", "them"), (40, 70, "d", "them"), (80, 84, "y", "you")]
        part, _ = _call_part(self.np, 90, you, them, segs)
        out = msp.name_the_call([part], diarizer=TwoVoices())
        self.assertTrue(out.ok, (out.reason, out.notice))
        self.assertEqual(out.text.split("\n"), ["Speaker 1: b", "Speaker 2: d", "You: y"])
        self.assertEqual(out.speakers, 2)
        self.assertEqual(out.shares, [0.5, 0.5])

    def test_a_voice_that_talks_for_moments_is_folded_into_the_one_voice(self):
        them = [(7, 11), (16, 23), (28, 34), (40, 60)]
        segs = [(7, 11, "a", "them"), (16, 23, "b", "them"), (28, 34, "c", "them"), (40, 60, "d", "them")]
        part, _ = _call_part(self.np, 64, [], them, segs)
        d = FakeDiarizer(split_noise=True)                   # one stretch gets a voice of its own
        out = msp.name_the_call([part], diarizer=d)
        self.assertEqual(out.text, "Speaker 1: a b c d")      # exactly one voice left: Speaker 1, no invented names
        self.assertEqual(out.speakers, 1)

    def test_the_call_track_is_let_go_before_the_names_are_worked_out(self):
        # The background pass reads the call's whole sound (two hours = 230 MB); the voice prints are made from
        # slices of it, so the track must be released once they are - not held while the people are told apart.
        import gc
        import weakref
        _mic, system = call_tracks(self.np, 40, self.YOU, self.THEM)
        alive = {}

        def loader():
            s = system.copy()
            alive["system"] = weakref.ref(s)
            return s

        real = msp.voice_prints.cluster

        def probe(*a, **k):
            gc.collect()
            alive["during"] = alive["system"]() is not None
            return real(*a, **k)
        rows = [{"start": a, "end": b, "text": t, "source": src} for a, b, t, src in self.SEGS]
        with mock.patch.object(msp.voice_prints, "cluster", probe):
            out = msp.name_the_call([msp.CallPart(rows, loader, 40.0)], diarizer=FakeDiarizer())
        self.assertTrue(out.ok, (out.reason, out.notice))
        self.assertIs(alive["during"], False)

    def test_a_call_that_spoke_too_little_is_not_separated(self):
        part, _ = _call_part(self.np, 40, [(2, 6)], [(7, 11)], [(2, 6, "a", "you"), (7, 11, "b", "them")])
        d = FakeDiarizer(installed=False)                    # not even asked
        with self.assertLogs("transcribe", level="INFO") as cm:
            out = msp.name_the_call([part], diarizer=d)
        self.assertEqual((out.reason, out.texts, out.notice), (msp.R_BRIEF, [], msp.CALL_BRIEF))
        self.assertIn("One other voice at most is likely", out.notice)
        self.assertEqual(d.embed_calls, [])
        self.assertTrue(msp.is_speaker_notice(msp.CALL_BRIEF))
        self.assertEqual([r.levelno for r in cm.records if "Speaker labels" in r.getMessage()], [logging.INFO])
        self.assertEqual(msp.MIN_CALL_SEC, 20.0)

    # -- the stretches that are mostly silence (the real call: 14 of them made a "second speaker") --
    def _quiet_call(self, quiet_seconds, quiet_every=3, count=40, **kw):
        """A call of ``count`` stretches of 5 s, ten seconds apart. Every ``quiet_every``-th holds only
        ``quiet_seconds`` of sound (a short word and silence, like a fixed 5 second cloud piece); the rest are
        all speech. Returns (the part, the spans of the quiet stretches, the diarizer: the quiet ones sound
        like another voice)."""
        spans = [(i * 10.0, i * 10.0 + 5.0) for i in range(count)]
        quiet = [s for i, s in enumerate(spans) if i % quiet_every == quiet_every - 1]
        sound = [(a, a + quiet_seconds) if (a, b) in quiet else (a, b) for a, b in spans]
        rows = [(a, b, "w%d" % i, "them") for i, (a, b) in enumerate(spans)]
        part, _ = _call_part(self.np, count * 10.0 + 5.0, [], sound, rows, **kw)
        d = FakeDiarizer(voices=lambda start, end: 1 if (round(start, 1), round(end, 1)) in quiet else 0)
        return part, quiet, d

    def test_stretches_that_are_mostly_silence_get_no_voice_print_and_are_no_second_speaker(self):
        part, quiet, d = self._quiet_call(1.0, quiet_every=3, count=45)          # 15 stretches of one second of speech
        out = msp.name_the_call([part], diarizer=d)
        self.assertTrue(out.ok, (out.reason, out.notice))
        asked = [span for call in d.embed_calls for span in call["spans"]]
        self.assertEqual(len(asked), 30)                                       # the 15 quiet ones are not asked for
        self.assertEqual(out.units, 30)
        self.assertFalse([s for s in asked if (round(s[0], 1), round(s[1], 1)) in quiet])
        self.assertEqual(out.speakers, 1)                                      # one voice, as there really was
        self.assertEqual(out.text.count("Speaker"), 1)                         # ... one block, every word in it
        self.assertTrue(out.text.startswith("Speaker 1: w0 w1 w2 w3"))         # (the quiet stretches' words included)
        self.assertEqual(msp.result_line(msp.PassResult(msp.PassResult.DONE, "", "", out.speakers)),
                         (msp.LINE_ONE, "ok"))

    def test_without_the_speech_floor_those_stretches_would_have_been_a_second_speaker(self):
        part, quiet, d = self._quiet_call(1.0, quiet_every=3, count=45)
        with mock.patch.object(msp.voice_prints, "MIN_SPEECH_SEC", 0.0), \
                mock.patch.object(msp.voice_prints, "gate_units",
                                  lambda units, speech, minimum=0.0: (units, speech)):
            out = msp.name_the_call([part], diarizer=d)
        self.assertEqual(out.speakers, 2)                                      # what the call got before the floor

    def test_a_second_voice_that_really_speaks_is_still_found(self):
        part, quiet, d = self._quiet_call(4.0, quiet_every=3, count=45)          # the same stretches, with real speech
        out = msp.name_the_call([part], diarizer=d)
        self.assertTrue(out.ok, (out.reason, out.notice))
        self.assertEqual(out.speakers, 2)
        self.assertEqual(out.units, 45)

    def test_a_stretch_whose_sound_cannot_be_measured_is_kept_by_its_length(self):
        part, _quiet, d = self._quiet_call(1.0, quiet_every=3, count=45)
        with mock.patch.object(msp.speaker_labels, "frame_rms", side_effect=RuntimeError("numpy trouble")):
            out = msp.name_the_call([part], diarizer=d)
        self.assertTrue(out.ok, (out.reason, out.notice))
        self.assertEqual(out.units, 45)                                        # nothing was dropped for it

    def test_the_sound_of_a_one_part_meeting_is_read_once_and_a_longer_one_once_per_use(self):
        part, _ = _call_part(self.np, 40, self.YOU, self.THEM, self.SEGS)
        loads = []
        real = part._loader
        part._loader = lambda: loads.append(1) or real()
        self.assertTrue(msp.name_the_call([part], diarizer=FakeDiarizer()).ok)
        self.assertEqual(len(loads), 1)                                        # for the speech in it and for the prints
        p1, _ = _call_part(self.np, 40, self.YOU, self.THEM, self.SEGS, key="p1")
        p2, _ = _call_part(self.np, 40, [], [(2, 30)], [(2, 30, "z", "them")], key="p2")
        loads2 = []
        for p in (p1, p2):
            real = p._loader
            p._loader = (lambda real=real: loads2.append(1) or real())
        self.assertTrue(msp.name_the_call([p1, p2], diarizer=FakeDiarizer()).ok)
        self.assertEqual(len(loads2), 4)                                       # (never two parts held at once)

    def test_the_grouping_can_be_cancelled_and_ends_quietly(self):
        part, _ = _call_part(self.np, 40, self.YOU, self.THEM, self.SEGS)
        with mock.patch.object(msp.voice_prints, "cluster", side_effect=msp.voice_prints.Cancelled()):
            out = msp.name_the_call([part], diarizer=FakeDiarizer())
        self.assertTrue(out.cancelled)
        self.assertEqual((out.notice, out.texts), ("", []))

    # -- the speaker model comes down once, and Cancel works while it does --
    def _downloader(self, steps, **kw):
        return slow_download_diarizer(steps, **kw)

    def test_the_download_reports_its_bytes_as_a_stage_of_its_own(self):
        part, _ = _call_part(self.np, 40, self.YOU, self.THEM, self.SEGS)
        d = self._downloader(4)
        seen = []
        out = msp.name_the_call([part], diarizer=d, progress=lambda *a: seen.append(a))
        self.assertTrue(out.ok, (out.reason, out.notice))
        self.assertEqual([e for e in seen if e[0] == "download"],
                         [("download", 0, 0), ("download", 10, 40), ("download", 20, 40),
                          ("download", 30, 40), ("download", 40, 40)])
        self.assertLess(seen.index(("download", 40, 40)), seen.index(("reading", 0, 0)))

    def test_cancel_during_the_download_ends_it_at_once_and_quietly(self):
        part, _ = _call_part(self.np, 40, self.YOU, self.THEM, self.SEGS)
        d = self._downloader(50)
        cancel = {"now": False}

        def progress(stage, done, total):
            if stage == "download" and done >= 20:
                cancel["now"] = True
        out = msp.name_the_call([part], diarizer=d, progress=progress, should_cancel=lambda: cancel["now"])
        self.assertTrue(out.cancelled)                                           # not "the download did not finish"
        self.assertEqual((out.reason, out.notice, out.texts), (msp.R_CANCELLED, "", []))
        self.assertLessEqual(d.stopped_at, 3)                                    # between two chunks, not at the end
        self.assertEqual(d.embed_calls, [])

    def test_an_error_that_arrives_together_with_a_cancel_is_a_quiet_cancel(self):
        # (the child was ended because the person pressed Cancel / started to dictate: its exit code is no news)
        part, _ = _call_part(self.np, 40, self.YOU, self.THEM, self.SEGS)
        state = {"asked": False}
        d = FakeDiarizer()

        def embed(audio, spans, on_progress=None, should_cancel=None):
            state["asked"] = True
            return [], "worker exited with code 3"
        d.embed_spans = embed
        out = msp.name_the_call([part], diarizer=d, should_cancel=lambda: state["asked"])
        self.assertTrue(out.cancelled)
        self.assertEqual((out.notice, out.texts), ("", []))
        out = msp.name_the_call([part], diarizer=d)                          # (without a cancel it is an error)
        self.assertEqual((out.reason, out.notice), (msp.R_ERROR, msp.CALL_TOGETHER))

    def test_a_download_that_fails_is_still_said_in_plain_words(self):
        part, _ = _call_part(self.np, 40, self.YOU, self.THEM, self.SEGS)
        out = msp.name_the_call([part], diarizer=FakeDiarizer(downloaded=False, download_ok=False))
        self.assertEqual(out.reason, msp.R_DOWNLOAD)
        self.assertIn("check your internet connection", out.notice)

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
        d.embed_spans = lambda audio, spans, **kw: ([None] * len(spans), "")     # nothing to hear: silence
        out = msp.name_the_call([part], diarizer=d)
        self.assertEqual(out.text.split("\n"), ["You: a", "Them: b", "You: c", "Them: d"])
        self.assertEqual((out.reason, out.notice), (msp.R_NO_VOICES, msp.CALL_TOGETHER))

    def test_a_call_too_choppy_to_cut_into_stretches_is_no_voices_too(self):
        segs = [(i * 3.0, i * 3.0 + 1.0, "w%d" % i, "them") for i in range(25)]      # 25 s in lone single seconds
        part, _ = _call_part(self.np, 80, [], [(a, b) for a, b, _t, _s in segs], segs)
        d = FakeDiarizer()
        out = msp.name_the_call([part], diarizer=d)
        self.assertEqual(d.embed_calls, [])                  # nothing worth a voice print: the model is not asked
        self.assertEqual((out.reason, out.notice), (msp.R_NO_VOICES, msp.CALL_TOGETHER))
        self.assertEqual(out.units, 0)

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
        d = FakeDiarizer()
        out = msp.name_the_call([part], should_cancel=lambda: True, diarizer=d)
        self.assertTrue(out.cancelled)
        self.assertEqual(d.embed_calls, [])

    def test_a_timeout_keeps_you_and_them_with_its_own_notice(self):
        part, _ = _call_part(self.np, 40, self.YOU, self.THEM, self.SEGS)
        for why in ("timed out", "stalled"):
            out = msp.name_the_call([part], diarizer=FakeDiarizer(fail=why))
            self.assertEqual((out.reason, out.notice), (msp.R_TIMEOUT, msp.CALL_TOO_LONG), why)
            self.assertEqual(out.text.split("\n"), ["You: a", "Them: b", "You: c", "Them: d"])

    def test_any_other_failure_of_the_speaker_tool_is_an_error_with_the_try_again_notice(self):
        part, _ = _call_part(self.np, 40, self.YOU, self.THEM, self.SEGS)
        for why in ("worker exited with code 3", "unavailable", "could not start: boom", "incomplete"):
            out = msp.name_the_call([part], diarizer=FakeDiarizer(fail=why))
            self.assertEqual((out.reason, out.notice), (msp.R_ERROR, msp.CALL_TOGETHER), why)

    def test_a_call_sound_that_cannot_be_read_is_an_error_not_a_crash(self):
        part, _ = _call_part(self.np, 40, self.YOU, self.THEM, self.SEGS)
        part._loader = lambda: (_ for _ in ()).throw(RuntimeError("disk"))
        out = msp.name_the_call([part], diarizer=FakeDiarizer())
        self.assertEqual((out.reason, out.texts), (msp.R_ERROR, []))

    def test_a_call_sound_that_is_missing_is_no_voices(self):
        part, _ = _call_part(self.np, 40, self.YOU, self.THEM, self.SEGS)
        part._loader = lambda: None
        d = FakeDiarizer()
        out = msp.name_the_call([part], diarizer=d)
        self.assertEqual((out.reason, out.notice), (msp.R_NO_VOICES, msp.CALL_TOGETHER))
        self.assertEqual(d.embed_calls, [])

    def test_two_parts_share_one_numbering_and_the_second_keeps_its_place_in_time(self):
        p1, _ = _call_part(self.np, 40, self.YOU, self.THEM, self.SEGS)
        p2, _ = _call_part(self.np, 40, [], [(2, 30)], [(2, 30, "z", "them")])
        d = FakeDiarizer()
        out = msp.name_the_call([p1, p2], diarizer=d)
        self.assertEqual(out.texts, ["You: a\nSpeaker 1: b\nYou: c\nSpeaker 1: d", "Speaker 1: z"])
        self.assertEqual(len(d.embed_calls), 2)              # one run of voice prints per part's sound ...
        # ... and the people are told apart across the whole meeting: the second part's voice is another
        # voice here (it must not be matched against the first part's turns at the wrong time)
        calls = {"n": 0}

        def second_part_is_someone_else(start, end):
            return 1 if calls["n"] > 1 else 0
        d2 = FakeDiarizer(voices=second_part_is_someone_else)
        real = d2.embed_spans

        def counting(audio, spans, **kw):
            calls["n"] += 1
            return real(audio, spans, **kw)
        d2.embed_spans = counting
        calls["n"] = 0                                       # (the first embed_spans call is part 1, the second part 2)
        out2 = msp.name_the_call([p1, p2], diarizer=d2)
        self.assertEqual(out2.texts, ["You: a\nSpeaker 1: b\nYou: c\nSpeaker 1: d", "Speaker 2: z"])
        self.assertEqual(out2.speakers, 2)

    def test_the_progress_runs_from_the_first_voice_print_to_the_last(self):
        part, _ = _call_part(self.np, 40, self.YOU, self.THEM, self.SEGS)
        seen = []
        out = msp.name_the_call([part], diarizer=FakeDiarizer(), progress=lambda *a: seen.append(a))
        self.assertTrue(out.ok)
        # the sound is read first (how many stretches there will be is not known yet), then again for the prints
        self.assertEqual([e for e in seen if e[0] == "reading"], [("reading", 0, 0), ("reading", 0, 4)])
        self.assertEqual(seen[0], ("reading", 0, 0))
        voices = [(done, total) for stage, done, total in seen if stage == "voices"]
        self.assertEqual(voices, [(1, 4), (2, 4), (3, 4), (4, 4)])
        self.assertEqual([s for s, _d, _t in seen if s not in ("reading", "voices")], [])

    def test_a_progress_callback_that_fails_does_not_end_the_pass(self):
        part, _ = _call_part(self.np, 40, self.YOU, self.THEM, self.SEGS)

        def broken(*a):
            raise RuntimeError("the page went away")
        out = msp.name_the_call([part], diarizer=FakeDiarizer(), progress=broken)
        self.assertTrue(out.ok)

    def test_the_progress_counts_all_the_parts_of_a_meeting_together(self):
        p1, _ = _call_part(self.np, 40, self.YOU, self.THEM, self.SEGS, key="audio_part1.wav")
        p2, _ = _call_part(self.np, 40, [], [(2, 30)], [(2, 30, "z", "them")], key="audio_part2.wav")
        seen = []
        msp.name_the_call([p1, p2], diarizer=FakeDiarizer(), progress=lambda *a: seen.append(a))
        voices = [(done, total) for stage, done, total in seen if stage == "voices"]
        self.assertEqual(voices[-1], (8, 8))                  # 4 + 4 pieces
        self.assertEqual([d for d, _t in voices], sorted(d for d, _t in voices))
        self.assertEqual({t for _d, t in voices}, {8})

    def test_voice_prints_made_before_a_stop_are_kept_and_not_made_again(self):
        part, _ = _call_part(self.np, 40, self.YOU, self.THEM, self.SEGS, key="audio_part1.wav")
        resume = {}
        d = FakeDiarizer()

        first = msp.name_the_call([part], diarizer=_StopsAfter(2), should_cancel=lambda: False, resume=resume)
        self.assertTrue(first.cancelled)
        self.assertEqual(len(resume[("audio_part1.wav", 4)]), 2)            # two finished, kept
        self.assertEqual(first.texts, [])
        second_d = FakeDiarizer()
        second = msp.name_the_call([part], diarizer=second_d, resume=resume)
        self.assertTrue(second.ok, (second.reason, second.notice))
        self.assertEqual(len(second_d.embed_calls[0]["spans"]), 2)           # only the rest
        self.assertEqual(second.text.split("\n"), ["You: a", "Speaker 1: b", "You: c", "Speaker 1: d"])
        # a third run makes no voice print at all - everything is there (the sound is read once, for the speech in it)
        loads = []
        real_loader = part._loader
        part._loader = lambda: loads.append(1) or real_loader()
        third_d = FakeDiarizer()
        third = msp.name_the_call([part], diarizer=third_d, resume=resume)
        self.assertTrue(third.ok, (third.reason, third.notice))
        self.assertEqual((loads, third_d.embed_calls), ([1], []))

    def test_voice_prints_that_do_not_belong_to_these_stretches_are_not_reused(self):
        part, _ = _call_part(self.np, 40, self.YOU, self.THEM, self.SEGS, key="audio_part1.wav")
        resume = {("audio_part1.wav", 99): [[1.0] * 12] * 99}               # another meeting's / another cut
        d = FakeDiarizer()
        out = msp.name_the_call([part], diarizer=d, resume=resume)
        self.assertTrue(out.ok)
        self.assertEqual(len(d.embed_calls[0]["spans"]), 4)

    def test_a_stop_by_the_speaker_tool_keeps_the_prints_it_gave_back(self):
        part, _ = _call_part(self.np, 40, self.YOU, self.THEM, self.SEGS, key="audio_part1.wav")
        d = FakeDiarizer()
        real = d.embed_spans

        def two_then_a_stall(audio, spans, **kw):
            vectors, _err = real(audio, spans[:2], **kw)
            return vectors, "stalled"
        d.embed_spans = two_then_a_stall
        resume = {}
        out = msp.name_the_call([part], diarizer=d, resume=resume)
        self.assertEqual(out.reason, msp.R_TIMEOUT)
        self.assertEqual(len(resume[("audio_part1.wav", 4)]), 2)

    def test_the_log_says_where_a_pass_was(self):
        part, _ = _call_part(self.np, 40, self.YOU, self.THEM, self.SEGS)
        with self.assertLogs("transcribe", level="INFO") as cm:
            msp.name_the_call([part], diarizer=FakeDiarizer())
        lines = [r.getMessage() for r in cm.records]
        started = [m for m in lines if m.startswith("Speaker pass started")]
        done = [m for m in lines if m.startswith("Speaker names done")]
        self.assertEqual(len(started), 1)
        self.assertIn("call=24s units=4", started[0])
        self.assertEqual(len(done), 1)
        for word in ("units=4", "call-speakers=1", "shares=100", "voices ", "cluster ", "total "):
            self.assertIn(word, done[0])
        self.assertFalse([m for m in lines if "Speaker 1: b" in m])           # numbers only: no transcript text


class _StopsAfter(FakeDiarizer):
    """A speaker model that is stopped (by a dictation, say) after it made ``n`` voice prints: it hands back the
    ones it had, with "cancelled"."""

    def __init__(self, n, **kw):
        FakeDiarizer.__init__(self, **kw)
        self.n = n

    def embed_spans(self, audio, spans, on_progress=None, should_cancel=None):
        vectors, _err = FakeDiarizer.embed_spans(self, audio, spans[:self.n])
        return vectors, "cancelled"


@unittest.skipUnless(_real_numpy(), "real numpy not importable (stubbed)")
class TestNameTheCallOldEngine(unittest.TestCase):
    """GUSAN_SPEAKER_ENGINE=windows: the sliding-window diarizer, for comparing the two engines in the field."""
    YOU = [(2, 6), (12, 15)]
    THEM = [(7, 11), (16, 36)]
    SEGS = [(2, 6, "a", "you"), (7, 11, "b", "them"), (12, 15, "c", "you"), (16, 36, "d", "them")]

    def setUp(self):
        import numpy as np
        self.np = np
        patcher = mock.patch.dict("os.environ", {"GUSAN_SPEAKER_ENGINE": "windows"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_one_voice_goes_through_the_diarizer_on_the_stretches_the_call_made_sound(self):
        part, _ = _call_part(self.np, 40, self.YOU, self.THEM, self.SEGS)
        d = FakeDiarizer()
        out = msp.name_the_call([part], diarizer=d)
        self.assertTrue(out.ok, (out.reason, out.notice))
        self.assertEqual(out.text.split("\n"), ["You: a", "Speaker 1: b", "You: c", "Speaker 1: d"])
        self.assertEqual(len(d.diarize_calls), 1)
        self.assertLess(d.diarize_calls[0], 40.0)
        self.assertEqual(d.embed_calls, [])                  # no voice prints

    def test_two_voices_and_a_voice_that_talks_for_moments(self):
        segs = [(2, 32, "b", "them"), (40, 70, "d", "them"), (80, 84, "y", "you")]
        part, _ = _call_part(self.np, 90, [(80, 84)], [(2, 32), (40, 70)], segs)
        out = msp.name_the_call([part], diarizer=TwoVoices())
        self.assertEqual(out.text.split("\n"), ["Speaker 1: b", "Speaker 2: d", "You: y"])
        them = [(7, 11), (16, 23), (28, 34), (40, 60)]
        segs = [(7, 11, "a", "them"), (16, 23, "b", "them"), (28, 34, "c", "them"), (40, 60, "d", "them")]
        part, _ = _call_part(self.np, 64, [], them, segs)
        out = msp.name_the_call([part], diarizer=FakeDiarizer(split_noise=True))
        self.assertEqual(out.text, "Speaker 1: a b c d")

    def test_no_turns_a_timeout_and_a_cancel_end_the_way_they_did(self):
        part, _ = _call_part(self.np, 40, self.YOU, self.THEM, self.SEGS)
        d = FakeDiarizer()
        d.diarize = lambda audio, **kw: []
        out = msp.name_the_call([part], diarizer=d)
        self.assertEqual((out.reason, out.notice), (msp.R_NO_VOICES, msp.CALL_TOGETHER))
        out = msp.name_the_call([part], diarizer=FakeDiarizer(fail="timed out"))
        self.assertEqual((out.reason, out.notice), (msp.R_TIMEOUT, msp.CALL_TOO_LONG))
        for why in ("cancelled", "app quitting"):
            self.assertTrue(msp.name_the_call([part], diarizer=FakeDiarizer(fail=why)).cancelled)

    def test_the_default_is_the_voice_engine(self):
        self.assertEqual(msp.voice_prints.engine({}), "voices")


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
        self.assertEqual(d.embed_calls, [])
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
        d.embed_spans = lambda audio, spans, **kw: ([None] * len(spans), "")
        res = msp.refine_meeting(self.folder, diarizer=d)
        self.assertEqual((res.state, res.reason), ("concluded", msp.R_NO_VOICES))
        self.assertEqual(self._read(), self.OLD)
        self.assertEqual(ms.load_meta(self.folder)["notices"], [msp.CALL_TOGETHER])

    def test_a_transcript_someone_else_rewrote_is_never_replaced(self):
        self._meeting()
        (self.folder / "transcript.txt").write_text("Speaker 1: someone's own rewrite", encoding="utf-8")
        d = FakeDiarizer()
        self.assertEqual(msp.refine_meeting(self.folder, diarizer=d).state, "nothing")
        self.assertEqual(d.embed_calls, [])
        self.assertEqual(self._read(), "Speaker 1: someone's own rewrite")

        def hook(_d):                                          # ...or while the pass works
            (self.folder / "transcript.txt").write_text("You: a\nSpeaker 1: b\nYou: c\nSpeaker 2: d",
                                                        encoding="utf-8")
        self._meeting()
        res = msp.refine_meeting(self.folder, diarizer=Probe(hook))
        # Nothing was replaced, so nothing is claimed: no "Found 2 speakers", the meeting is not marked as done
        # (it would never offer Identify speakers again), the backup is gone, the person's rewrite is untouched.
        self.assertEqual(res.state, "nothing")
        self.assertIsNone(msp.result_line(res))
        self.assertEqual(self._read(), "You: a\nSpeaker 1: b\nYou: c\nSpeaker 2: d")
        self.assertNotIn("speakers_at", ms.load_meta(self.folder))
        self.assertFalse(ms.load_segments(self.folder)["parts"][0]["done"])
        self.assertFalse((self.folder / ms.BEFORE_SPEAKERS).exists())

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
                    self.assertEqual(d.embed_calls, [])
                    self.assertEqual(self._read(), named)
                else:
                    self.assertEqual(msp.refine_meeting(self.folder, diarizer=FakeDiarizer()).state, "done")
                    self.assertEqual(self._read(), named)

    def test_a_pass_that_raises_inside_is_a_failed_result_not_an_exception(self):
        self._meeting()
        d = FakeDiarizer()
        d.embed_spans = mock.Mock(side_effect=RuntimeError("model crashed"))
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
        self.assertEqual(d.embed_calls, [])
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

            def embed_spans(inner, audio, spans, on_progress=None, should_cancel=None):
                shutil.rmtree(self.folder)                      # the meeting is deleted from the page
                asked["cancel"] = bool(should_cancel and should_cancel())
                inner.last_error = "cancelled" if asked["cancel"] else ""
                return [], inner.last_error
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

    # ── how it ends, what it says, where it was ──
    def test_a_finished_pass_records_how_many_people_it_found(self):
        self._meeting()
        res = msp.refine_meeting(self.folder, diarizer=FakeDiarizer())
        self.assertEqual((res.state, res.speakers), ("done", 1))
        meta = ms.load_meta(self.folder)
        self.assertEqual(meta["speakers_found"], 1)
        self.assertIn("speakers_at", meta)
        self.setUp()
        self._meeting(you=[(80, 84)], them=[(2, 32), (40, 70)], seconds=90,
                      segments=[(2, 32, "b", "them"), (40, 70, "d", "them"), (80, 84, "y", "you")],
                      text="Them: b\nThem: d\nYou: y")
        res = msp.refine_meeting(self.folder, diarizer=TwoVoices())
        self.assertEqual((res.state, res.speakers), ("done", 2))
        self.assertEqual(ms.load_meta(self.folder)["speakers_found"], 2)
        self.assertEqual(self._read(), "Speaker 1: b\nSpeaker 2: d\nYou: y")

    def test_a_pass_that_failed_or_ran_out_of_time_records_no_count(self):
        self._meeting()
        res = msp.refine_meeting(self.folder, diarizer=FakeDiarizer(fail="timed out"))
        self.assertEqual((res.state, res.reason), ("concluded", msp.R_TIMEOUT))
        meta = ms.load_meta(self.folder)
        self.assertNotIn("speakers_found", meta)
        self.assertNotIn("speakers_at", meta)                  # not concluded: the page offers Identify speakers
        self.assertEqual(meta["notices"], [msp.CALL_TOO_LONG])
        res = msp.refine_meeting(self.folder, diarizer=FakeDiarizer(fail="worker exited with code 3"))
        self.assertEqual((res.state, res.reason), ("failed", msp.R_ERROR))
        self.assertNotIn("speakers_found", ms.load_meta(self.folder))

    def test_a_call_nobody_could_be_told_apart_in_records_a_count_of_zero(self):
        self._meeting()
        d = FakeDiarizer()
        d.embed_spans = lambda audio, spans, **kw: ([None] * len(spans), "")
        msp.refine_meeting(self.folder, diarizer=d)
        meta = ms.load_meta(self.folder)
        self.assertEqual(meta["speakers_found"], 0)
        self.assertIn("speakers_at", meta)

    def test_progress_and_the_voice_prints_pass_through_to_the_work(self):
        self._meeting()
        seen, resume = [], {}
        res = msp.refine_meeting(self.folder, diarizer=FakeDiarizer(), progress=lambda *a: seen.append(a),
                                 resume=resume)
        self.assertEqual(res.state, "done")
        self.assertEqual(seen[0], ("reading", 0, 0))
        self.assertEqual(seen[-1], ("voices", 4, 4))
        self.assertEqual(list(resume), [("audio_part1.wav", 4)])        # keyed by the recording's own name

    def test_a_pass_that_was_stopped_goes_on_from_the_voice_prints_it_had(self):
        self._meeting()
        resume = {}
        stopped = msp.refine_meeting(self.folder, diarizer=_StopsAfter(3), resume=resume)
        self.assertEqual(stopped.state, "cancelled")
        self.assertEqual(self._read(), self.OLD)                       # nothing changed ...
        self.assertFalse((self.folder / ms.BEFORE_SPEAKERS).exists())  # ... and no backup lies about
        self.assertNotIn("speakers_at", ms.load_meta(self.folder))
        self.assertEqual(len(resume[("audio_part1.wav", 4)]), 3)
        d = FakeDiarizer()
        res = msp.refine_meeting(self.folder, diarizer=d, resume=resume)
        self.assertEqual(res.state, "done")
        self.assertEqual(len(d.embed_calls[0]["spans"]), 1)             # one print left to make
        self.assertEqual(self._read(), "You: a\nSpeaker 1: b\nYou: c\nSpeaker 1: d")

    def test_refinable_lists_the_parts_a_pass_can_still_do(self):
        self.assertEqual(msp.refinable(self.folder / "missing"), [])
        self.assertEqual(msp.refinable(self.folder), [])                # no segments.json yet
        self._meeting()
        parts = msp.refinable(self.folder)
        self.assertEqual([p["audio"] for p in parts], ["audio_part1.wav"])
        # the transcript was rewritten by someone: the block is not there any more
        (self.folder / "transcript.txt").write_text("Speaker 1: someone's own rewrite", encoding="utf-8")
        self.assertEqual(msp.refinable(self.folder), [])
        self.setUp()
        # a part that is done is not offered again
        self._meeting()
        msp.refine_meeting(self.folder, diarizer=FakeDiarizer())
        self.assertEqual(msp.refinable(self.folder), [])
        # the call's sound is gone
        self.setUp()
        self._meeting(tracks=False)
        self.assertEqual(msp.refinable(self.folder), [])
        # damaged segments.json
        (self.folder / "segments.json").write_text("{not json", encoding="utf-8")
        self.assertEqual(msp.refinable(self.folder), [])

    def test_refinable_agrees_with_what_refine_meeting_would_do(self):
        self._meeting()
        self.assertTrue(msp.refinable(self.folder))
        self.assertNotEqual(msp.refine_meeting(self.folder, diarizer=FakeDiarizer()).state, "nothing")
        self.assertFalse(msp.refinable(self.folder))
        self.assertEqual(msp.refine_meeting(self.folder, diarizer=FakeDiarizer()).state, "nothing")


class TestResultLine(unittest.TestCase):
    """The one calm line when a pass ends: fixed words and whole numbers only."""

    def R(self, state, reason="", speakers=0):
        return msp.PassResult(state, reason, "", speakers)

    def test_found_people(self):
        self.assertEqual(msp.result_line(self.R("done", "", 2), "done"), ("Found 2 speakers on the call.", "ok"))
        self.assertEqual(msp.result_line(self.R("done", "", 5), "done"), ("Found 5 speakers on the call.", "ok"))
        self.assertEqual(msp.result_line(self.R("done", "", 1), "done"), ("Only one person spoke on the call.", "ok"))

    def test_a_short_call_and_an_empty_one(self):
        self.assertEqual(msp.result_line(self.R("concluded", msp.R_BRIEF), "done"),
                         ("The other side spoke only briefly, so it stays Them.", "ok"))
        self.assertEqual(msp.result_line(self.R("concluded", msp.R_NO_CALL), "done"),
                         ("Nobody on the call spoke.", "ok"))

    def test_failures_are_amber(self):
        for result in (self.R("failed", msp.R_ERROR), self.R("failed", msp.R_PRIVACY),
                       self.R("concluded", msp.R_NO_VOICES), self.R("concluded", msp.R_TIMEOUT)):
            self.assertEqual(msp.result_line(result, "done"), ("Couldn't tell the people on the call apart.", "amber"))

    def test_stopped_and_interrupted(self):
        self.assertEqual(msp.result_line(self.R("cancelled", msp.R_CANCELLED), "cancelled"), ("Stopped.", "ok"))
        self.assertEqual(msp.result_line(None, "cancelled"), ("Stopped.", "ok"))       # dropped while it waited
        self.assertEqual(msp.result_line(self.R("cancelled", msp.R_CANCELLED), "gave_up"),
                         ("Other work kept interrupting it. You can try again.", "amber"))
        self.assertEqual(msp.result_line(self.R("cancelled", msp.R_CANCELLED), "done"), ("Stopped.", "ok"))

    def test_nothing_to_say(self):
        self.assertIsNone(msp.result_line(None))
        self.assertIsNone(msp.result_line(None, "done"))
        self.assertIsNone(msp.result_line(self.R("nothing"), "done"))
        self.assertIsNone(msp.result_line(self.R("concluded", "", 0), "done"))      # nothing changed, nothing to report

    def test_no_line_has_markup_or_markdown_in_it(self):
        lines = [msp.LINE_ONE, msp.LINE_BRIEF, msp.LINE_NOBODY, msp.LINE_FAILED, msp.LINE_STOPPED,
                 msp.LINE_INTERRUPTED, msp.result_line(self.R("done", "", 3), "done")[0]]
        for text in lines:
            self.assertFalse(any(c in text for c in "<>*_`#[]{}"), text)
            self.assertNotIn("limit", text.lower())
            self.assertNotIn("Armen", text)
            self.assertTrue(text.endswith("."), text)


if __name__ == "__main__":
    unittest.main()
