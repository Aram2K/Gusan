"""Meeting names: what is a placeholder, what a model's answer becomes, how a list groups and dates meetings."""
import json
import os
import sys
import tempfile
import threading
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import action_api  # noqa: E402
import actions  # noqa: E402
import meeting_store  # noqa: E402
import meeting_titles as mt  # noqa: E402


class TestPlaceholders(unittest.TestCase):
    def test_the_apps_own_names_are_placeholders(self):
        for title in ("", "   ", "Untitled Meeting", "untitled", "Meeting", "Meeting · Tue 14:30",
                      "Meeting · mar. 14:30", "Meeting · 9:05", "Live session 14:30", "Live session",
                      "Live Assistant 09:00", "Recording 11:20"):
            self.assertTrue(mt.is_placeholder(title), title)

    def test_a_real_name_is_not(self):
        for title in ("Weekly sync", "Meeting with Anna", "Q4 roadmap", "Interview · backend role",
                      "Live session retro", "Meeting notes for the board"):
            self.assertFalse(mt.is_placeholder(title), title)

    def test_only_an_unnamed_meeting_wants_a_title(self):
        self.assertTrue(mt.wants_title({"title": "Meeting · Tue 14:30"}))
        self.assertTrue(mt.wants_title({}))
        self.assertFalse(mt.wants_title({"title": "Weekly sync"}))
        # named by the AI already, the person, or the calendar: never again
        for source in ("auto", "user", "calendar"):
            self.assertFalse(mt.wants_title({"title": "Untitled Meeting", "title_source": source}))
        self.assertFalse(mt.wants_title({"title": "Untitled Meeting", "title_tried": True}))


class TestCleanTitle(unittest.TestCase):
    def test_a_plain_answer_stays(self):
        self.assertEqual(mt.clean_title("Q3 budget review and hiring plan"), "Q3 budget review and hiring plan")

    def test_label_quotes_markdown_and_full_stop_go(self):
        self.assertEqual(mt.clean_title('Title: "Hiring interview for the backend role."'),
                         "Hiring interview for the backend role")
        self.assertEqual(mt.clean_title("**Product roadmap planning**"), "Product roadmap planning")
        self.assertEqual(mt.clean_title("# Weekly sync with Anna"), "Weekly sync with Anna")
        self.assertEqual(mt.clean_title("“Réunion budget trimestriel”"), "Réunion budget trimestriel")

    def test_an_intro_line_is_skipped_and_only_the_first_line_is_kept(self):
        self.assertEqual(mt.clean_title("Here is a title:\nLaunch plan for the new site\nBecause it was the topic"),
                         "Launch plan for the new site")

    def test_too_long_is_cut_at_a_word_and_never_ends_on_a_connector(self):
        out = mt.clean_title("one two three four five six seven eight nine of the end of it all")
        self.assertLessEqual(len(out.split()), mt.TITLE_MAX_WORDS)
        self.assertEqual(mt.clean_title("Planning the launch of the"), "Planning the launch")

    def test_empty_labels_and_placeholders_are_not_titles(self):
        for raw in ("", "   ", "Summary", "Meeting notes", "Untitled Meeting", "Meeting · Tue 14:30", "None", "ok"):
            self.assertEqual(mt.clean_title(raw), "", raw)


class TestDisplay(unittest.TestCase):
    def test_a_real_title_is_shown_as_it_is(self):
        self.assertEqual(mt.display_title({"title": "Weekly sync", "preview": "Whatever the notes say."}), "Weekly sync")

    def test_a_placeholder_is_shown_as_a_name_from_the_notes(self):
        m = {"title": "Meeting · Tue 14:30",
             "preview": "The team discussed the candidate's background in detail and agreed on next steps."}
        self.assertEqual(mt.display_title(m), "The team discussed the candidate's background in detail\u2026")

    def test_a_short_first_sentence_is_taken_whole(self):
        self.assertEqual(mt.display_title({"title": "Untitled Meeting", "preview": "Demo moves to Thursday. More."}),
                         "Demo moves to Thursday")

    def test_nothing_to_go_on_is_untitled(self):
        self.assertEqual(mt.display_title({"title": "Meeting · Tue 14:30"}), "Untitled Meeting")
        self.assertEqual(mt.display_title({}), "Untitled Meeting")


NOW = datetime(2026, 11, 7, 18, 0)          # a Saturday


class TestDates(unittest.TestCase):
    def group(self, text):
        return mt.day_group(mt.parse_started(text), NOW)

    def test_groups(self):
        self.assertEqual(self.group("2026-11-07 09:00:00"), ("today", "Today"))
        self.assertEqual(self.group("2026-11-06 09:00:00"), ("yesterday", "Yesterday"))
        self.assertEqual(self.group("2026-11-03 09:00:00"), ("week", "Earlier this week"))     # Tuesday
        self.assertEqual(self.group("2026-10-29 09:00:00"), ("lastweek", "Last week"))         # Thursday before
        self.assertEqual(self.group("2026-10-20 09:00:00"), ("m202610", "October"))
        self.assertEqual(self.group("2025-12-31 09:00:00"), ("m202512", "December 2025"))
        self.assertEqual(self.group("garbage"), ("unknown", "Earlier"))

    def test_a_future_start_counts_as_today(self):
        self.assertEqual(self.group("2026-11-08 09:00:00")[0], "today")

    def test_row_dates_under_a_header_say_only_what_the_header_does_not(self):
        def when(text, grouped=True):
            return mt.when_text(mt.parse_started(text), NOW, grouped)
        self.assertEqual(when("2026-11-07 16:32:00"), "16:32")
        self.assertEqual(when("2026-11-06 09:05:00"), "09:05")
        self.assertEqual(when("2026-11-03 14:30:00"), "Tue 14:30")
        self.assertEqual(when("2026-10-29 14:30:00"), "Thu 14:30")                             # (last week)
        self.assertEqual(when("2026-10-20 14:30:00"), "Tue 20 Oct \u00b7 14:30")

    def test_row_dates_without_headers_say_it_all(self):
        def when(text):
            return mt.when_text(mt.parse_started(text), NOW, False)
        self.assertEqual(when("2026-11-07 16:32:00"), "Today \u00b7 16:32")
        self.assertEqual(when("2026-11-06 09:05:00"), "Yesterday \u00b7 09:05")
        self.assertEqual(when("2026-11-03 14:30:00"), "Tue 3 Nov \u00b7 14:30")

    def test_sections_keep_the_order_and_split_at_each_change_of_group(self):
        ms = [{"started": s} for s in ("2026-11-07 16:00:00", "2026-11-07 09:00:00", "2026-11-06 20:00:00",
                                       "2026-11-03 10:00:00", "2026-10-20 10:00:00", "2026-10-02 10:00:00")]
        out = mt.sections(ms, NOW)
        self.assertEqual([(label, len(items)) for label, items in out],
                         [("Today", 2), ("Yesterday", 1), ("Earlier this week", 1), ("October", 2)])


class TestStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name) / "20261107_143000"
        self.folder.mkdir()
        (self.folder / "transcript.txt").write_text("hello", encoding="utf-8")

    def meta(self):
        return json.loads((self.folder / "meta.json").read_text(encoding="utf-8"))

    def test_the_ai_names_an_unnamed_meeting(self):
        meeting_store.update_meta(self.folder, title="Meeting · Tue 14:30")
        self.assertTrue(meeting_store.set_auto_title(self.folder, '"Hiring plan for Q4."'))
        m = self.meta()
        self.assertEqual((m["title"], m["title_source"]), ("Hiring plan for Q4", "auto"))

    def test_the_ai_never_replaces_what_a_person_wrote(self):
        meeting_store.update_meta(self.folder, title="Meeting · Tue 14:30")
        self.assertTrue(meeting_store.set_title(self.folder, "Board prep"))
        self.assertFalse(meeting_store.set_auto_title(self.folder, "Something else entirely"))
        self.assertEqual((self.meta()["title"], self.meta()["title_source"]), ("Board prep", "user"))

    def test_the_ai_never_replaces_a_title_the_start_card_or_the_calendar_gave(self):
        meeting_store.update_meta(self.folder, title="Weekly sync")
        self.assertFalse(meeting_store.set_auto_title(self.folder, "Something else entirely"))
        self.assertEqual(self.meta()["title"], "Weekly sync")

    def test_a_title_the_ai_wrote_can_still_be_renamed_and_blanking_it_lets_the_ai_try_again(self):
        meeting_store.update_meta(self.folder, title="Meeting · Tue 14:30")
        meeting_store.set_auto_title(self.folder, "Hiring plan for Q4")
        self.assertTrue(meeting_store.set_title(self.folder, "Hiring"))
        self.assertEqual(self.meta()["title_source"], "user")
        self.assertTrue(meeting_store.set_title(self.folder, "   "))
        m = self.meta()
        self.assertEqual(m["title"], "Untitled Meeting")
        self.assertNotIn("title_source", m)
        self.assertTrue(meeting_titles_wants(m))

    def test_a_bad_answer_is_not_saved(self):
        meeting_store.update_meta(self.folder, title="Meeting · Tue 14:30")
        self.assertFalse(meeting_store.set_auto_title(self.folder, "Summary"))
        self.assertEqual(self.meta()["title"], "Meeting · Tue 14:30")

    def test_a_try_that_came_back_empty_is_remembered(self):
        meeting_store.update_meta(self.folder, title="Untitled Meeting")
        self.assertTrue(meeting_store.mark_title_tried(self.folder))
        self.assertFalse(meeting_titles_wants(self.meta()))

    def test_list_meetings_says_who_named_it_and_can_add_the_first_notes_line(self):
        meeting_store.update_meta(self.folder, title="Meeting · Tue 14:30", title_source="auto", title_tried=True)
        (self.folder / "notes.md").write_text("## Summary\nThe team agreed to ship on Thursday. More.\n\n- x\n",
                                              encoding="utf-8")
        with mock.patch.object(meeting_store, "meetings_dir", return_value=Path(self.tmp.name)):
            plain = meeting_store.list_meetings()
            rich = meeting_store.list_meetings(preview=True)
        self.assertEqual((plain[0]["title_source"], plain[0]["title_tried"]), ("auto", True))
        self.assertNotIn("preview", plain[0])
        self.assertTrue(rich[0]["preview"].startswith("The team agreed to ship on Thursday"))


def meeting_titles_wants(meta):
    return mt.wants_title(meta)


class TestTheModelCall(unittest.TestCase):
    def test_the_prompt_is_the_titles_own(self):
        msgs = action_api.build_messages("## Summary\nWe planned the launch.", "meeting_title")
        text = "\n".join(m["content"] for m in msgs)
        self.assertIn(mt.TITLE_INSTRUCTION, text)
        self.assertIn("We planned the launch.", text)

    def test_the_server_sees_a_meeting_note_request_so_a_free_persons_note_covers_it(self):
        self.assertEqual(action_api.server_mode("meeting_title"), "meeting_notes")
        self.assertEqual(action_api.server_mode("live_rule"), "live_assist")
        self.assertEqual(action_api.server_mode("summarize"), "summarize")

    def test_the_answer_is_short(self):
        self.assertLessEqual(action_api._max_tokens_for("meeting_title"), 200)

    def test_the_rule_based_formatter_never_makes_a_title_up(self):
        self.assertEqual(actions.process("Some long notes", actions.ACTION_MEETING_TITLE, model=actions.RULE_BASED_ID), "")
        self.assertEqual(actions.normalize_action_mode("meeting_title"), actions.ACTION_MEETING_TITLE)

    def test_privacy_mode_never_sends_notes_out_for_a_title(self):
        with mock.patch.object(action_api, "run_managed_action", side_effect=AssertionError("sent")):
            out = actions.process("notes", actions.ACTION_MEETING_TITLE, model=actions.API_MANAGED_ID,
                                  config={"privacy_mode": True, "_managed_token": "t"})
        self.assertEqual(out, "")

    def test_gusan_cloud_gets_the_title_request_with_the_notes_note_ref(self):
        seen = {}

        def fake(text, mode, token, **kw):
            seen.update(mode=mode, token=token, note_ref=kw.get("note_ref"))
            return "Launch plan"
        with mock.patch.object(action_api, "run_managed_action", fake):
            out = actions.process("notes", actions.ACTION_MEETING_TITLE, model=actions.API_MANAGED_ID,
                                  config={"_managed_token": "tok", "_note_ref": "C:/m/1"})
        self.assertEqual(out, "Launch plan")
        self.assertEqual(seen, {"mode": "meeting_title", "token": "tok", "note_ref": "C:/m/1"})


class TestNamingFromTheSession(unittest.TestCase):
    """meeting_session._name_meeting: the call that names a meeting right after its notes are written."""

    def make(self, title="Meeting · Tue 14:30"):
        import meeting_session
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        folder = Path(tmp.name) / "20261107_143000"
        folder.mkdir()
        meeting_store.update_meta(folder, title=title)
        s = meeting_session.MeetingSession.__new__(meeting_session.MeetingSession)
        s._meeting_dir = folder
        s._meeting_title = title
        s._final_lang = "en"
        return meeting_session, s, folder

    def meta(self, folder):
        return json.loads((folder / "meta.json").read_text(encoding="utf-8"))

    def test_an_unnamed_meeting_is_named_from_its_notes(self):
        ms, s, folder = self.make()
        calls = []

        def fake(text, mode, **kw):
            calls.append((mode, kw.get("model")))
            return '"Launch plan for the new site."'
        with mock.patch.object(ms.actions, "process", fake):
            s._name_meeting("## Summary\nWe planned the launch.", "managed", {"_note_ref": "x"}, "managed")
        self.assertEqual(calls, [("meeting_title", "managed")])
        self.assertEqual(self.meta(folder)["title"], "Launch plan for the new site")
        self.assertEqual(s._meeting_title, "Launch plan for the new site")

    def test_no_ai_no_title(self):
        ms, s, folder = self.make()
        with mock.patch.object(ms.actions, "process", side_effect=AssertionError("asked")):
            s._name_meeting("notes", actions.RULE_BASED_ID, {}, actions.RULE_BASED_ID)         # built-in formatter
            s._name_meeting("notes", "managed", {}, actions.RULE_BASED_ID)                      # e.g. the free note was spent
        self.assertEqual(self.meta(folder)["title"], "Meeting · Tue 14:30")

    def test_a_named_meeting_is_not_asked_about(self):
        ms, s, folder = self.make(title="Weekly sync")
        with mock.patch.object(ms.actions, "process", side_effect=AssertionError("asked")):
            s._name_meeting("notes", "managed", {}, "managed")
        self.assertEqual(self.meta(folder)["title"], "Weekly sync")

    def test_an_answer_that_is_not_a_title_is_marked_tried(self):
        ms, s, folder = self.make()
        with mock.patch.object(ms.actions, "process", lambda *a, **k: "Meeting notes"):
            s._name_meeting("notes", "managed", {}, "managed")
        m = self.meta(folder)
        self.assertEqual(m["title"], "Meeting · Tue 14:30")
        self.assertTrue(m["title_tried"])

    def test_a_failed_call_is_not_marked_so_it_is_tried_again(self):
        ms, s, folder = self.make()
        with mock.patch.object(ms.actions, "process", side_effect=actions.ActionError("offline")):
            s._name_meeting("notes", "managed", {}, "managed")                  # never raises
        m = self.meta(folder)
        self.assertEqual(m["title"], "Meeting · Tue 14:30")
        self.assertNotIn("title_tried", m)

    def test_a_slow_engine_does_not_hold_the_notes_up_and_still_names_the_meeting(self):
        ms, s, folder = self.make()
        release = threading.Event()
        finished = threading.Event()

        def slow(*a, **k):
            release.wait(5)
            return "Late title"

        real_set = meeting_store.set_auto_title

        def set_and_signal(*a, **k):
            try:
                return real_set(*a, **k)
            finally:
                finished.set()
        with mock.patch.object(ms.actions, "process", slow), \
                mock.patch.object(ms, "TITLE_WAIT_S", 0.05), \
                mock.patch.object(meeting_store, "set_auto_title", set_and_signal):
            s._name_meeting("notes", "managed", {}, "managed")
            self.assertEqual(self.meta(folder)["title"], "Meeting · Tue 14:30")       # returned without waiting
            self.assertEqual(s._meeting_title, "Meeting · Tue 14:30")                  # (the session's copy is left alone)
            release.set()
            self.assertTrue(finished.wait(5))
        self.assertEqual(self.meta(folder)["title"], "Late title")

    def test_a_later_write_of_meta_keeps_a_name_the_notes_page_or_the_ai_saved(self):
        ms, s, folder = self.make()
        meeting_store.set_auto_title(folder, "Launch plan")
        s._meeting_attendees = ""
        s._screen_people = []
        s._people_removed = []
        s._record_started_at = 0
        s._write_meta(notices=[])                      # the session still holds "Meeting · Tue 14:30"
        self.assertEqual(self.meta(folder)["title"], "Launch plan")
        meeting_store.set_title(folder, "My name")
        s._write_meta(notices=[])
        self.assertEqual(self.meta(folder)["title"], "My name")


if __name__ == "__main__":
    unittest.main()
