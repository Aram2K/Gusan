"""whats_new.py: what the "What's new" window lists, and when it shows (once after an update, never on a fresh
install). The founder (2026-11-09): after an update, show what changed - new features, things that work better,
fixes - simply."""

import re
import unittest

import whats_new as wn


class TestNotes(unittest.TestCase):
    def test_every_release_has_a_few_plain_items_of_a_known_kind(self):
        seen = set()
        for version, items in wn.NOTES:
            self.assertRegex(version, r"^\d+\.\d+\.\d+$")
            self.assertNotIn(version, seen)
            seen.add(version)
            self.assertTrue(2 <= len(items) <= 7, version)
            for kind, title, line in items:
                self.assertIn(kind, wn.KINDS, (version, title))
                self.assertTrue(title and len(title) <= 48, title)
                self.assertLessEqual(len(line), 110, title)
                text = (title + " " + line).lower()
                self.assertNotIn("limit", text)                     # the release-notes rules
                self.assertIsNone(re.search(r"armenia", text), title)

    def test_newest_first(self):
        versions = [wn._v(v) for v, _items in wn.NOTES]
        self.assertEqual(versions, sorted(versions, reverse=True))


class TestSections(unittest.TestCase):
    def test_the_releases_since_the_last_one_newest_first(self):
        self.assertEqual([v for v, _ in wn.sections("2.1.3", "2.1.4")], ["2.1.4"])
        self.assertEqual([v for v, _ in wn.sections("2.1.1", "2.1.4")], ["2.1.4", "2.1.3", "2.1.2"])
        self.assertEqual([v for v, _ in wn.sections("v2.1.2", "2.1.3")], ["2.1.3"])
        self.assertEqual([v for v, _ in wn.sections("", "2.1.3")], ["2.1.3"])      # where from unknown: this one
        self.assertEqual(wn.sections("2.1.4", "2.1.4"), [])
        self.assertEqual(wn.sections("", "9.9.9"), [])                             # no notes for it: nothing

    def test_never_a_release_newer_than_the_one_running(self):
        self.assertEqual([v for v, _ in wn.all_sections("2.1.3")], ["2.1.3", "2.1.2"])


class TestDue(unittest.TestCase):
    def test_once_after_an_update(self):
        cfg = {"onboarding_done": True, "whats_new_seen": "2.1.3"}
        prev, found = wn.due(cfg, "2.1.4")
        self.assertEqual((prev, [v for v, _ in found]), ("2.1.3", ["2.1.4"]))
        self.assertIsNone(wn.due(dict(cfg, whats_new_seen="2.1.4"), "2.1.4"))   # seen: never twice
        self.assertIsNone(wn.due(dict(cfg, whats_new_seen="2.1.5"), "2.1.4"))   # an older version again: nothing

    def test_a_fresh_install_shows_nothing(self):
        self.assertIsNone(wn.due({"onboarding_done": False}, "2.1.4"))
        self.assertIsNone(wn.due({}, "2.1.4"))

    def test_an_update_from_before_this_feature_uses_where_the_install_came_from(self):
        cfg = {"onboarding_done": True, "update_attempt": {"to": "v2.1.4", "from": "2.1.1"}}
        prev, found = wn.due(cfg, "2.1.4")
        self.assertEqual((prev, [v for v, _ in found]), ("2.1.1", ["2.1.4", "2.1.3", "2.1.2"]))
        prev, found = wn.due({"onboarding_done": True}, "2.1.4")                  # nothing to tell where from
        self.assertEqual((prev, [v for v, _ in found]), ("", ["2.1.4"]))

    def test_no_notes_for_this_version_shows_nothing(self):
        self.assertIsNone(wn.due({"onboarding_done": True, "whats_new_seen": "2.1.4"}, "2.1.9"))


if __name__ == "__main__":
    unittest.main()
