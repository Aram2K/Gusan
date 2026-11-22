"""Speaker ids and names of a saved meeting (speaker_map.py, Gusan 2.1.11): the stored model, the migration of old
transcripts, a rename written in one step, the background pass that never overwrites a name the person set (and still
finds its block after a rename), named labels recognised wherever the transcript is read."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import meeting_chat
import meeting_speakers as msp
import meeting_store as ms
import meeting_translation
import rich_text
import speaker_map as sm


class _Folder(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.folder = Path(self._tmp.name) / "20261121_100000"
        self.folder.mkdir()

    def write(self, transcript, **meta):
        ms.write_transcript(self.folder, transcript)
        ms.update_meta(self.folder, title="Call", **meta)

    def read(self):
        return (self.folder / "transcript.txt").read_text(encoding="utf-8")


class TestIds(unittest.TestCase):
    def test_canonical_labels_and_their_ids(self):
        for sid, label in (("you", "You"), ("them", "Them"), ("s1", "Speaker 1"), ("s12", "Speaker 12"),
                           ("s?", "Speaker ?")):
            self.assertEqual(sm.canonical(sid), label)
            self.assertEqual(sm.id_of_canonical(label), sid)
        self.assertEqual(sm.id_of_canonical("Speaker  3"), "s3")
        for name in ("Anna", "Speaker", "Speaker 0", "you", ""):
            self.assertEqual(sm.id_of_canonical(name), "", name)
        self.assertEqual(sm.canonical("anna"), "")

    def test_a_name_is_cleaned_to_what_a_label_can_carry(self):
        self.assertEqual(sm.clean_name("  Anna:\n  Lee "), "Anna Lee")
        self.assertEqual(sm.clean_name("— Resumed —"), "Resumed")
        self.assertEqual(sm.clean_name("x" * 80), "x" * sm.MAX_NAME)
        self.assertEqual(sm.clean_name("   "), "")

    def test_only_clean_names_of_real_ids_are_read_from_meta(self):
        meta = {sm.NAMES_KEY: {"s1": "Anna", "you": "  ", "bogus": "Bob", "s2": 5, "them": "Lee"},
                sm.BY_KEY: {"s1": "screen", "them": "nonsense"}}
        self.assertEqual(sm.stored(meta), {"s1": ("Anna", "screen"), "them": ("Lee", "user")})
        self.assertEqual(sm.stored(None), {})


class TestReading(unittest.TestCase):
    TEXT = "You: hi there\nThem: hello you\nYou: ok\n\n— Resumed —\n\nSpeaker 2: yes"

    def test_ids_order_and_shares(self):
        m = sm.from_text(self.TEXT)
        self.assertEqual(m.order, ["you", "them", "s2"])
        self.assertEqual(m.words, {"you": 3, "them": 2, "s2": 1})
        speakers = m.speakers()
        self.assertEqual([s.sid for s in speakers], ["you", "them", "s2"])
        self.assertAlmostEqual(sum(s.share for s in speakers), 1.0)
        self.assertEqual(speakers[0].label, "You")

    def test_you_comes_first_whoever_spoke_first(self):
        m = sm.from_text("Speaker 1: a b\nYou: c")
        self.assertEqual([s.sid for s in m.speakers()], ["you", "s1"])

    def test_a_stored_name_is_a_label_whatever_it_looks_like(self):
        meta = {sm.NAMES_KEY: {"s1": "anna-maria lópez jr.", "s2": "Աննա"},
                sm.BY_KEY: {"s1": "user", "s2": "user"}}
        m = sm.from_text("You: hi\nanna-maria lópez jr.: hello\nԱննա: բարեվ", meta)
        self.assertEqual(m.order, ["you", "s1", "s2"])
        self.assertEqual(m.label("s1"), "anna-maria lópez jr.")
        self.assertEqual(m.by("s1"), "user")

    def test_an_older_transcripts_name_becomes_an_automatic_name_of_a_new_id(self):
        m = sm.from_text("You: a\nSpeaker 1: b\nAnna Petrosyan: c d\nSpeaker 1: e")
        self.assertEqual(m.order, ["you", "s1", "s2"])
        self.assertEqual(m.derived, {"s2": "Anna Petrosyan"})
        self.assertEqual((m.name("s2"), m.by("s2")), ("Anna Petrosyan", "auto"))

    def test_a_call_only_meeting_named_by_an_older_pass_is_read_once_the_pass_says_so(self):
        text = "Anna Petrosyan: hello\nAnna Petrosyan: bye"
        self.assertEqual(sm.from_text(text).order, [])                         # nothing says it has labels
        m = sm.from_text(text, {"speakers_at": "2026-11-20 10:00:00"})
        self.assertEqual(m.order, ["s1"])
        self.assertEqual(m.derived, {"s1": "Anna Petrosyan"})

    def test_a_plain_transcript_has_no_speakers(self):
        for text in ("So here's the thing: we ship.\nNote: later", "", "hello world"):
            self.assertEqual(sm.from_text(text).speakers(), [], text)

    def test_markers_and_blank_lines_are_nobody(self):
        m = sm.from_text(self.TEXT)
        self.assertIsNone(m.split("— Resumed —"))
        self.assertIsNone(m.split(""))

    def test_the_page_writes_you_as_the_first_name(self):
        m = sm.from_text(self.TEXT)
        self.assertEqual(m.shown("Aram Adamyan")["You"], "Aram (You)")
        self.assertEqual(m.shown("")["You"], "You")
        renamed = sm.from_text("Bob: hi", {sm.NAMES_KEY: {"you": "Bob"}})
        self.assertEqual(renamed.shown("Aram"), {"Bob": "Bob"})                # renamed: no "(You)"


class TestRename(_Folder):
    TEXT = "You: Hello team.\nSpeaker 1: Welcome.\n\n— Resumed 2026-11-21 10:30 —\n\nSpeaker 1: Bye.\nYou: Thanks."

    def test_every_line_of_the_speaker_gets_the_name_in_one_write(self):
        self.write(self.TEXT)
        writes = []
        real = ms.write_transcript
        with patch.object(sm.meeting_store, "write_transcript", side_effect=lambda f, t: writes.append(t) or real(f, t)):
            res = sm.rename(self.folder, "s1", "anna lee")
        self.assertTrue(res.ok, res.message)
        self.assertEqual(len(writes), 1)                                       # one atomic write
        expected = self.TEXT.replace("Speaker 1:", "anna lee:")
        self.assertEqual(self.read(), expected)
        self.assertEqual(res.text, expected)
        meta = ms.load_meta(self.folder)
        self.assertEqual(meta[sm.NAMES_KEY], {"s1": "anna lee"})
        self.assertEqual(meta[sm.BY_KEY], {"s1": "user"})
        self.assertTrue(meta[sm.CHANGED_KEY])
        self.assertEqual(meta["title"], "Call")                                # nothing else of meta changes
        self.assertEqual([p.name for p in self.folder.iterdir() if p.name.endswith(".tmp")], [])
        # read again: the name is the speaker
        m = sm.load(self.folder)
        self.assertEqual(m.order, ["you", "s1"])
        self.assertEqual(m.label("s1"), "anna lee")

    def test_an_empty_name_is_speaker_n_again(self):
        self.write(self.TEXT)
        sm.rename(self.folder, "s1", "Anna")
        res = sm.rename(self.folder, "s1", "   ")
        self.assertTrue(res.ok)
        self.assertEqual(self.read(), self.TEXT)
        self.assertNotIn(sm.NAMES_KEY, ms.load_meta(self.folder))
        self.assertEqual(res.label, "Speaker 1")

    def test_you_and_them_can_be_renamed_and_back(self):
        self.write("You: a\nThem: b")
        self.assertTrue(sm.rename(self.folder, "them", "Anna").ok)
        self.assertTrue(sm.rename(self.folder, "you", "Aram").ok)
        self.assertEqual(self.read(), "Aram: a\nAnna: b")
        self.assertTrue(sm.rename(self.folder, "them", "").ok)
        self.assertEqual(self.read(), "Aram: a\nThem: b")

    def test_a_name_another_speaker_has_or_is_is_refused(self):
        self.write("You: a\nSpeaker 1: b\nSpeaker 2: c")
        self.assertTrue(sm.rename(self.folder, "s1", "Anna").ok)
        for sid, name, code in (("s2", "anna", sm.R_TAKEN), ("s2", "Speaker 1", sm.R_RESERVED),
                                ("s2", "You", sm.R_RESERVED), ("s1", "Them", sm.R_RESERVED)):
            res = sm.rename(self.folder, sid, name)
            self.assertFalse(res.ok, name)
            self.assertEqual(res.code, code, name)
            self.assertTrue(res.message)
        self.assertEqual(self.read(), "You: a\nAnna: b\nSpeaker 2: c")         # nothing changed
        self.assertTrue(sm.rename(self.folder, "s2", "Speaker 2").ok)          # its own label: no name

    def test_an_unknown_speaker_or_no_transcript_is_refused(self):
        self.assertEqual(sm.rename(self.folder, "s1", "Anna").code, sm.R_NO_TRANSCRIPT)
        self.write("You: a")
        self.assertEqual(sm.rename(self.folder, "s4", "Anna").code, sm.R_UNKNOWN)

    def test_a_failed_write_changes_nothing(self):
        self.write("You: a\nSpeaker 1: b")
        with patch.object(sm.meeting_store, "write_transcript", return_value=False):
            res = sm.rename(self.folder, "s1", "Anna")
        self.assertEqual(res.code, sm.R_WRITE)
        self.assertEqual(self.read(), "You: a\nSpeaker 1: b")
        self.assertNotIn(sm.NAMES_KEY, ms.load_meta(self.folder))

    def test_the_first_rename_of_an_old_meeting_stores_its_names(self):
        self.write("You: a\nAnna Petrosyan: b\nYou: c", speakers_at="2026-11-01 10:00:00")
        self.assertTrue(sm.rename(self.folder, "you", "Aram").ok)
        meta = ms.load_meta(self.folder)
        self.assertEqual(meta[sm.NAMES_KEY], {"s1": "Anna Petrosyan", "you": "Aram"})
        self.assertEqual(meta[sm.BY_KEY], {"s1": "auto", "you": "user"})
        self.assertEqual(sm.load(self.folder).by("s1"), "auto")


class TestDecide(unittest.TestCase):
    def meta(self, **names):
        return {sm.NAMES_KEY: {k: v[0] for k, v in names.items()}, sm.BY_KEY: {k: v[1] for k, v in names.items()}}

    def test_a_name_the_person_set_is_never_overwritten(self):
        meta = self.meta(s1=("Bob", "user"))
        self.assertEqual(sm.decide(meta, ["you", "s1"], {"s1": "Anna Petrosyan"}), {"s1": ("Bob", "user")})

    def test_the_steps_own_name_is_kept_as_automatic(self):
        self.assertEqual(sm.decide({}, ["you", "s1"], {"s1": "Anna"}), {"s1": ("Anna", "screen")})

    def test_an_old_automatic_name_goes_when_the_step_names_nobody(self):
        meta = self.meta(s1=("Anna", "screen"), s5=("Zed", "auto"))
        self.assertEqual(sm.decide(meta, ["you", "s1", "s2"], {}), {"s5": ("Zed", "auto")})

    def test_them_named_by_the_person_is_the_one_voice_found(self):
        meta = self.meta(them=("Anna", "user"))
        self.assertEqual(sm.decide(meta, ["you", "s1"], {}, one_voice=True), {"s1": ("Anna", "user")})
        # two voices: nobody is named on a guess (the name stays on "Them", which is no longer in the transcript)
        self.assertEqual(sm.decide(meta, ["you", "s1", "s2"], {}, one_voice=False), {"them": ("Anna", "user")})

    def test_two_speakers_never_share_a_name(self):
        meta = self.meta(s2=("Anna", "user"))
        self.assertEqual(sm.decide(meta, ["s1", "s2"], {"s1": "anna"}), {"s2": ("Anna", "user")})


class TestSettle(_Folder):
    def test_a_fresh_block_gets_the_meetings_names_and_the_names_are_kept(self):
        ms.update_meta(self.folder, **{sm.NAMES_KEY: {"you": "Aram"}, sm.BY_KEY: {"you": "user"}})
        texts = sm.settle(self.folder, ["You: a\nSpeaker 1: b\nYou: c"], {"s1": "Anna P"})
        self.assertEqual(texts, ["Aram: a\nAnna P: b\nAram: c"])
        self.assertEqual(sm.stored(ms.load_meta(self.folder)), {"you": ("Aram", "user"), "s1": ("Anna P", "screen")})

    def test_without_a_folder_only_the_steps_names_apply(self):
        self.assertEqual(sm.settle(None, ["You: a\nSpeaker 1: b"], {}), ["You: a\nSpeaker 1: b"])


def _real_numpy():
    from tests.test_meeting_speakers import _real_numpy as real
    return real()


@unittest.skipUnless(_real_numpy(), "real numpy not importable (stubbed)")
class TestThePassAndTheNames(_Folder):
    """The background pass (meeting_speakers.refine_meeting) finds its block by the ids - also after the person renamed
    "You" or "Them" while it waited - and never overwrites a name the person set."""
    OLD = "You: a\nThem: b\nYou: c\nThem: d"
    SEGS = [(2, 6, "a", "you"), (7, 11, "b", "them"), (12, 15, "c", "you"), (16, 36, "d", "them")]

    def meeting(self, them=((7, 11), (16, 36)), segs=None, **meta):
        import numpy as np
        import audio_export
        from tests.test_meeting_speakers import call_tracks
        mic, system = call_tracks(np, 40, [(2, 6), (12, 15)], list(them))
        path = self.folder / "audio_part1.wav"
        audio_export.write_wav(path, ((mic.astype(np.float32) + system.astype(np.float32)) / 32767 * 0.75))
        audio_export.write_wav_int16(ms.track_path(path, "mic"), mic)
        audio_export.write_wav_int16(ms.track_path(path, "system"), system)
        segs = segs or self.SEGS
        entry = {"audio": "audio_part1.wav", "kind": "sources", "seconds": 40.0, "done": False, "text": self.OLD,
                 "canon": self.OLD,
                 "segments": [{"s": a, "e": b, "t": t, "src": src, "spk": src} for a, b, t, src in segs]}
        self.assertTrue(ms.write_segments(self.folder, {"version": 1, "parts": [entry]}))
        self.write(self.OLD, notices=[], **meta)

    def diarizer(self, **kw):
        from tests.test_meeting_speakers import FakeDiarizer
        return FakeDiarizer(**kw)

    def test_a_rename_while_the_pass_waits_does_not_break_it(self):
        self.meeting()
        self.assertTrue(sm.rename(self.folder, "you", "Aram").ok)
        self.assertTrue(sm.rename(self.folder, "them", "Anna").ok)
        self.assertEqual(self.read(), "Aram: a\nAnna: b\nAram: c\nAnna: d")
        self.assertTrue(msp.refinable(self.folder))                           # still to do, found by its ids
        res = msp.refine_meeting(self.folder, diarizer=self.diarizer())
        self.assertEqual((res.state, res.speakers), ("done", 1))
        # one voice on the call: the name the person gave "Them" is that voice's
        self.assertEqual(self.read(), "Aram: a\nAnna: b\nAram: c\nAnna: d")
        self.assertEqual(sm.stored(ms.load_meta(self.folder)), {"you": ("Aram", "user"), "s1": ("Anna", "user")})
        part = ms.load_segments(self.folder)["parts"][0]
        self.assertTrue(part["done"])
        self.assertEqual(part["canon"], "You: a\nSpeaker 1: b\nYou: c\nSpeaker 1: d")
        self.assertEqual([s["spk"] for s in part["segments"]], ["you", "s1", "you", "s1"])

    def test_two_voices_after_them_was_renamed_are_speaker_1_and_2(self):
        import numpy as np
        import audio_export
        from tests.test_meeting_speakers import TwoVoices, call_tracks
        segs = [(2, 32, "b", "them"), (40, 70, "d", "them"), (80, 84, "y", "you")]
        old = "Them: b\nThem: d\nYou: y"        # (an older meeting: its part has no "canon", its "text" is the block)
        mic, system = call_tracks(np, 90, [(80, 84)], [(2, 32), (40, 70)])
        path = self.folder / "audio_part1.wav"
        audio_export.write_wav(path, ((mic.astype(np.float32) + system.astype(np.float32)) / 32767 * 0.75))
        audio_export.write_wav_int16(ms.track_path(path, "mic"), mic)
        audio_export.write_wav_int16(ms.track_path(path, "system"), system)
        entry = {"audio": "audio_part1.wav", "kind": "sources", "seconds": 90.0, "done": False, "text": old,
                 "segments": [{"s": a, "e": b, "t": t, "src": src} for a, b, t, src in segs]}
        ms.write_segments(self.folder, {"version": 1, "parts": [entry]})
        self.write(old, notices=[])
        self.assertTrue(sm.rename(self.folder, "them", "Anna").ok)
        res = msp.refine_meeting(self.folder, diarizer=TwoVoices())
        self.assertEqual((res.state, res.speakers), ("done", 2))
        self.assertEqual(self.read(), "Speaker 1: b\nSpeaker 2: d\nYou: y")      # two voices: never named on a guess

    def test_the_one_name_from_the_screen_never_replaces_the_persons_name(self):
        self.meeting()
        ms.write_transcript(self.folder, self.OLD)
        ms.update_meta(self.folder, **{sm.NAMES_KEY: {"s1": "Bob"}, sm.BY_KEY: {"s1": "user"}})
        res = msp.refine_meeting(self.folder, diarizer=self.diarizer(), call_name="Anna Petrosyan")
        self.assertEqual(res.state, "done")
        self.assertEqual(self.read(), "You: a\nBob: b\nYou: c\nBob: d")
        self.assertEqual(sm.stored(ms.load_meta(self.folder)), {"s1": ("Bob", "user")})

    def test_the_one_name_from_the_screen_is_kept_as_automatic_and_editable(self):
        self.meeting()
        res = msp.refine_meeting(self.folder, diarizer=self.diarizer(), call_name="Anna Petrosyan")
        self.assertEqual(res.state, "done")
        self.assertEqual(self.read(), "You: a\nAnna Petrosyan: b\nYou: c\nAnna Petrosyan: d")
        m = sm.load(self.folder)
        self.assertEqual((m.label("s1"), m.by("s1")), ("Anna Petrosyan", "screen"))
        self.assertTrue(sm.rename(self.folder, "s1", "Anna").ok)
        self.assertEqual(self.read(), "You: a\nAnna: b\nYou: c\nAnna: d")

    def test_a_rename_while_the_voices_are_worked_out_is_kept_and_the_pass_still_lands(self):
        import threading
        self.meeting()
        done = {}

        def hook(_d):
            # the person renames "You" while the voices are worked out (another thread, as the page does)
            t = threading.Thread(target=lambda: done.setdefault("r", sm.rename(self.folder, "you", "Aram")))
            t.start()
            t.join(5)
        from tests.test_meeting_speakers import Probe
        res = msp.refine_meeting(self.folder, diarizer=Probe(hook))
        self.assertTrue(done["r"].ok)
        self.assertEqual(res.state, "done")
        self.assertEqual(self.read(), "Aram: a\nSpeaker 1: b\nAram: c\nSpeaker 1: d")


class TestNamedLabelsAreRead(_Folder):
    def test_rich_text_and_the_transcript_rows_take_the_meetings_labels(self):
        labels = ["anna lee", "You"]
        self.assertEqual(rich_text.split_speaker("anna lee: hi there", labels), ("anna lee", "hi there"))
        self.assertEqual(rich_text.split_speaker("anna lee: hi there"), ("", "anna lee: hi there"))   # (the pattern alone)
        rows = meeting_translation.rows("You: a\nanna lee: " + "Long sentence here. " * 60, labels)
        self.assertEqual(rows[0], ("You", "a", False))
        self.assertEqual(rows[1][0], "anna lee")
        self.assertTrue(rows[1][1].startswith("Long sentence"))
        self.assertTrue(all(not r[0] for r in rows[2:]))

    def test_the_chat_reads_a_renamed_speaker_as_a_label_and_keeps_its_times(self):
        text = "You: Hello team.\nSpeaker 1: Welcome."
        entry = {"audio": "audio_part1.wav", "kind": "sources", "seconds": 40.0, "done": True, "text": text,
                 "segments": [{"s": 2.0, "e": 6.0, "t": "Hello team.", "src": "you"},
                              {"s": 7.0, "e": 11.0, "t": "Welcome.", "src": "them"}]}
        ms.write_segments(self.folder, {"version": 1, "parts": [entry]})
        self.write(text)
        sm.rename(self.folder, "s1", "dr. anna")
        m = meeting_chat.load_material(self.folder)
        self.assertTrue(m.labelled)
        self.assertIn("dr. anna", m.labels)
        marked, ok = m.marked()
        self.assertTrue(ok)
        self.assertEqual(marked, "You: [00:02] Hello team.\ndr. anna: Welcome.")

    def test_a_rename_and_the_end_of_a_pass_take_turns(self):
        import threading
        self.write("You: a\nSpeaker 1: b")
        done = threading.Event()
        with sm.LOCK:                                   # (the pass is replacing its block)
            t = threading.Thread(target=lambda: (sm.rename(self.folder, "s1", "Anna"), done.set()))
            t.start()
            self.assertFalse(done.wait(0.3))             # the rename waits for it
        self.assertTrue(done.wait(5))
        self.assertEqual(self.read(), "You: a\nAnna: b")

    def test_the_translation_keys_follow_the_labels(self):
        self.write("You: Bonjour.\nSpeaker 1: Salut.")
        sm.rename(self.folder, "s1", "anna")
        seen = []
        meeting_translation.translate_saved(self.folder, "en", lambda t, s, l, c: seen.append(t) or "x", workers=1)
        self.assertEqual(sorted(seen), ["Bonjour.", "Salut."])                  # the words, never "anna: Salut."


# ── the session ──────────────────────────────────────────────────────────────
from tests.test_meeting_session import _App, _Base, _SegRec, _interruptible  # noqa: E402
import meeting_session as msess  # noqa: E402


class TestTheSessionRenames(_Base):
    def folder_with(self, text, **meta):
        folder = self.root / "20261121_100000"
        folder.mkdir(exist_ok=True)
        ms.write_transcript(folder, text)
        ms.update_meta(folder, title="Call", **meta)
        return folder

    def test_the_meeting_being_written_is_not_renamed(self):
        s = self.session()
        folder = self.folder_with("You: a\nSpeaker 1: b")
        s._meeting_dir = folder
        s.state = msess.STATE_PROCESSING
        res = s.rename_speaker(folder, "s1", "Anna")
        self.assertEqual(res.code, sm.R_BUSY)
        self.assertEqual(ms.load_transcript(folder), "You: a\nSpeaker 1: b")
        other = self.root / "20261120_090000"
        other.mkdir()
        ms.write_transcript(other, "You: x\nSpeaker 1: y")
        self.assertTrue(s.rename_speaker(other, "s1", "Bob").ok)             # another meeting: fine meanwhile

    def test_the_meeting_the_session_holds_takes_the_new_transcript(self):
        s = self.session()
        folder = self.folder_with("You: a\nSpeaker 1: b")
        s._meeting_dir = folder
        s.state = msess.STATE_DONE
        s._final_transcript = "You: a\nSpeaker 1: b"
        self.assertTrue(s.rename_speaker(folder, "s1", "Anna").ok)
        self.assertEqual(s._final_transcript, "You: a\nAnna: b")            # Rewrite notes / Retry read this

    def test_new_notes_take_the_offer_away_and_are_written_from_the_names(self):
        s = self.session()
        folder = self.folder_with("You: a\nSpeaker 1: b", speaker_names_changed=True)
        (folder / "notes.md").write_text("## Notes\n- old", encoding="utf-8")
        seen = []
        with patch.object(msess.actions, "process", side_effect=lambda text, *a, **k: seen.append(text) or "## Notes\n- new"):
            self.assertTrue(sm.rename(folder, "s1", "Anna").ok)
            self.assertTrue(s.retry_notes(None, folder=folder))
        self.assertIn("Anna: b", seen[0])
        meta = ms.load_meta(folder)
        self.assertNotIn(sm.CHANGED_KEY, meta)
        self.assertEqual((folder / "notes.md").read_text(encoding="utf-8"), "## Notes\n- new")

    def test_failed_notes_keep_the_old_ones_and_the_offer(self):
        s = self.session()
        folder = self.folder_with("You: a\nSpeaker 1: b")
        (folder / "notes.md").write_text("## Notes\n- old", encoding="utf-8")
        self.assertTrue(sm.rename(folder, "s1", "Anna").ok)
        with patch.object(msess.actions, "process", side_effect=RuntimeError("offline")):
            self.assertTrue(s.retry_notes(None, folder=folder))
        self.assertEqual((folder / "notes.md").read_text(encoding="utf-8"), "## Notes\n- old")
        self.assertTrue(ms.load_meta(folder).get(sm.CHANGED_KEY))

    def test_a_speaker_steps_texts_get_the_names_the_person_set(self):
        s = self.session()
        folder = self.folder_with("x", **{sm.NAMES_KEY: {"s2": "Bob"}, sm.BY_KEY: {"s2": "user"}})
        out = msp.Outcome()
        out.mode, out.speakers = "voices", 2
        out.canonical = ["Speaker 1: a\nSpeaker 2: b"]
        out.texts = list(out.canonical)
        self.assertEqual(s._named_texts(out, folder), ["Speaker 1: a\nBob: b"])
        bare = msp.Outcome()
        bare.texts = ["You: hi"]                                        # (a step without canonical texts: as it is)
        self.assertEqual(s._named_texts(bare, folder), ["You: hi"])


@unittest.skipUnless(_real_numpy(), "real numpy not importable (stubbed)")
class TestTheStopKeepsIds(_Base):
    def test_the_segments_carry_their_speaker_ids_before_and_after_the_pass(self):
        app = _App(_SegRec(), meeting_audio_mode="smart_meeting")
        s = self.session(app)
        s._followup.poll_s = 0.0
        s._followup.settle_polls = 0
        seen = {}

        def hook(_d):
            part = ms.load_segments(s.meeting_dir)["parts"][0]
            seen["before"] = ([x.get("spk") for x in part["segments"]], part.get("canon"))
        with patch.object(msess.meeting_speakers, "diarization", _interruptible(hook)), \
                patch.object(msess.actions, "process", return_value="## Notes\n- Ship Friday"):
            self.assertTrue(s.start("Call", "", audio_mode="smart_meeting", language="en")[0])
            self.assertTrue(s.stop(""))
            self._let_the_speaker_pass_end(s)
        folder = s.meeting_dir
        self.assertEqual(seen["before"], (["you", "them", "you", "them"],
                                          "You: Hello team.\nThem: Welcome.\nYou: Thanks.\nThem: Let us ship on Friday."))
        part = ms.load_segments(folder)["parts"][0]
        self.assertEqual([x["spk"] for x in part["segments"]], ["you", "s1", "you", "s1"])
        self.assertEqual(sm.load(folder).order, ["you", "s1"])
