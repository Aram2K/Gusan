"""transcript_store: the saved Audio to text library and its exports (pure).

Golden .txt / .md / .srt outputs are spelled out in full so any change to the
exported text is a deliberate one. The library runs in a throwaway data folder
(TRANSCRIBE_APP_DATA_DIR) per test.
"""
import datetime
import io
import os
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock
from xml.dom import minidom

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import transcript_store as ts  # noqa: E402

SEGMENTS = [
    {"start": 0.0, "end": 3.5, "text": "Thanks for coming in, Mariam.", "speaker": 0},
    {"start": 3.6, "end": 5.2, "text": "How was the drive?", "speaker": 0},
    {"start": 5.4, "end": 8.0, "text": "Easy, thanks. Traffic was kind today.", "speaker": 1},
    {"start": 65.0, "end": 71.25, "text": "Last year I redesigned the booking app for a clinic. "
                                          "Fewer steps, bigger buttons, and a much shorter form.",
     "speaker": 1},
]


def make_result(path="interview_mariam.m4a", **kw):
    r = {"path": path, "name": os.path.basename(path), "duration": 72.0,
         "segments": [dict(s) for s in SEGMENTS], "timestamps": True, "speakers": True,
         "speaker_count": 2, "model": "small", "language": "en", "language_setting": "auto"}
    r.update(kw)
    return r


GOLDEN_TEXT = """\
[00:00] Speaker 1: Thanks for coming in, Mariam. How was the drive?

[00:05] Speaker 2: Easy, thanks. Traffic was kind today.

[01:05] Speaker 2: Last year I redesigned the booking app for a clinic. Fewer steps, bigger buttons, and a much shorter form."""

GOLDEN_TXT = """\
interview_mariam
Transcribed from interview_mariam.m4a · 1 min 12 s · Gusan

""" + GOLDEN_TEXT + "\n"

GOLDEN_MD = """\
# interview_mariam

Transcribed from interview_mariam.m4a · 1 min 12 s · Gusan

**[00:00] Speaker 1:** Thanks for coming in, Mariam. How was the drive?

**[00:05] Speaker 2:** Easy, thanks. Traffic was kind today.

**[01:05] Speaker 2:** Last year I redesigned the booking app for a clinic. Fewer steps, bigger buttons, and a much shorter form.
"""

GOLDEN_SRT = """\
1
00:00:00,000 --> 00:00:03,500
Speaker 1: Thanks for coming in, Mariam.

2
00:00:03,600 --> 00:00:05,200
How was the drive?

3
00:00:05,400 --> 00:00:08,000
Speaker 2: Easy, thanks. Traffic was kind
today.

4
00:01:05,000 --> 00:01:10,060
Last year I redesigned the booking app for
a clinic. Fewer steps, bigger buttons, and

5
00:01:10,060 --> 00:01:11,250
a much shorter form.
"""


class TestGoldenExports(unittest.TestCase):
    def test_copy_text(self):
        self.assertEqual(ts.plain_text(make_result()), GOLDEN_TEXT)

    def test_txt(self):
        self.assertEqual(ts.to_txt(make_result()), GOLDEN_TXT)

    def test_markdown(self):
        self.assertEqual(ts.to_markdown(make_result()), GOLDEN_MD)

    def test_srt(self):
        self.assertEqual(ts.to_srt(make_result()), GOLDEN_SRT)

    def test_no_speakers_no_timestamps(self):
        segs = [{"start": 0.0, "end": 2.0, "text": "# not a heading"},
                {"start": 9.0, "end": 12.0, "text": "1. First * star [link](x) <b> `code`"}]
        doc = make_result(segments=segs, timestamps=False, speaker_count=0, duration=12.4)
        self.assertEqual(ts.plain_text(doc), "# not a heading\n\n1. First * star [link](x) <b> `code`")
        self.assertEqual(ts.to_markdown(doc), (
            "# interview_mariam\n\n"
            "Transcribed from interview_mariam.m4a · 12 s · Gusan\n\n"
            "\\# not a heading\n\n"
            "1\\. First \\* star \\[link\\](x) \\<b> \\`code\\`\n"))
        self.assertEqual(ts.to_srt(doc), (
            "1\n00:00:00,000 --> 00:00:02,000\n# not a heading\n\n"
            "2\n00:00:09,000 --> 00:00:12,000\n1. First * star [link](x) <b> `code`\n"))

    def test_timestamps_override_and_speaker_names(self):
        doc = make_result(speaker_names={"0": "Anna", 1: "Mariam"})
        first = ts.plain_text(doc, timestamps=False).split("\n\n")[0]
        self.assertEqual(first, "Anna: Thanks for coming in, Mariam. How was the drive?")
        self.assertIn("Mariam: Easy, thanks.", ts.to_srt(doc))

    def test_renamed_title_is_the_document_title(self):
        doc = make_result(title="Interview with Mariam")
        self.assertTrue(ts.to_txt(doc).startswith("Interview with Mariam\n"))
        self.assertEqual(ts.suggested_name(doc, "md"), "Interview with Mariam transcript.md")
        self.assertEqual(ts.suggested_name(make_result(), ".SRT"), "interview_mariam transcript.srt")


class TestSrtDetails(unittest.TestCase):
    def test_time_format(self):
        self.assertEqual(ts.srt_time(0), "00:00:00,000")
        self.assertEqual(ts.srt_time(3725.4567), "01:02:05,457")
        self.assertEqual(ts.srt_time(-3), "00:00:00,000")

    def test_long_segments_become_two_line_cues_in_order(self):
        words = " ".join(f"word{i}" for i in range(60))
        doc = make_result(segments=[{"start": 10.0, "end": 40.0, "text": words}])
        cues = ts.srt_cues(doc)
        self.assertGreater(len(cues), 2)
        self.assertEqual(cues[0][0], 10.0)
        self.assertAlmostEqual(cues[-1][1], 40.0)
        for (s, e, lines), nxt in zip(cues, cues[1:] + [None]):
            self.assertLess(s, e)
            self.assertLessEqual(len(lines), ts.SRT_MAX_LINES)
            self.assertTrue(all(len(line) <= ts.SRT_LINE_CHARS for line in lines))
            if nxt:
                self.assertAlmostEqual(e, nxt[0])
        self.assertEqual(" ".join(" ".join(c[2]) for c in cues), words)

    def test_zero_length_segment_gets_time(self):
        doc = make_result(segments=[{"start": 5.0, "end": 5.0, "text": "Hi."}])
        self.assertEqual(ts.srt_cues(doc), [(5.0, 7.0, ["Hi."])])


class TestWordExport(unittest.TestCase):
    def test_creator_is_gusan_and_meta_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = ts.export_to(make_result(), "docx", os.path.join(tmp, "a.docx"))
            with zipfile.ZipFile(out) as z:
                core = z.read("docProps/core.xml").decode("utf-8")
                doc = z.read("word/document.xml").decode("utf-8")
            minidom.parseString(core)
            minidom.parseString(doc)
            self.assertIn("<dc:creator>Gusan</dc:creator>", core)
            self.assertNotIn("Transcribe App", core + doc)
            self.assertIn("Transcribed from interview_mariam.m4a · 1 min 12 s · Gusan", doc)
            self.assertIn("Speaker 2:", doc)
            self.assertIn("[01:05]", doc)

    def test_text_exports_write_utf8(self):
        with tempfile.TemporaryDirectory() as tmp:
            for fmt, golden in (("txt", GOLDEN_TXT), ("md", GOLDEN_MD), ("srt", GOLDEN_SRT)):
                p = ts.export_to(make_result(), fmt, os.path.join(tmp, "out." + fmt))
                with open(p, "r", encoding="utf-8") as f:
                    self.assertEqual(f.read(), golden)
            with self.assertRaises(ValueError):
                ts.export_to(make_result(), "pdf", os.path.join(tmp, "x.pdf"))

    def test_unique_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "a transcript.docx"
            self.assertEqual(ts.unique_path(p), p)
            p.write_bytes(b"x")
            self.assertEqual(ts.unique_path(p).name, "a transcript (2).docx")
            (Path(tmp) / "a transcript (2).docx").write_bytes(b"x")
            self.assertEqual(ts.unique_path(p).name, "a transcript (3).docx")

    def test_save_word_copy_next_to_source_then_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "src"
            src.mkdir()
            audio = src / "talk.mp3"
            audio.write_bytes(b"")
            first = ts.save_word_copy(make_result(path=str(audio)))
            self.assertEqual(Path(first), src / "talk transcript.docx")
            second = ts.save_word_copy(make_result(path=str(audio)))
            self.assertEqual(Path(second).name, "talk transcript (2).docx")
            # The source folder can't be written: Documents/Gusan instead.
            docs = Path(tmp) / "Documents"
            real_save = ts.save_word

            def picky(doc, path):
                if Path(path).parent == src:
                    raise PermissionError("read-only")
                return real_save(doc, path)
            with mock.patch.object(ts, "documents_dir", lambda: docs), \
                    mock.patch.object(ts, "save_word", picky):
                third = ts.save_word_copy(make_result(path=str(audio)))
            self.assertEqual(Path(third), docs / "Gusan" / "talk transcript.docx")
            self.assertTrue(zipfile.is_zipfile(third))


class TestFileKind(unittest.TestCase):
    def test_kinds(self):
        self.assertEqual(ts.file_kind("lecture_week3.mp4"), "video")
        self.assertEqual(ts.file_kind("board_call.mp3"), "call")
        self.assertEqual(ts.file_kind("GMT20240101-120000_Recording.m4a"), "call")
        self.assertEqual(ts.file_kind(r"C:\a\Team Meeting.wav"), "call")
        self.assertEqual(ts.file_kind("interview_mariam.m4a"), "voice")
        self.assertEqual(ts.file_kind("recall_notes.m4a"), "voice")
        self.assertEqual(ts.file_kind(""), "voice")

    def test_format_duration(self):
        self.assertEqual(ts.format_duration(59), "59 s")
        self.assertEqual(ts.format_duration(125), "2 min 05 s")
        self.assertEqual(ts.format_duration(3725), "1 h 02 min")
        self.assertEqual(ts.format_duration(None), "0 s")


class TestLibrary(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.data = Path(self._tmp.name)
        self._env = mock.patch.dict(os.environ, {"TRANSCRIBE_APP_DATA_DIR": str(self.data)})
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()

    def _at(self, h, m, s=0):
        return datetime.datetime(2026, 10, 7, h, m, s).astimezone()

    def test_root_keeps_the_existing_data_folder(self):
        self.assertEqual(ts.transcripts_dir(), self.data / "transcripts")

    def test_save_list_load_round_trip(self):
        source = os.path.join(str(self.data), "rec", "interview_mariam.m4a")
        folder = ts.save_transcript(make_result(path=source), created=self._at(11, 20))
        self.assertEqual(folder.name, "20261007_112000")
        self.assertTrue((folder / "meta.json").is_file())
        self.assertTrue((folder / "segments.json").is_file())
        rows = ts.list_transcripts()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["id"], "20261007_112000")
        self.assertEqual(row["title"], "interview_mariam.m4a")
        self.assertEqual(row["source_path"], source)
        self.assertEqual((row["duration_sec"], row["model"], row["language"]), (72.0, "small", "en"))
        self.assertEqual((row["speaker_count"], row["timestamps"], row["kind"]), (2, True, "voice"))
        doc = ts.load_transcript(row["id"])
        self.assertEqual(len(doc["segments"]), 4)
        self.assertEqual(doc["segments"][3]["end"], 71.25)
        self.assertEqual([p["start"] for p in doc["paragraphs"]], [0.0, 5.4, 65.0])
        # A loaded transcript exports exactly like the fresh result.
        self.assertEqual(ts.to_srt(doc), GOLDEN_SRT)
        self.assertEqual(ts.to_markdown(doc), GOLDEN_MD)
        self.assertEqual(ts.load_transcript(row["dir"])["id"], row["id"])

    def test_newest_first_and_same_second(self):
        ts.save_transcript(make_result(path="a.mp3"), created=self._at(9, 0))
        ts.save_transcript(make_result(path="b.mp3"), created=self._at(10, 0))
        ts.save_transcript(make_result(path="c.mp3"), created=self._at(10, 0))
        rows = ts.list_transcripts()
        self.assertEqual([r["source_name"] for r in rows], ["c.mp3", "b.mp3", "a.mp3"])
        self.assertEqual(rows[0]["id"], "20261007_100000_2")
        self.assertEqual(len(ts.list_transcripts(limit=2)), 2)

    def test_unfinished_folder_is_not_listed(self):
        ts.save_transcript(make_result(), created=self._at(9, 0))
        (ts.transcripts_dir() / "20261007_093000").mkdir()
        self.assertEqual(len(ts.list_transcripts()), 1)
        self.assertIsNone(ts.load_transcript("20261007_093000"))

    def test_rename_and_update_meta(self):
        folder = ts.save_transcript(make_result(), created=self._at(9, 0))
        self.assertTrue(ts.rename_transcript(folder.name, "  Interview   with Mariam "))
        self.assertEqual(ts.load_transcript(folder.name)["title"], "Interview with Mariam")
        self.assertFalse(ts.rename_transcript(folder.name, "   "))
        self.assertTrue(ts.update_meta(folder.name, timestamps=False))
        self.assertFalse(ts.load_transcript(folder.name)["timestamps"])
        self.assertFalse(ts.update_meta(folder.name, model="large-v3"))   # not editable
        self.assertTrue(ts.update_meta(folder.name, speaker_names={0: "Anna"}))
        self.assertEqual(ts.load_transcript(folder.name)["speaker_names"], {0: "Anna"})
        word = self.data / "out" / "talk transcript.docx"        # a Path is stored as text
        self.assertTrue(ts.update_meta(folder.name, word_path=word))
        self.assertEqual(ts.load_transcript(folder.name)["word_path"], str(word))
        self.assertFalse(ts.rename_transcript("20990101_000000", "x"))

    def test_delete_stays_inside_the_library(self):
        keep = ts.save_transcript(make_result(), created=self._at(9, 0))
        outside = self.data / "meetings" / "20261007_090000"
        outside.mkdir(parents=True)
        nested = keep / "inner"
        nested.mkdir()
        for bad in ("", "..", ".", str(self.data), str(ts.transcripts_dir()), str(outside),
                    str(nested), "../meetings/20261007_090000", "missing"):
            self.assertFalse(ts.delete_transcript(bad), bad)
        self.assertTrue(outside.is_dir())
        self.assertTrue(keep.is_dir())
        self.assertTrue(ts.delete_transcript(keep.name))
        self.assertFalse(keep.exists())
        self.assertEqual(ts.list_transcripts(), [])

    def test_delete_all_keeps_one(self):
        a = ts.save_transcript(make_result(path="a.mp3"), created=self._at(9, 0))
        ts.save_transcript(make_result(path="b.mp3"), created=self._at(9, 1))
        (ts.transcripts_dir() / "leftover").mkdir()
        self.assertEqual(ts.delete_all_transcripts(keep=a.name), 2)
        self.assertEqual([r["id"] for r in ts.list_transcripts()], [a.name])
        self.assertEqual(ts.delete_all_transcripts(), 1)
        self.assertEqual(ts.delete_all_transcripts(), 0)

    def test_bad_segments_are_cleaned(self):
        result = make_result(segments=[
            {"start": "1.5", "end": None, "text": " hi ", "speaker": "1"},
            {"start": 2, "end": 3, "text": "   "},
            "junk",
            {"start": float("nan"), "end": 4, "text": "there", "speaker": True},
        ])
        folder = ts.save_transcript(result, created=self._at(9, 0))
        segs = ts.load_transcript(folder.name)["segments"]
        self.assertEqual(segs, [{"start": 1.5, "end": 1.5, "text": "hi", "speaker": 1},
                                {"start": 0.0, "end": 4.0, "text": "there", "speaker": None}])


if __name__ == "__main__":
    unittest.main()
