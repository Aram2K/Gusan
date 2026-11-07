"""The quiet pass that names meetings saved before meetings were named (meeting_title_pass.py)."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import actions  # noqa: E402
import meeting_store  # noqa: E402
import meeting_title_pass as tp  # noqa: E402

NOTES = "## Summary\nThe team planned the launch of the new website and agreed who writes the announcement.\n"


class TestEngines(unittest.TestCase):
    def test_gusan_cloud_only_with_a_pro_token(self):
        self.assertTrue(tp.allowed_engine(actions.API_MANAGED_ID, {"_managed_token": "t"}))
        self.assertFalse(tp.allowed_engine(actions.API_MANAGED_ID, {}))                       # Free: the month's one note
        self.assertFalse(tp.allowed_engine(actions.API_MANAGED_ID, {"_managed_token": ""}))

    def test_never_in_privacy_mode_or_during_a_live_session(self):
        self.assertFalse(tp.allowed_engine(actions.API_MANAGED_ID, {"_managed_token": "t", "privacy_mode": True}))
        self.assertFalse(tp.allowed_engine(actions.API_MANAGED_ID, {"_managed_token": "t", "_live_session_id": "abc"}))

    def test_an_own_cloud_key_may(self):
        cfg = {"action_api_key": "sk-test", "action_api_provider": "openai"}
        self.assertTrue(tp.allowed_engine(actions.API_OPENAI_ID, cfg))
        self.assertFalse(tp.allowed_engine(actions.API_OPENAI_ID, {}))                        # no key saved

    def test_the_built_in_formatter_and_a_model_on_this_computer_never_do(self):
        self.assertFalse(tp.allowed_engine(actions.RULE_BASED_ID, {}))
        local = next(k for k, v in actions.ACTION_MODELS.items() if v.get("kind") == "local_llm")
        self.assertFalse(tp.allowed_engine(local, {}))


class TestPass(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.object(meeting_store, "meetings_dir", return_value=Path(self.tmp.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.cfg = {"_managed_token": "tok"}
        self.n = 0

    def meeting(self, title="Meeting · Tue 14:30", notes=NOTES, **meta):
        self.n += 1
        folder = Path(self.tmp.name) / ("2026110%d_143000" % self.n)
        folder.mkdir()
        (folder / "transcript.txt").write_text("hello", encoding="utf-8")
        if notes is not None:
            (folder / "notes.md").write_text(notes, encoding="utf-8")
        meeting_store.update_meta(folder, title=title, **meta)
        return folder

    def meta(self, folder):
        return json.loads((folder / "meta.json").read_text(encoding="utf-8"))

    def test_it_names_unnamed_meetings_with_notes_newest_first_and_leaves_the_rest(self):
        old = self.meeting()
        named = self.meeting(title="Weekly sync")
        by_user = self.meeting(title="Untitled Meeting", title_source="user")
        no_notes = self.meeting(notes=None)
        new = self.meeting(title="Live session 09:00")
        asked = []

        def process(text, mode, **kw):
            asked.append((mode, kw["model"], kw["config"]["_managed_token"]))
            return "Website launch plan"
        count = tp.run(actions.API_MANAGED_ID, self.cfg, process=process, pause=0)
        self.assertEqual(count, 2)
        self.assertEqual([self.meta(f)["title"] for f in (old, new)], ["Website launch plan"] * 2)
        self.assertEqual(self.meta(named)["title"], "Weekly sync")
        self.assertEqual(self.meta(by_user)["title"], "Untitled Meeting")
        self.assertEqual(self.meta(no_notes)["title"], "Meeting · Tue 14:30")
        self.assertEqual(asked, [("meeting_title", actions.API_MANAGED_ID, "tok")] * 2)
        self.assertEqual(self.meta(old)["title_source"], "auto")

    def test_a_second_pass_asks_about_nothing_already_named(self):
        self.meeting()
        self.assertEqual(tp.run(actions.API_MANAGED_ID, self.cfg, process=lambda *a, **k: "Website launch plan", pause=0), 1)
        with mock.patch.object(actions, "process", side_effect=AssertionError("asked again")):
            self.assertEqual(tp.run(actions.API_MANAGED_ID, self.cfg, pause=0), 0)

    def test_a_failed_call_ends_the_pass_and_marks_nothing(self):
        a, b = self.meeting(), self.meeting()
        calls = []

        def process(*args, **kw):
            calls.append(1)
            raise actions.ActionError("offline")
        self.assertEqual(tp.run(actions.API_MANAGED_ID, self.cfg, process=process, pause=0), 0)
        self.assertEqual(len(calls), 1)                                  # it did not go on to the next
        for f in (a, b):
            self.assertNotIn("title_tried", self.meta(f))

    def test_an_answer_that_is_not_a_title_marks_that_meeting_and_the_pass_goes_on(self):
        a, b = self.meeting(), self.meeting()
        answers = iter(["Meeting notes", "Hiring plan for Q4"])
        count = tp.run(actions.API_MANAGED_ID, self.cfg, process=lambda *a, **k: next(answers), pause=0)
        self.assertEqual(count, 1)
        # newest first: the second folder is asked first
        self.assertTrue(self.meta(b)["title_tried"])
        self.assertEqual(self.meta(a)["title"], "Hiring plan for Q4")

    def test_notes_too_short_to_name_a_meeting_by_are_not_sent(self):
        f = self.meeting(notes="ok")
        with mock.patch.object(actions, "process", side_effect=AssertionError("sent")):
            self.assertEqual(tp.run(actions.API_MANAGED_ID, self.cfg, pause=0), 0)
        self.assertTrue(self.meta(f)["title_tried"])

    def test_it_gives_way_the_moment_something_starts(self):
        for _ in range(3):
            self.meeting()
        busy = iter([False, True])
        calls = []

        def process(*a, **k):
            calls.append(1)
            return "Website launch plan"
        count = tp.run(actions.API_MANAGED_ID, self.cfg, should_stop=lambda: next(busy), process=process, pause=0)
        self.assertEqual((count, len(calls)), (1, 1))

    def test_one_run_names_a_limited_number(self):
        for _ in range(5):
            self.meeting()
        count = tp.run(actions.API_MANAGED_ID, self.cfg, limit=2, process=lambda *a, **k: "Website launch plan", pause=0)
        self.assertEqual(count, 2)

    def test_a_forbidden_engine_asks_nothing(self):
        self.meeting()
        with mock.patch.object(actions, "process", side_effect=AssertionError("sent")):
            self.assertEqual(tp.run(actions.API_MANAGED_ID, {}, pause=0), 0)                       # Free
            self.assertEqual(tp.run(actions.API_MANAGED_ID, dict(self.cfg, privacy_mode=True), pause=0), 0)
            self.assertEqual(tp.run(actions.RULE_BASED_ID, {}, pause=0), 0)


if __name__ == "__main__":
    unittest.main()
