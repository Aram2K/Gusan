"""rich_text: Markdown rendered for display, copy and paste (D9) and meeting
transcripts as paragraphs (D10). Pure stdlib, no Qt."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import rich_text as rt  # noqa: E402

TODO = "- [ ] Call David\n- [x] Book the room\n- [ ] Send notes (Owner: Anna, Due: Fri)"

NOTES = ("## Summary\nWe met about the beta.\nIt went well.\n\n"
         "## Key decisions\n- Ship **beta** on Friday\n- Keep _Qt_\n\n"
         "## Action items\n- [ ] `Fix speaker-label drift` (Owner: David, Due: Wed)\n"
         "- [x] Book the room (Owner: -)")

EMAIL = "Subject: Q3 Report Ready\n\nHi John,\n\nThe Q3 report is ready.\n\nBest,\nAram"

PROSE = [
    "Hello there, this is a plain sentence.",
    "Line one\nLine two\n\nA new paragraph.  Two spaces stay.\n",
    "Ich habe 3 * 4 = 12 gerechnet - fertig.",
    "Բարև, ինչպե՞ս ես։ Ամեն ինչ լավ է։",
    "   leading spaces and trailing   ",
    "a_snake_case_name and 5*3",
    "",
]


class TestHasMarkup(unittest.TestCase):
    def test_markup(self):
        for md in (TODO, NOTES, "# Title", "* item", "• item", "1. one", "2) two", "> quote",
                   "```\ncode\n```", "say **this**", "run `ls`", "text\n- item", "-[ ] task"):
            self.assertTrue(rt.has_markup(md), md)

    def test_no_markup(self):
        for md in PROSE + [EMAIL, None, "#hashtag", "*not a list", "-5 degrees", "2026 was good"]:
            self.assertFalse(rt.has_markup(md), md)


class TestPlain(unittest.TestCase):
    def test_todo(self):
        self.assertEqual(rt.to_plain(TODO),
                         "☐ Call David\n☑ Book the room\n☐ Send notes (Anna · by Fri)")

    def test_meeting_notes(self):
        self.assertEqual(rt.to_plain(NOTES),
                         "Summary\nWe met about the beta.\nIt went well.\n\n"
                         "Key decisions\n• Ship beta on Friday\n• Keep Qt\n\n"
                         "Action items\n☐ Fix speaker-label drift (David · by Wed)\n☑ Book the room")

    def test_no_markers_left(self):
        out = rt.to_plain(NOTES)
        for marker in ("**", "##", "`", "- [", "_Qt_"):
            self.assertNotIn(marker, out)

    def test_prose_is_byte_identical(self):
        for md in PROSE + [EMAIL]:
            self.assertIs(rt.to_plain(md), md)
        self.assertEqual(rt.to_plain(None), "")

    def test_email_with_markup_keeps_its_lines(self):
        md = "Subject: Plan\n\nHi Anna,\n\n**Key points:**\n- one\n- two\n\nBest,\nAram"
        self.assertEqual(rt.to_plain(md),
                         "Subject: Plan\n\nHi Anna,\n\nKey points\n• one\n• two\n\nBest,\nAram")

    def test_heading_gets_a_blank_line_before(self):
        self.assertEqual(rt.to_plain("Intro line\n# Title\nBody"), "Intro line\n\nTitle\nBody")
        self.assertEqual(rt.to_plain("# Title\n\nBody"), "Title\nBody")

    def test_numbered(self):
        self.assertEqual(rt.to_plain("Steps:\n1. Open it\n2. Click **Save**\n3) Done"),
                         "Steps:\n1. Open it\n2. Click Save\n3. Done")

    def test_code_fence(self):
        md = "Run this:\n\n```python\nx = 1\n    y = 2\n```\n\nThen **go**."
        self.assertEqual(rt.to_plain(md), "Run this:\n\nx = 1\n    y = 2\n\nThen go.")

    def test_rule_is_a_blank_line(self):
        self.assertEqual(rt.to_plain("- a\n---\n- b"), "• a\n\n• b")


class TestHtml(unittest.TestCase):
    def test_todo(self):
        html = rt.to_html(TODO)
        self.assertEqual(html, "<p>☐&nbsp;Call David<br>☑&nbsp;Book the room<br>"
                               "☐&nbsp;Send notes (Anna · by Fri)</p>")

    def test_notes(self):
        html = rt.to_html(NOTES)
        self.assertIn("<h3>Summary</h3>", html)
        self.assertIn("<p>We met about the beta.<br>It went well.</p>", html)
        self.assertIn("<ul><li>Ship <b>beta</b> on Friday</li><li>Keep <i>Qt</i></li></ul>", html)
        self.assertIn("☐&nbsp;Fix speaker-label drift (David · by Wed)", html)

    def test_minimal_no_styling(self):
        html = rt.to_html(NOTES + "\n\n```\ncode\n```")
        for bad in ("style=", "font", "color", "-qt-", "class="):
            self.assertNotIn(bad, html)

    def test_numbered_and_code(self):
        self.assertEqual(rt.to_html("1. a\n2. b"), "<ol><li>a</li><li>b</li></ol>")
        self.assertEqual(rt.to_html("3. c\n4. d"), '<ol start="3"><li>c</li><li>d</li></ol>')
        self.assertEqual(rt.to_html("```js\nif (a < b) {}\n```"), "<pre>if (a &lt; b) {}</pre>")
        self.assertEqual(rt.to_html("use `ls -la` now"), "<p>use <code>ls -la</code> now</p>")

    def test_escapes(self):
        self.assertEqual(rt.to_html("- <script>x</script> & y"),
                         "<ul><li>&lt;script&gt;x&lt;/script&gt; &amp; y</li></ul>")

    def test_bold_label_is_bold_paragraph(self):
        self.assertEqual(rt.to_html("**Key points:**\n- a"), "<p><b>Key points</b></p>\n<ul><li>a</li></ul>")

    def test_quote(self):
        self.assertEqual(rt.to_html("> said it"), "<blockquote>said it</blockquote>")

    def test_qt_html(self):
        html = rt.qt_html(TODO + "\n\n- point\n\n## Head", {"muted": "#888888", "text": "#111111"})
        self.assertIn("☐&nbsp;Call David", html)
        self.assertIn('<span style="color:#888888;">☑&nbsp;Book the room</span>', html)
        self.assertIn("<ul", html)
        self.assertIn("<li>point</li>", html)
        self.assertIn("<h3", html)
        self.assertNotIn("**", rt.qt_html("**b**"))
        self.assertIn("<b>b</b>", rt.qt_html("**b**"))
        self.assertIn("Line one<br>Line two", rt.qt_html("Line one\nLine two"))


class TestBlocks(unittest.TestCase):
    def test_kinds(self):
        md = ("# Title\nPara one\nstill one\n\n- bullet\n1. first\n- [x] done task\n"
              "> quote\n```py\ncode\n```\n**Label:**")
        self.assertEqual(rt.parse_blocks(md), [
            ("head", 1, "Title"), ("para", "Para one\nstill one"), ("bullet", "bullet"),
            ("num", 1, "first"), ("task", True, "done task", 6), ("quote", "quote"),
            ("code", "py", "code"), ("head", 4, "Label"),
        ])

    def test_task_line_index_round_trips(self):
        md = "## Action items\n\n- [ ] One\nnote\n- [ ] Two\n"
        tasks = [b for b in rt.parse_blocks(md) if b[0] == "task"]
        self.assertEqual([(t[2], t[3]) for t in tasks], [("One", 2), ("Two", 4)])
        flipped = rt.toggle_task_line(md, tasks[1][3], True)
        self.assertEqual(flipped, "## Action items\n\n- [ ] One\nnote\n- [x] Two\n")

    def test_line_index_after_code_fence(self):
        md = "```\n- [ ] not a task\n```\n- [ ] real"
        self.assertEqual(rt.parse_blocks(md), [("code", "", "- [ ] not a task"), ("task", False, "real", 3)])

    def test_unclosed_fence_runs_to_the_end(self):
        self.assertEqual(rt.parse_blocks("```\na\nb"), [("code", "", "a\nb")])

    def test_parse_notes_is_the_old_shape(self):
        blocks = rt.parse_notes("## Summary\nOne.\nTwo.\n\n**Decisions:**\n- A\n- [x] B (Owner: Anna)\n> note")
        self.assertEqual(blocks, [("head", "Summary"), ("para", "One. Two."), ("head", "Decisions"),
                                  ("bullet", "A"), ("task", (True, "B (Owner: Anna)", 6)), ("quote", "note")])
        self.assertEqual(rt.parse_notes("1. one"), [("bullet", "one")])

    def test_note_helpers(self):
        self.assertEqual(rt.parse_task("`Fix drift` (Owner: David, Due: Wednesday)"),
                         ("Fix drift", "David", "by Wednesday"))
        self.assertEqual(rt.parse_task("Send copy (Owner: -)"), ("Send copy", "", ""))
        self.assertEqual(rt.notes_plain("## S\n- [ ] Do (Owner: Al)"), ["S", "☐ Do (Al)"])
        self.assertEqual(rt.toggle_task_line("- [ ] a\n- [ ] b", 1, True), "- [ ] a\n- [x] b")


class TestInline(unittest.TestCase):
    def test_inline_html(self):
        self.assertEqual(rt.inline_html("**a** <b>"), "<b>a</b> &lt;b&gt;")
        self.assertEqual(rt.inline_html("*i* and _j_ and `k`"), "<i>i</i> and <i>j</i> and k")
        self.assertEqual(rt.inline_html("`k`", code=True), "<code>k</code>")
        self.assertEqual(rt.inline_html("snake_case_name"), "snake_case_name")

    def test_strip_inline(self):
        self.assertEqual(rt.strip_inline("  **a** *b* _c_ `d`  "), "a b c d")
        self.assertEqual(rt.strip_inline("5 * 3 * 2"), "5 * 3 * 2")
        self.assertEqual(rt.strip_inline(None), "")


class TestTranscriptParagraphs(unittest.TestCase):
    def test_short_lines_untouched(self):
        text = "Speaker 1: Hello team.\n\n— Resumed at 11:20 —\nYou: Hi."
        self.assertEqual(rt.transcript_paragraphs(text),
                         ["Speaker 1: Hello team.", "— Resumed at 11:20 —", "You: Hi."])

    def test_wall_of_text_is_packed(self):
        sentence = "This is a sentence about the plan for the beta release. "
        text = (sentence * 40).strip()
        paras = rt.transcript_paragraphs(text, max_chars=550)
        self.assertGreater(len(paras), 3)
        for p in paras:
            self.assertLessEqual(len(p), 550)
            self.assertTrue(p.endswith("."), p)
        self.assertEqual(" ".join(paras), text)

    def test_speaker_prefix_stays_on_the_first_paragraph(self):
        body = " ".join("Sentence number %d is here." % i for i in range(60))
        paras = rt.transcript_paragraphs("Speaker 2: " + body, max_chars=200)
        self.assertTrue(paras[0].startswith("Speaker 2: Sentence number 0"))
        self.assertTrue(all(not p.startswith("Speaker") for p in paras[1:]))
        self.assertEqual(" ".join(paras), "Speaker 2: " + body)

    def test_markers_never_packed(self):
        marker = "— " + "x" * 700 + " —"
        self.assertEqual(rt.transcript_paragraphs(marker), [marker])

    def test_armenian_full_stop(self):
        sent = "Սա հայերեն նախադասություն է, որը բավական երկար է։ "
        text = (sent * 30).strip()
        paras = rt.transcript_paragraphs(text, max_chars=300)
        self.assertGreater(len(paras), 1)
        self.assertTrue(all(p.endswith("։") for p in paras))
        self.assertEqual(" ".join(paras), text)

    def test_other_enders(self):
        text = ("真的吗？" * 200)
        paras = rt.transcript_paragraphs(text, max_chars=100)
        self.assertTrue(all(len(p) <= 100 and p.endswith("？") for p in paras))
        self.assertEqual("".join(paras), text)
        text = ("Is it done؟ " * 100).strip()
        self.assertTrue(all(p.endswith("؟") for p in rt.transcript_paragraphs(text, max_chars=120)))

    def test_no_punctuation_falls_back_to_words(self):
        text = " ".join("word%d" % i for i in range(400))
        paras = rt.transcript_paragraphs(text, max_chars=200)
        self.assertGreater(len(paras), 5)
        self.assertTrue(all(len(p) <= 200 for p in paras))
        self.assertEqual(" ".join(paras), text)

    def test_one_huge_word_is_cut(self):
        text = "x" * 1300
        paras = rt.transcript_paragraphs(text, max_chars=550)
        self.assertEqual([len(p) for p in paras], [550, 550, 200])

    def test_empty(self):
        self.assertEqual(rt.transcript_paragraphs(""), [])
        self.assertEqual(rt.transcript_paragraphs(None), [])
        self.assertEqual(rt.transcript_paragraphs("\n \n"), [])

    def test_split_speaker(self):
        self.assertEqual(rt.split_speaker("Speaker 1: Hi"), ("Speaker 1", "Hi"))
        self.assertEqual(rt.split_speaker("just text"), ("", "just text"))
        self.assertTrue(rt.is_marker("— The recording stopped here —"))
        self.assertFalse(rt.is_marker("— not a marker"))


if __name__ == "__main__":
    unittest.main()
