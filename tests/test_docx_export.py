"""docx_export: valid OPC package, correct content, sane paragraph grouping.

Pure stdlib - the round-trip opens the produced bytes with zipfile and parses
every XML part, which is exactly what Word does first.
"""
import unittest
import zipfile
import io
from xml.dom import minidom

import docx_export as dx


def _parts(data):
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        return {n: z.read(n).decode("utf-8") for n in z.namelist()}


class TestBuildDocx(unittest.TestCase):
    PARAS = [
        {"start": 0.0, "speaker": 0, "text": "Hello there, welcome."},
        {"start": 65.0, "speaker": 1, "text": "Thanks — glad to be here."},
    ]

    def test_package_structure(self):
        parts = _parts(dx.build_docx("My Interview", self.PARAS))
        self.assertIn("[Content_Types].xml", parts)
        self.assertIn("_rels/.rels", parts)
        self.assertIn("word/document.xml", parts)
        self.assertIn("docProps/core.xml", parts)

    def test_every_part_is_wellformed_xml(self):
        for name, xml in _parts(dx.build_docx("T", self.PARAS)).items():
            minidom.parseString(xml)   # raises on malformed XML

    def test_content_present(self):
        doc = _parts(dx.build_docx("My Interview", self.PARAS,
                                   meta_line="recording.mp3 · 1 h 02 min"))["word/document.xml"]
        self.assertIn("My Interview", doc)
        self.assertIn("recording.mp3", doc)
        self.assertIn("Hello there, welcome.", doc)
        self.assertIn("Speaker 1:", doc)
        self.assertIn("Speaker 2:", doc)
        self.assertIn("[01:05]", doc)

    def test_timestamps_can_be_disabled(self):
        doc = _parts(dx.build_docx("T", self.PARAS,
                                   include_timestamps=False))["word/document.xml"]
        self.assertNotIn("[01:05]", doc)

    def test_xml_injection_is_escaped(self):
        evil = [{"start": 0, "speaker": None,
                 "text": 'Tags <w:p> & "quotes" </w:document> here'}]
        doc = _parts(dx.build_docx("A <b>&</b> title", evil))["word/document.xml"]
        minidom.parseString(doc)
        self.assertIn("&lt;w:p&gt;", doc)
        self.assertIn("&amp;", doc)

    def test_empty_transcript_still_valid(self):
        parts = _parts(dx.build_docx("Empty", []))
        minidom.parseString(parts["word/document.xml"])
        self.assertIn("No speech", parts["word/document.xml"])

    def test_xml_illegal_control_chars_are_stripped(self):
        # Regression: a raw backspace (0x08) from Whisper's byte-level
        # decoder made Word refuse the whole document ("unreadable
        # content"). Illegal-in-XML characters must be stripped and every
        # part must still parse. Control chars are built with chr() so the
        # test source itself contains none.
        bad = "hello " + chr(8) + " world " + chr(0) + chr(31) + " end"
        paras = [{"start": 0, "speaker": 0, "text": bad}]
        parts = _parts(dx.build_docx("Bad " + chr(8) + " title", paras))
        for xml in parts.values():
            minidom.parseString(xml)
        doc = parts["word/document.xml"]
        self.assertIn("hello  world", doc)
        for code in (0, 8, 31):
            self.assertNotIn(chr(code), doc)

    def test_legal_whitespace_survives(self):
        # Tab is LEGAL XML - the sanitizer must not eat it.
        paras = [{"start": 0, "speaker": None, "text": "a" + chr(9) + "b"}]
        doc = _parts(dx.build_docx("T", paras))["word/document.xml"]
        self.assertIn("a" + chr(9) + "b", doc)

    def test_unicode_survives(self):
        paras = [{"start": 0, "speaker": None, "text": "Բարեւ ձեզ — привет 你好"}]
        doc = _parts(dx.build_docx("Յakob", paras))["word/document.xml"]
        self.assertIn("Բարեւ ձեզ", doc)


NOTES = """## Summary
The team agreed on **14 October**.

**Decisions:**
1. Launch on 14 October.
- Keep the *beta* price

## Action items
- [ ] Fix speaker-label drift (Owner: David, Due: Wed)
- [x] Confirm the date (Owner: Aram)
> Ask legal first
"""


class TestStyledNotes(unittest.TestCase):
    """Meeting notes in Word (Gusan 2.0): real headings and list items, no
    Markdown symbols."""

    def test_notes_paragraphs_are_typed_blocks(self):
        paras = dx.notes_paragraphs(NOTES)
        self.assertEqual([p["style"] for p in paras],
                         ["heading", "body", "subheading", "num", "bullet", "heading", "task", "task", "quote"])
        self.assertEqual(paras[3]["n"], 1)
        task = paras[6]
        self.assertEqual((task["text"], task["after"], task["done"]),
                         ("Fix speaker-label drift", "(David · by Wed)", False))
        self.assertTrue(paras[7]["done"])

    def test_word_xml_has_bold_headings_glyphs_and_runs(self):
        doc = _parts(dx.build_docx("Q4 roadmap", dx.notes_paragraphs(NOTES),
                                   include_timestamps=False))["word/document.xml"]
        minidom.parseString(doc)
        for raw in ("##", "**", "- [ ]", "[x]", "> Ask"):
            self.assertNotIn(raw, doc)
        # A heading: bold 14 pt, kept with what follows.
        self.assertIn('<w:keepNext/>', doc)
        self.assertIn('<w:sz w:val="28"/><w:szCs w:val="28"/><w:b/></w:rPr>'
                      '<w:t xml:space="preserve">Summary</w:t>', doc)
        # **bold** inside a sentence is a bold run of its own.
        self.assertIn('<w:b/></w:rPr><w:t xml:space="preserve">14 October</w:t>', doc)
        # *italic* too.
        self.assertIn('<w:i/></w:rPr><w:t xml:space="preserve">beta</w:t>', doc)
        # Items hang their glyph: •, 1., ☐ and ☑, the owner muted after the task.
        self.assertEqual(doc.count('w:hanging="280"'), 4)
        for glyph in ("•", "1.", "☐", "☑"):
            self.assertIn('<w:t xml:space="preserve">%s</w:t></w:r><w:r><w:tab/></w:r>' % glyph, doc)
        self.assertIn("(David · by Wed)", doc)
        self.assertIn("Ask legal first", doc)

    def test_labels_markers_and_line_breaks(self):
        paras = [{"start": 0, "speaker": None, "label": "Anna", "text": "Hello."},
                 {"style": "marker", "text": "— Resumed at 11:20 —"},
                 {"style": "code", "text": "a = 1\nb = 2"}]
        doc = _parts(dx.build_docx("T", paras, include_timestamps=False))["word/document.xml"]
        minidom.parseString(doc)
        self.assertIn(">Anna:  <", doc)
        self.assertIn('<w:i/><w:color w:val="64748B"/></w:rPr><w:t xml:space="preserve">— Resumed at 11:20 —', doc)
        self.assertIn('<w:r><w:br/></w:r>', doc)
        self.assertIn('w:ascii="Consolas"', doc)

    def test_plain_transcripts_look_as_before(self):
        # Audio to text's paragraphs (no style keys): one run per part, as before.
        doc = _parts(dx.build_docx("T", [{"start": 0, "speaker": 0, "text": "**not bold** here"}],
                                   include_timestamps=False))["word/document.xml"]
        self.assertIn(">**not bold** here<", doc)       # no "md": the words exactly as said
        self.assertNotIn("<w:br/>", doc)

    def test_inline_runs(self):
        self.assertEqual(dx.inline_runs("**Fix** the *drift* & `x`"),
                         [("Fix", True, False), (" the ", False, False), ("drift", False, True),
                          (" & x", False, False)])
        self.assertEqual(dx.inline_runs("<b>not a tag</b>"), [("<b>not a tag</b>", False, False)])


class TestGroupSegments(unittest.TestCase):
    def _seg(self, start, end, text, speaker=None):
        return {"start": start, "end": end, "text": text, "speaker": speaker}

    def test_merges_close_segments(self):
        out = dx.group_segments([self._seg(0, 2, "one"), self._seg(2.5, 4, "two")])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["text"], "one two")

    def test_breaks_on_speaker_change(self):
        out = dx.group_segments([self._seg(0, 2, "hi", 0), self._seg(2, 4, "hey", 1)])
        self.assertEqual([p["speaker"] for p in out], [0, 1])

    def test_breaks_on_silence_gap(self):
        out = dx.group_segments([self._seg(0, 2, "one"), self._seg(9, 10, "two")])
        self.assertEqual(len(out), 2)
        self.assertEqual(out[1]["start"], 9)

    def test_breaks_on_length(self):
        segs = [self._seg(i * 2, i * 2 + 2, "word " * 30) for i in range(10)]
        out = dx.group_segments(segs)
        self.assertGreater(len(out), 1)
        self.assertTrue(all(len(p["text"]) <= 750 for p in out))

    def test_empty_and_blank(self):
        self.assertEqual(dx.group_segments([]), [])
        self.assertEqual(dx.group_segments([self._seg(0, 1, "   ")]), [])


class TestHelpers(unittest.TestCase):
    def test_timestamps(self):
        self.assertEqual(dx.format_timestamp(0), "00:00")
        self.assertEqual(dx.format_timestamp(65), "01:05")
        self.assertEqual(dx.format_timestamp(3661), "1:01:01")

    def test_speaker_names(self):
        self.assertEqual(dx.speaker_name(0), "Speaker 1")
        self.assertEqual(dx.speaker_name(None), "")
        self.assertEqual(dx.speaker_name(0, {0: "Aram"}), "Aram")

    def test_safe_filename(self):
        self.assertEqual(dx.safe_filename('bad:"name?.mp3'), "bad name .mp3")
        self.assertEqual(dx.safe_filename(""), "transcript")


if __name__ == "__main__":
    unittest.main()
