"""Meeting notes in rich_text: who an action item belongs to (split_task / parse_task / clean_notes), topics and empty sections
(heading_roles / parse_notes(sections=True)), and the headings of a topic in the clipboard HTML. Pure text, no Qt."""
import time
import unittest

import rich_text as rt


class TestSplitTask(unittest.TestCase):
    CASES = [
        ("Send the file (Owner: Them, Due: )", ("Send the file", "", "")),
        ("Send the file (Owner: , Due: )", ("Send the file", "", "")),
        ("Send the file (Owner: , Due: Friday)", ("Send the file", "", "Friday")),
        ("Send the file (Owner: Aram Due: Friday)", ("Send the file", "Aram", "Friday")),
        ("Send the file (Due: Friday, Owner: Aram)", ("Send the file", "Aram", "Friday")),
        ("Send the file (Owner: Aram, Due: Friday, March 3)", ("Send the file", "Aram", "Friday, March 3")),
        ("Send the file (Owner: [blank], Due: [blank])", ("Send the file", "", "")),
        ("Send the file (Owner: unknown, Due: not specified)", ("Send the file", "", "")),
        ("Send the file (Owner: <name>, Due: <date>)", ("Send the file", "", "")),
        ("Send the file (Owner: TBD, Due: n/a)", ("Send the file", "", "")),
        ("Send the file (Owner: Speaker 1, Due: next week)", ("Send the file", "", "next week")),
        ("Send the file (Owner: Speaker ?, Due: next week)", ("Send the file", "", "next week")),
        ("Send the file (Owner: You, Due:)", ("Send the file", "You", "")),
        ("Send the file (Owner: Them / Speaker 2)", ("Send the file", "", "")),
        ("Send the file (Owner: the other side)", ("Send the file", "", "")),
        ("Send the file (Owner: everyone, Due: Friday)", ("Send the file", "", "Friday")),
        ("Send the file (Owner: Them and Speaker 2)", ("Send the file", "", "")),
        ("Send the file - Owner: , Due: ", ("Send the file", "", "")),
        ("Send the file. Owner: Them. Due: ", ("Send the file", "", "")),
        ("Send the file - Owner: Aram, Due: Friday", ("Send the file", "Aram", "Friday")),
        ("**Send the file** (Owner: , Due: )", ("**Send the file**", "", "")),
        ("Send the file (Owner: the interviewer, Due: )", ("Send the file", "", "")),
        ("Send the file (owner: Aram, due: Friday)", ("Send the file", "Aram", "Friday")),
        ("Envoyer le fichier (Responsable : Aram, Échéance : vendredi)", ("Envoyer le fichier", "Aram", "vendredi")),
        ("Envoyer le fichier (Owner: Aram, Due: vendredi)", ("Envoyer le fichier", "Aram", "vendredi")),
        ("Envoyer le fichier (Responsable : Eux, Échéance : )", ("Envoyer le fichier", "", "")),
        ("Ask Claire (the lead) to review (Owner: Marc and Nadia, Due: Mon)",
         ("Ask Claire (the lead) to review", "Marc, Nadia", "Mon")),
        ("`Fix drift` (Owner: David, Due: Wednesday)", ("Fix drift", "David", "Wednesday")),
        ("Send copy (Owner: -)", ("Send copy", "", "")),
        ("Plan the trip (Due: Friday)", ("Plan the trip", "", "Friday")),
        ("Send the file (Assigned to: Marc)", ("Send the file", "Marc", "")),
        ("Send the file (Deadline: 1 May, Assignee: Sam)", ("Send the file", "Sam", "1 May")),
        ("Call Anne-Marie (Owner: Anne-Marie and Jean Pierre, Due: Mon)",
         ("Call Anne-Marie", "Anne-Marie, Jean Pierre", "Mon")),
        ("Do it (Owner: **Aram**, Due: `Fri`)", ("Do it", "Aram", "Fri")),
        ("Do it (Owner: Aram (the host), Due: Fri)", ("Do it", "Aram (the host)", "Fri")),
        ("Do it (Owner: Aram, Due: Fri) because the client asked", ("Do it because the client asked", "Aram", "Fri")),
        ("(Owner: Aram)", ("", "Aram", "")),
        # what is NOT an owner or a due: the text stays as it is
        ("Remember the owner: it is Claire's call", ("Remember the owner: it is Claire's call", "", "")),
        ("Pay the invoice due: next month", ("Pay the invoice due: next month", "", "")),
        ("Call Anna (before the meeting)", ("Call Anna (before the meeting)", "", "")),
        ("Pay (invoice due: 5 May)", ("Pay (invoice due: 5 May)", "", "")),
        ("Sara: write the announcement", ("Sara: write the announcement", "", "")),
        ("", ("", "", "")),
    ]

    def test_every_shape_a_model_writes_it_in(self):
        for text, want in self.CASES:
            self.assertEqual(rt.split_task(text), want, text)

    def test_none_is_nothing(self):
        self.assertEqual(rt.split_task(None), ("", "", ""))

    def test_the_due_is_as_written_and_parse_task_adds_by_to_a_bare_date_only(self):
        self.assertEqual(rt.parse_task("Do it (Owner: Aram, Due: Friday)"), ("Do it", "Aram", "by Friday"))
        self.assertEqual(rt.parse_task("Do it (Due: 12 March.)"), ("Do it", "", "by 12 March"))
        for due in ("within a week", "on Friday", "ASAP", "before the review", "by Friday", "until 5 May", "in two days",
                    "after the launch", "avant vendredi", "EOD", "week of Nov 3"):
            self.assertEqual(rt.parse_task("Do it (Due: %s)" % due)[2], due, due)

    def test_by_goes_before_a_date_that_reads_as_english(self):
        for due in ("Wed", "Friday", "Friday, March 3", "March 3", "15 November", "5th November", "1st of May", "3pm",
                    "2026-11-25", "12/03", "next week", "this week", "tomorrow", "end of November", "mid December", "Q4",
                    "the 15th", "Mon 5 Nov"):
            self.assertEqual(rt.parse_task("Do it (Due: %s)" % due)[2], "by " + due, due)

    def test_a_due_in_french_is_not_given_an_english_by(self):
        for due in ("vendredi", "mardi 3 mars", "15 novembre", "la semaine prochaine", "demain", "fin novembre"):
            self.assertEqual(rt.parse_task("Faire le point (Échéance : %s)" % due)[2], due, due)

    def test_parse_task_keeps_its_old_contract(self):
        self.assertEqual(rt.parse_task("`Fix drift` (Owner: David, Due: Wednesday)"), ("Fix drift", "David", "by Wednesday"))
        self.assertEqual(rt.parse_task("Send copy (Owner: -)"), ("Send copy", "", ""))
        self.assertEqual(rt.parse_task("Ship it (Due: Fri)"), ("Ship it", "", "by Fri"))
        self.assertEqual(rt.parse_task("Call David"), ("Call David", "", ""))

    def test_a_generic_owner_never_becomes_a_person(self):
        for who in ("Them", "them", "THEM", "Speaker 1", "Speaker 12", "speaker ?", "Participant 2", "the other side",
                    "The other party", "someone", "Everyone", "all", "we", "the team", "Team", "the speaker",
                    "the interviewer", "les autres", "tout le monde", "", "-", "none", "N/A", "[blank]", "unknown"):
            self.assertTrue(rt.owner_is_generic(who), who)
            self.assertEqual(rt.clean_owner(who), "", who)

    def test_a_person_is_a_person(self):
        for who in ("Aram", "You", "Marc", "Anne-Marie", "Jean Pierre", "Océane", "李伟", "Sara O'Neil", "Dr. Who"):
            self.assertFalse(rt.owner_is_generic(who), who)
            self.assertEqual(rt.clean_owner(who), who)

    def test_several_owners_are_listed_and_the_generic_ones_dropped(self):
        self.assertEqual(rt.clean_owner("Marc and Nadia"), "Marc, Nadia")
        self.assertEqual(rt.clean_owner("Marc / Them"), "Marc")
        self.assertEqual(rt.clean_owner("Marc, marc"), "Marc, marc")
        self.assertEqual(rt.owner_names("Marc et Nadia"), ["Marc", "Nadia"])
        self.assertEqual(rt.owner_names("Them / Speaker 2"), [])

    def test_a_long_run_of_the_same_character_is_read_at_once(self):
        t0 = time.time()
        for s in ("a" + " " * 60000 + "b", "(" * 60000, ")" * 60000, "x (Owner: " + "." * 60000 + ")",
                  "x (Owner: a, Due: " + "(" * 20000 + ")", "x " + "Owner: " * 20000, "x (" + "Due: a, " * 20000 + ")",
                  "x - " + "Owner: , Due: " * 8000):
            rt.split_task(s)
            rt.parse_task(s)
        self.assertLess(time.time() - t0, 5.0)

    def test_an_endless_group_is_prose_and_left_alone(self):
        long = "x (Owner: " + "a" * 800 + ")"
        self.assertEqual(rt.split_task(long)[0], long)


class TestCleanNotes(unittest.TestCase):
    RAW = ("## Summary\nA.\n\n## Key decisions\n- None.\n\n## Action items\n"
           "- [ ] Send the file (Owner: Them, Due: )\n- [x] Book (Owner: You, Due: Friday)\n"
           "1. [ ] Numbered (Owner: Speaker 2)\n- [ ] Both (Owner: Marc and You, Due: within a week)\n"
           "- [ ] Ask (Owner: Aram Due: Friday)\n- [ ] No parts (Owner: , Due: )\n- [ ] Plain\n\n"
           "## Open questions\n_None._\n")

    def test_every_action_item_in_one_shape_and_no_empty_sections(self):
        out = rt.clean_notes(self.RAW, me="Aram")
        self.assertEqual(out, (
            "## Summary\nA.\n\n## Action items\n"
            "- [ ] Send the file\n- [x] Book (Owner: Aram, Due: Friday)\n"
            "1. [ ] Numbered\n- [ ] Both (Owner: Marc, Aram, Due: within a week)\n"
            "- [ ] Ask (Owner: Aram, Due: Friday)\n- [ ] No parts\n- [ ] Plain\n"))

    def test_you_stays_you_when_the_name_is_not_known(self):
        out = rt.clean_notes(self.RAW)
        self.assertIn("- [x] Book (Owner: You, Due: Friday)", out)
        self.assertIn("(Owner: Marc, You, Due: within a week)", out)

    def test_cleaning_twice_changes_nothing(self):
        once = rt.clean_notes(self.RAW, me="Aram")
        self.assertEqual(rt.clean_notes(once, me="Aram"), once)

    def test_clean_notes_come_back_as_they_are(self):
        md = "## Summary\nA.\n\n## Action items\n- [ ] Do it (Owner: Aram, Due: Friday)\n- [x] Done\n"
        self.assertEqual(rt.clean_notes(md, me="Aram"), md)
        self.assertEqual(rt.clean_notes(md), md)

    def test_a_ticked_box_stays_ticked_and_the_marker_stays(self):
        out = rt.clean_notes("* [x] Done (Owner: Them)\n- [X] Also (Owner: , Due: )\n2) [ ] Third (Owner: -)\n")
        self.assertEqual(out, "* [x] Done\n- [X] Also\n2) [ ] Third\n")

    def test_line_endings_stay(self):
        out = rt.clean_notes("## A\r\n- [ ] Do it (Owner: Them, Due: )\r\n\r\n## B\r\n- None\r\n")
        self.assertEqual(out, "## A\r\n- [ ] Do it\r\n")
        out = rt.clean_notes("## A\r\n- [ ] x (Owner: Them)\r\n- [ ] y (Owner: Al)\r\n")
        self.assertEqual(out, "## A\r\n- [ ] x\r\n- [ ] y (Owner: Al)\r\n")

    def test_no_final_newline_stays_none(self):
        self.assertEqual(rt.clean_notes("## A\n- [ ] x (Owner: Them)"), "## A\n- [ ] x")
        self.assertEqual(rt.clean_notes("## A\n- x\n\n## B\n- None."), "## A\n- x")

    def test_empty_input(self):
        self.assertEqual(rt.clean_notes(""), "")
        self.assertEqual(rt.clean_notes(None), "")
        self.assertEqual(rt.clean_notes("   "), "   ")

    def test_a_section_with_only_none_goes_in_every_spelling(self):
        for none in ("None.", "None", "- None", "- None.", "_None._", "**None**", "N/A", "Nothing.", "No open questions.",
                     "No action items were identified.", "No key decisions were made.", "Aucune.", "Rien"):
            md = "## Summary\nA.\n\n## Open questions\n%s\n" % none
            self.assertEqual(rt.clean_notes(md), "## Summary\nA.\n", none)

    def test_a_section_with_more_than_a_none_line_stays(self):
        md = "## Summary\nA.\n\n## Open questions\n- None.\n- Is the price right?\n"
        self.assertEqual(rt.clean_notes(md), md)
        md = "## Summary\nA.\n\n## Open questions\nNone of this was settled.\n"
        self.assertEqual(rt.clean_notes(md), md)

    def test_an_empty_topic_goes_and_so_does_a_discussion_left_with_nothing(self):
        md = "## Summary\nA.\n\n## Discussion\n### One\n\n### Two\n"
        self.assertEqual(rt.clean_notes(md), "## Summary\nA.\n")
        md = "## Summary\nA.\n\n## Discussion\n### One\n- a\n\n### Two\n\n### Three\n- c\n"
        self.assertEqual(rt.clean_notes(md), "## Summary\nA.\n\n## Discussion\n### One\n- a\n\n### Three\n- c\n")

    def test_a_title_above_its_sections_stays(self):
        md = "# Notes\n## Summary\nA.\n"
        self.assertEqual(rt.clean_notes(md), md)

    def test_code_is_left_alone(self):
        md = "## A\n- x\n\n```\n## not a heading\n- [ ] not a task (Owner: Them)\n```\n"
        self.assertEqual(rt.clean_notes(md), md)

    def test_a_task_makes_a_section_real(self):
        md = "## Action items\n- [ ] Do it (Owner: Them)\n"
        self.assertEqual(rt.clean_notes(md), "## Action items\n- [ ] Do it\n")

    def test_hostile_text_is_cleaned_at_once(self):
        t0 = time.time()
        rt.clean_notes("## A\n" + "- [ ] x (Owner: Them)\n" * 20000)
        rt.clean_notes("".join("## h%d\n" % i for i in range(20000)))
        rt.clean_notes("#" * 100000 + " x\n" + "- [ ] " + " " * 100000 + "y\n")
        self.assertLess(time.time() - t0, 8.0)


class TestSectionsAndTopics(unittest.TestCase):
    NOTES = ("## Summary\nA.\n\nB.\n\n## Key decisions\n\n## Action items\n- [ ] x (Owner: Al)\n\n## Open questions\n- _None._\n\n"
             "## Discussion\n### Topic one\n- a\n- b\n\n### Empty topic\n\n### Topic two\n- c\n")

    def test_a_topic_under_a_section_is_a_sub_heading(self):
        blocks = rt.parse_blocks("# Title\n## Section\n### Topic\n#### Deeper\n**Label:**\ntext")
        self.assertEqual(rt.heading_roles(blocks), {0: "section", 1: "section", 2: "sub", 3: "sub", 4: "sub"})

    def test_notes_with_only_deep_headings_keep_them_as_sections(self):
        blocks = rt.parse_blocks("### Summary\ntext\n### Action items\n- [ ] x")
        self.assertEqual(rt.heading_roles(blocks), {0: "section", 2: "section"})
        self.assertEqual(rt.parse_notes("### Summary\ntext\n### Action items\n- [ ] x", sections=True)[0], ("head", "Summary"))

    def test_the_default_parse_is_unchanged(self):
        blocks = rt.parse_notes(self.NOTES)
        self.assertIn(("head", "Key decisions"), blocks)
        self.assertIn(("head", "Topic one"), blocks)
        self.assertIn(("bullet", "_None._"), blocks)
        self.assertNotIn("subhead", [k for k, _ in blocks])

    def test_the_notes_view_gets_topics_and_no_empty_section(self):
        blocks = rt.parse_notes(self.NOTES, sections=True)
        self.assertEqual(blocks, [
            ("head", "Summary"), ("para", "A."), ("para", "B."),
            ("head", "Action items"), ("task", (False, "x (Owner: Al)", 8)),
            ("head", "Discussion"), ("subhead", "Topic one"), ("bullet", "a"), ("bullet", "b"),
            ("subhead", "Topic two"), ("bullet", "c")])

    def test_a_task_keeps_its_own_line_of_the_raw_notes(self):
        md = "## Summary\nA.\n\n## Key decisions\n\n## Action items\n- [ ] x\n- [x] y\n"
        tasks = [d for k, d in rt.parse_notes(md, sections=True) if k == "task"]
        lines = md.split("\n")
        for done, text, idx in tasks:
            self.assertIn(text, lines[idx])
        self.assertEqual(rt.toggle_task_line(md, tasks[0][2], True).split("\n")[tasks[0][2]], "- [x] x")

    def test_no_notes(self):
        self.assertEqual(rt.parse_notes("", sections=True), [])
        self.assertEqual(rt.parse_notes("## Only\n", sections=True), [])

    def test_the_default_does_not_drop_anything(self):
        self.assertEqual(rt.parse_notes("## Only\n"), [("head", "Only")])


class TestTopicsInTheClipboardHtml(unittest.TestCase):
    MD = "## Discussion\n### Topic\n- a\n\n**Label:**\n- b"

    def test_a_topic_is_an_h4_under_an_h3(self):
        html = rt.to_html(self.MD)
        self.assertIn("<h3>Discussion</h3>", html)
        self.assertIn("<h4>Topic</h4>", html)
        self.assertIn("<p><b>Label</b></p>", html)

    def test_notes_without_a_section_keep_h3(self):
        self.assertIn("<h3>Topic</h3>", rt.to_html("### Topic\n- a"))
        self.assertIn("<h3>Plan</h3>", rt.to_html("## Plan\n- a"))

    def test_the_in_app_html_makes_a_topic_a_bold_line(self):
        html = rt.qt_html(self.MD)
        self.assertIn("<h3", html)
        self.assertIn("<b>Topic</b>", html)
        self.assertEqual(html.count("<h3"), 1)

    def test_plain_text_keeps_a_topic_on_its_own_line(self):
        plain = rt.to_plain(self.MD)
        self.assertIn("Discussion\nTopic\n", plain.replace("\n\nTopic", "\nTopic"))
        self.assertNotIn("#", plain)

    def test_an_owner_that_is_no_person_is_not_printed_in_copy(self):
        md = "## Action items\n- [ ] Send it (Owner: Them, Due: )\n- [ ] Call (Owner: , Due: Friday)"
        self.assertIn("☐ Send it", rt.to_plain(md))
        self.assertNotIn("Them", rt.to_plain(md))
        self.assertNotIn("Owner", rt.to_plain(md))
        self.assertNotIn("Them", rt.to_html(md))
        self.assertIn("☐ Call (by Friday)", rt.to_plain(md))


class TestTheShareHelpersReadTheNewShape(unittest.TestCase):
    """meeting_share (the plain-text share formats) copes with a Discussion: no # left, topics on their own line."""
    MD = "## Summary\nA.\n\n## Discussion\n### Release\n- It **slips**.\n"

    def test_strip_markdown(self):
        import meeting_share
        out = meeting_share.strip_markdown(self.MD)
        self.assertNotIn("#", out)
        self.assertNotIn("**", out)
        self.assertIn("Release\n• It slips.", out)

    def test_the_slack_and_email_formats(self):
        import meeting_share
        slack = meeting_share.format_share("slack", self.MD, "Sync")
        self.assertIn("*Release*", slack)
        self.assertNotIn("###", slack)
        email = meeting_share.format_share("email", self.MD, "Sync")
        self.assertNotIn("#", email)


class TestInlineRuns(unittest.TestCase):
    def test_it_lives_in_rich_text_and_docx_export_keeps_the_name(self):
        self.assertEqual(rt.inline_runs("**Fix** the *drift*"),
                         [("Fix", True, False), (" the ", False, False), ("drift", False, True)])
        import docx_export
        self.assertIs(docx_export.inline_runs, rt.inline_runs)

    def test_markup_is_text(self):
        self.assertEqual(rt.inline_runs("<b>not a tag</b>"), [("<b>not a tag</b>", False, False)])


if __name__ == "__main__":
    unittest.main()
