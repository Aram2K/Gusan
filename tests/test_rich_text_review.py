"""rich_text / docx_export review guards (Gusan 2.0, D9 / D10): a ticked
meeting checkbox rewrites its own notes.md line whatever odd characters the
notes hold, speech that merely contains a colon is not a speaker label, and
the Word XML keeps the element order Word checks. Pure stdlib, no Qt."""
import io
import os
import re
import sys
import unittest
import zipfile
from xml.dom import minidom

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import docx_export as dx  # noqa: E402
import rich_text as rt  # noqa: E402


def _tasks(md):
    return [b for b in rt.parse_blocks(md) if b[0] == "task"]


class TestTaskLineIndex(unittest.TestCase):
    """line_index must count lines exactly as toggle_task_line does ("\\n")."""

    def _tick_each(self, md):
        for t in _tasks(md):
            flipped = rt.toggle_task_line(md, t[3], True).split("\n")
            self.assertTrue(flipped[t[3]].rstrip("\r").endswith(t[2]), (md, t))
            self.assertIn("[x]", flipped[t[3]])
            # Every other line is untouched.
            original = md.split("\n")
            self.assertEqual([ln for i, ln in enumerate(flipped) if i != t[3]],
                             [ln for i, ln in enumerate(original) if i != t[3]])

    def test_unicode_line_separators_do_not_shift_the_index(self):
        for sep in (" ", " ", "\x85", "\x0c", "\x1c", "\x1e", "\x0b"):
            md = "## Action items\nIntro%sstill intro\n- [ ] One\n- [ ] Two" % sep
            tasks = _tasks(md)
            self.assertEqual([(t[2], t[3]) for t in tasks], [("One", 2), ("Two", 3)], repr(sep))
            self._tick_each(md)

    def test_crlf_notes(self):
        md = "## Action items\r\n- [ ] One\r\n\r\n- [ ] Two\r\n"
        self.assertEqual([(t[2], t[3]) for t in _tasks(md)], [("One", 1), ("Two", 3)])
        self._tick_each(md)
        self.assertEqual(rt.to_plain(md), "Action items\n☐ One\n\n☐ Two")

    def test_code_fence_body_keeps_no_carriage_returns(self):
        self.assertEqual(rt.parse_blocks("```\r\na\r\n  b\r\n```\r\n- [ ] t"),
                         [("code", "", "a\n  b"), ("task", False, "t", 4)])


class TestSpeakerLabels(unittest.TestCase):
    def test_real_labels(self):
        for line, label in (("Speaker 1: Hi", "Speaker 1"), ("Speaker ?: Hi", "Speaker ?"),
                            ("You: Hi", "You"), ("Them: Hi", "Them"), ("Anna: Hi", "Anna"),
                            ("Anna Lee: Hi", "Anna Lee"), ("Dr. O'Brien: Hi", "Dr. O'Brien"),
                            ("Anna-Maria: Hi", "Anna-Maria")):
            self.assertEqual(rt.split_speaker(line), (label, "Hi"), line)

    def test_speech_with_a_colon_is_not_a_label(self):
        for line in ("So here's the thing: we ship on Friday.",
                     "Okay so the plan is: ship it.",
                     "This is it: done.",
                     "The main points are: price and date."):
            self.assertEqual(rt.split_speaker(line), ("", line), line)

    def test_a_packed_wall_of_text_keeps_its_colon(self):
        talk = "So here's the thing: " + " ".join("Sentence %d ends here." % i for i in range(60))
        paras = rt.transcript_paragraphs(talk)
        self.assertGreater(len(paras), 1)
        self.assertTrue(paras[0].startswith("So here's the thing: Sentence 0"))
        self.assertEqual(rt.split_speaker(paras[0])[0], "")


_PPR_ORDER = ["keepNext", "spacing", "ind"]


class TestWordXmlOrder(unittest.TestCase):
    """Word validates w:pPr children against the schema's sequence."""

    NOTES = ("## Summary\nWe agreed on **14 October** & *beta*.\n\n**Decisions:**\n1. Launch\n- Keep it\n\n"
             "## Action items\n- [ ] Fix drift (Owner: David, Due: Wed)\n- [x] Done\n> Ask legal\n"
             "```\na = 1\nb = 2\n```")

    def _doc(self):
        paras = dx.notes_paragraphs(self.NOTES) + [
            {"style": "heading", "text": "Transcript"},
            {"start": 0, "speaker": None, "label": "Speaker 1", "text": "Hello <team> & all."},
            {"style": "marker", "text": "— Resumed at 11:20 —"},
        ]
        data = dx.build_docx("Q4 roadmap", paras, meta_line="8 Oct", include_timestamps=False)
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            self.assertIsNone(z.testzip())
            parts = {n: z.read(n).decode("utf-8") for n in z.namelist()}
        for xml in parts.values():
            minidom.parseString(xml)
        return parts["word/document.xml"]

    def test_paragraph_properties_in_schema_order(self):
        doc = minidom.parseString(self._doc())
        ppr = doc.getElementsByTagName("w:pPr")
        self.assertGreater(len(ppr), 10)
        for p in ppr:
            names = [c.tagName[2:] for c in p.childNodes if c.nodeType == c.ELEMENT_NODE]
            self.assertEqual(names, [n for n in _PPR_ORDER if n in names], names)

    def test_every_run_has_text_tab_or_break(self):
        doc = minidom.parseString(self._doc())
        for r in doc.getElementsByTagName("w:r"):
            kids = [c.tagName for c in r.childNodes if c.nodeType == c.ELEMENT_NODE]
            self.assertTrue(kids in (["w:rPr", "w:t"], ["w:tab"], ["w:br"]), kids)

    def test_text_is_whole_and_escaped(self):
        xml = self._doc()
        text = "".join(re.findall(r'<w:t xml:space="preserve">([^<]*)</w:t>', xml))
        for words in ("Summary", "14 October", "Fix drift", "(David · by Wed)", "Ask legal",
                      "Hello &lt;team&gt; &amp; all.", "— Resumed at 11:20 —"):
            self.assertIn(words, text)
        for raw in ("**", "## ", "- [", "```"):
            self.assertNotIn(raw, text)


if __name__ == "__main__":
    unittest.main()
