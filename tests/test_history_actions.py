"""history_actions: counts, clear (keeping the meeting being recorded), texts, export.

Fakes for history and meeting_store (test_core replaces the real history
module with a stand-in for the whole run, so nothing here relies on it)."""
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import history_actions as ha  # noqa: E402


class FakeHist:
    def __init__(self, n):
        self.entries = [{"text": f"t{i}"} for i in range(n)]
        self.exported = []

    def load(self):
        return list(self.entries)

    def clear(self):
        self.entries = []

    def export_csv(self, path, entries=None):
        self.exported.append(("csv", path, entries))
        return len(self.entries if entries is None else entries)

    def export_txt(self, path, entries=None):
        self.exported.append(("txt", path, entries))
        return len(self.entries if entries is None else entries)


class FakeStore:
    def __init__(self, root, names, stuck=()):
        self.root = root
        self.dirs = [os.path.join(root, n) for n in names]
        self.stuck = {os.path.join(root, n) for n in stuck}
        self.kept = None

    def list_meetings(self):
        return [{"dir": d} for d in self.dirs]

    def delete_all_meetings(self, keep=None):
        self.kept = keep
        keep_n = os.path.normcase(os.path.abspath(keep)) if keep else None
        self.dirs = [d for d in self.dirs
                     if d in self.stuck or os.path.normcase(os.path.abspath(d)) == keep_n]


class TestClear(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(lambda: os.rmdir(self.root))

    def test_counts_skip_the_active_meeting(self):
        store = FakeStore(self.root, ["m1", "m2", "live"])
        n = ha.counts(os.path.join(self.root, "live"), hist=FakeHist(3), store=store)
        self.assertEqual(n, {"dictations": 3, "meetings": 2})
        self.assertEqual(ha.describe(n), "3 dictations and 2 meetings")
        self.assertEqual(ha.describe({"dictations": 1, "meetings": 0}), "1 dictation")

    def test_everything_but_the_active_meeting(self):
        hist, store = FakeHist(3), FakeStore(self.root, ["m1", "m2", "live"])
        tracked = []
        live = os.path.join(self.root, "live")
        res = ha.clear(True, live, hist=hist, store=store, track=lambda e, p: tracked.append((e, p)))
        self.assertEqual(res, {"dictations": 3, "meetings": 2, "meetings_left": 0, "kept_active": True})
        self.assertEqual(hist.entries, [])
        self.assertEqual(store.dirs, [live])
        self.assertEqual(store.kept, live)
        self.assertEqual(tracked, [("history_cleared", {"count": 3, "meetings": 2, "from": "settings"})])
        self.assertEqual(ha.result_text(res),
                         "Deleted 3 dictations and 2 meetings. The meeting you're recording was kept.")

    def test_only_dictations(self):
        hist, store = FakeHist(2), FakeStore(self.root, ["m1"])
        res = ha.clear(False, hist=hist, store=store)
        self.assertEqual((res["dictations"], res["meetings"]), (2, 0))
        self.assertEqual(len(store.dirs), 1)
        self.assertIsNone(store.kept)
        # Recording a meeting while deleting only dictations: no "was kept" note.
        res = ha.clear(False, os.path.join(self.root, "m1"), hist=FakeHist(1), store=store)
        self.assertEqual(ha.result_text(res), "Deleted 1 dictation.")

    def test_a_locked_recording_is_reported(self):
        hist, store = FakeHist(0), FakeStore(self.root, ["m1", "m2"], stuck=["m2"])
        res = ha.clear(True, hist=hist, store=store)
        self.assertEqual((res["meetings"], res["meetings_left"]), (1, 1))
        self.assertIn("couldn't be deleted", ha.result_text(res))
        self.assertEqual(ha.result_text({"dictations": 0, "meetings": 0}), "Deleted nothing.")


class TestActiveMeetingDir(unittest.TestCase):
    def test_nothing_without_a_session(self):
        # The 1.x MeetingsWindow is retired: only the meeting session counts.
        mw = SimpleNamespace(state="recording", STATE_RECORDING="recording",
                             STATE_PROCESSING="processing", _meeting_dir="C:/m/1")
        self.assertIsNone(ha.active_meeting_dir(SimpleNamespace(meetings_win=mw)))
        self.assertIsNone(ha.active_meeting_dir(None))

    def test_new_meeting_session(self):
        s = SimpleNamespace(active_dir=lambda: "C:/m/2")
        self.assertEqual(ha.active_meeting_dir(SimpleNamespace(meeting_session=s)), "C:/m/2")
        s = SimpleNamespace(state="paused", meeting_dir="C:/m/3")
        self.assertEqual(ha.active_meeting_dir(SimpleNamespace(meeting_session=s)), "C:/m/3")
        s = SimpleNamespace(active_dir=lambda: None)
        self.assertIsNone(ha.active_meeting_dir(SimpleNamespace(meeting_session=s, meetings_win=None)))


class TestExport(unittest.TestCase):
    def test_passthrough(self):
        hist = FakeHist(2)
        self.assertEqual(ha.export_csv("a.csv", hist=hist), 2)
        self.assertEqual(ha.export_txt("a.txt", [{"text": "x"}], hist=hist), 1)
        self.assertEqual([e[0] for e in hist.exported], ["csv", "txt"])
        self.assertEqual(ha.default_export_name("csv"), "gusan_history.csv")
        self.assertEqual(ha.default_export_name("txt"), "gusan_history.txt")


if __name__ == "__main__":
    unittest.main()
