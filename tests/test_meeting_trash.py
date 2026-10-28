"""meeting_trash: what makes "Delete" in a list undoable and safe (a move to meetings/.trash, an
Undo that moves it back, a purge that deletes for good) and the rules that keep it inside the
meetings library - never a link or junction, never anything outside. Real folders in a temp dir."""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import meeting_store as ms
import meeting_trash as mt


def _link(link, target):
    """A symlink (or, on Windows, a junction); None when this machine won't make one."""
    try:
        os.symlink(str(target), str(link), target_is_directory=True)
        return True
    except (OSError, NotImplementedError):
        pass
    if sys.platform == "win32":
        try:
            r = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                               capture_output=True, text=True)
            return r.returncode == 0
        except Exception:
            return False
    return False


class _Library(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.root = self.base / "meetings"
        self.root.mkdir()
        p = patch.object(ms.storage, "path_for", return_value=self.root)
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(self._tmp.cleanup)

    def meeting(self, name="20260926_210641", title="Q4 roadmap"):
        d = self.root / name
        d.mkdir()
        (d / "meta.json").write_text(json.dumps({"title": title, "duration_sec": 120}), encoding="utf-8")
        (d / "notes.md").write_text("## Summary\nhello", encoding="utf-8")
        (d / "transcript.txt").write_text("You: hi", encoding="utf-8")
        (d / "audio_part1.wav").write_bytes(b"RIFF" + b"\0" * 4096)
        (d / "segments.json").write_text("[]", encoding="utf-8")
        return d

    def snapshot(self, folder):
        return {p.name: p.read_bytes() for p in Path(folder).iterdir() if p.is_file()}


class TestMoveAndUndo(_Library):
    def test_a_delete_moves_the_folder_to_the_trash(self):
        d = self.meeting()
        before = self.snapshot(d)
        t = mt.move_to_trash(d)
        self.assertFalse(d.exists())
        self.assertEqual(t.name, d.name)
        self.assertEqual(self.snapshot(t.path), before)               # nothing was touched on the way
        self.assertTrue(Path(t.path).parent.parent == self.root / ".trash")
        self.assertEqual([m["id"] for m in ms.list_meetings()], [])   # the list no longer has it

    def test_the_trash_folder_is_never_a_meeting(self):
        self.meeting("20260926_210641")
        mt.move_to_trash(self.root / "20260926_210641")
        self.meeting("20260927_100000")
        self.assertEqual([m["id"] for m in ms.list_meetings()], ["20260927_100000"])

    def test_undo_puts_everything_back_exactly(self):
        d = self.meeting()
        before = self.snapshot(d)
        t = mt.move_to_trash(d)
        back = mt.restore(t)
        self.assertEqual(Path(back), d)
        self.assertEqual(self.snapshot(d), before)
        self.assertEqual([m["id"] for m in ms.list_meetings()], [d.name])
        self.assertEqual(ms.load_meta(d)["title"], "Q4 roadmap")
        self.assertFalse((self.root / ".trash" / t.token).exists())   # the box is cleared away

    def test_undo_will_not_replace_a_meeting_with_the_same_name(self):
        d = self.meeting()
        t = mt.move_to_trash(d)
        self.meeting(d.name, title="Another")                         # the name is taken now
        with self.assertRaises(mt.TrashError) as cm:
            mt.restore(t)
        self.assertEqual(cm.exception.code, "exists")
        self.assertTrue(Path(t.path).is_dir())                        # still waiting in the trash
        self.assertEqual(ms.load_meta(d)["title"], "Another")         # and the other one is untouched

    def test_two_deletes_undo_together(self):
        a, b = self.meeting("20260926_210641"), self.meeting("20260927_100000")
        ta, tb = mt.move_to_trash(a), mt.move_to_trash(b)
        self.assertEqual(len(mt.list_trash()), 2)
        mt.restore(ta)
        mt.restore(tb)
        self.assertEqual(sorted(m["id"] for m in ms.list_meetings()), ["20260926_210641", "20260927_100000"])
        self.assertEqual(mt.list_trash(), [])


class TestPurge(_Library):
    def test_purge_deletes_one_for_good(self):
        t = mt.move_to_trash(self.meeting())
        self.assertTrue(mt.purge(t))
        self.assertFalse((self.root / ".trash" / t.token).exists())
        with self.assertRaises(mt.TrashError):
            mt.restore(t)                                              # nothing left to bring back

    def test_purge_removes_a_read_only_recording(self):
        d = self.meeting()
        os.chmod(d / "audio_part1.wav", 0o444)
        t = mt.move_to_trash(d)
        self.assertTrue(mt.purge(t))

    def test_purge_all_empties_the_trash_and_removes_its_folder(self):
        for n in ("20260926_210641", "20260927_100000", "20260928_100000"):
            mt.move_to_trash(self.meeting(n))
        self.assertEqual(mt.purge_all(), 3)
        self.assertFalse((self.root / ".trash").exists())

    def test_the_start_only_purges_what_is_older_than_a_minute(self):
        now = time.time()
        old = mt.move_to_trash(self.meeting("20260926_210641"), now=now - 600)
        fresh = mt.move_to_trash(self.meeting("20260927_100000"), now=now - 5)
        self.assertEqual(mt.purge_older_than(60, now=now), 1)
        self.assertFalse(Path(old.path).exists())
        self.assertTrue(Path(fresh.path).is_dir())                    # its Undo may still be open

    def test_a_time_budget_leaves_the_rest_for_the_next_start(self):
        for n in ("20260926_210641", "20260927_100000"):
            mt.move_to_trash(self.meeting(n))
        self.assertEqual(mt.purge_all(budget_s=-1), 0)               # no time at all: nothing is started
        self.assertEqual(len(mt.list_trash()), 2)
        self.assertEqual(mt.purge_older_than(0, now=time.time() + 1), 2)

    def test_delete_everything_in_settings_empties_the_trash_too(self):
        kept = self.meeting("20260101_000000")
        self.meeting("20260102_000000")
        mt.move_to_trash(self.meeting("20260103_000000"))
        self.assertEqual(ms.delete_all_meetings(keep=kept), 1)
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ["20260101_000000"])


class TestSafety(_Library):
    def test_refuses_anything_that_is_not_a_meeting_folder_in_the_library(self):
        outside = self.base / "important"
        outside.mkdir()
        (outside / "keep.txt").write_text("mine", encoding="utf-8")
        nested = self.meeting() / "sub"
        nested.mkdir()
        for bad in (outside, self.root, self.root / ".trash", self.root / ".hidden", nested,
                    self.root / ".." / "important", str(self.root) + os.sep, "", None):
            with self.assertRaises(mt.TrashError, msg=repr(bad)):
                mt.move_to_trash(bad)
        self.assertEqual((outside / "keep.txt").read_text(encoding="utf-8"), "mine")
        self.assertTrue(nested.exists())

    def test_a_missing_folder_says_so(self):
        with self.assertRaises(mt.TrashError) as cm:
            mt.move_to_trash(self.root / "20260101_000000")
        self.assertEqual(cm.exception.code, "missing")

    def test_a_link_to_a_folder_outside_is_refused_and_the_target_is_untouched(self):
        outside = self.base / "important"
        outside.mkdir()
        (outside / "keep.txt").write_text("mine", encoding="utf-8")
        if not _link(self.root / "20260926_210641", outside):
            self.skipTest("this machine can't make a symlink or junction")
        with self.assertRaises(mt.TrashError) as cm:
            mt.move_to_trash(self.root / "20260926_210641")
        self.assertEqual(cm.exception.code, "link")
        self.assertFalse(ms.delete_meeting(self.root / "20260926_210641"))
        self.assertEqual((outside / "keep.txt").read_text(encoding="utf-8"), "mine")

    def test_a_link_to_another_meeting_is_refused_not_followed(self):
        real = self.meeting("20260101_000000")
        if not _link(self.root / "20260102_000000", real):
            self.skipTest("this machine can't make a symlink or junction")
        with self.assertRaises(mt.TrashError) as cm:
            mt.move_to_trash(self.root / "20260102_000000")
        self.assertEqual(cm.exception.code, "link")
        self.assertFalse(ms.delete_meeting(self.root / "20260102_000000"))
        self.assertTrue((real / "notes.md").is_file())

    def test_a_link_inside_a_meeting_is_not_followed_when_it_is_purged(self):
        outside = self.base / "important"
        outside.mkdir()
        (outside / "keep.txt").write_text("mine", encoding="utf-8")
        d = self.meeting()
        if not _link(d / "shortcut", outside):
            self.skipTest("this machine can't make a symlink or junction")
        t = mt.move_to_trash(d)
        self.assertTrue(mt.purge(t))
        self.assertEqual((outside / "keep.txt").read_text(encoding="utf-8"), "mine")

    def test_a_trash_folder_that_is_a_link_is_never_used_restored_from_or_emptied(self):
        """Somebody pointed meetings/.trash at another folder: nothing goes into it, comes out of it,
        and above all nothing in the folder it points to is ever deleted."""
        outside = self.base / "important"
        outside.mkdir()
        (outside / "keep.txt").write_text("mine", encoding="utf-8")
        (outside / "1700000000000-abcdef").mkdir()               # even something that looks like a trash entry
        (outside / "1700000000000-abcdef" / "x.txt").write_text("also mine", encoding="utf-8")
        if not _link(self.root / ".trash", outside):
            self.skipTest("this machine can't make a symlink or junction")
        d = self.meeting()
        with self.assertRaises(mt.TrashError) as cm:
            mt.move_to_trash(d)
        self.assertEqual(cm.exception.code, "link")
        self.assertTrue((d / "notes.md").is_file())              # the meeting stayed where it was
        self.assertEqual(mt.purge_all(), 0)
        self.assertEqual(mt.purge_older_than(max_age_s=0), 0)
        self.assertFalse(mt.purge("1700000000000-abcdef"))
        with self.assertRaises(mt.TrashError):
            mt.restore(mt.Trashed("1700000000000-abcdef", "x", "", "", 0))
        self.assertEqual(mt.list_trash(), [])
        self.assertEqual((outside / "keep.txt").read_text(encoding="utf-8"), "mine")
        self.assertEqual((outside / "1700000000000-abcdef" / "x.txt").read_text(encoding="utf-8"), "also mine")

    def test_an_undo_that_finds_the_name_taken_says_so(self):
        d = self.meeting()
        t = mt.move_to_trash(d)
        taken = FileExistsError(17, "exists")
        with patch("meeting_trash.os.path.lexists", return_value=False), \
                patch("meeting_trash.os.rename", side_effect=taken):
            with self.assertRaises(mt.TrashError) as cm:             # the name came back between the check and the move
                mt.restore(t)
        self.assertEqual(cm.exception.code, "exists")
        self.assertTrue((Path(t.path) / "notes.md").is_file())       # still in the trash, undo can be tried again

    def test_the_trash_only_takes_tokens_it_made(self):
        for bad in ("..", "../x", "123-zzzzzz", "", "20260101_000000"):
            self.assertFalse(mt.purge(bad), bad)
            with self.assertRaises(mt.TrashError):
                mt.restore(mt.Trashed(bad, "20260101_000000", "", "", 0))
        t = mt.move_to_trash(self.meeting())
        for name in ("..", "../meetings", ".hidden", "a/b", ""):
            with self.assertRaises(mt.TrashError):
                mt.restore(t._replace(name=name))                      # a name that isn't a plain folder name
        self.assertTrue(Path(t.path).is_dir())

    def test_a_file_in_use_keeps_the_meeting_where_it_is(self):
        d = self.meeting()
        busy = PermissionError(13, "The process cannot access the file because it is being used by another process")
        busy.winerror = 32
        with patch("meeting_trash.os.rename", side_effect=busy):
            with self.assertRaises(mt.TrashError) as cm:
                mt.move_to_trash(d)
        self.assertEqual(cm.exception.code, "busy")
        self.assertTrue((d / "notes.md").is_file())
        self.assertEqual(list((self.root / ".trash").iterdir()) if (self.root / ".trash").exists() else [], [])

    def test_no_permission_is_its_own_message(self):
        d = self.meeting()
        with patch("meeting_trash.os.rename", side_effect=PermissionError(13, "denied")):
            with self.assertRaises(mt.TrashError) as cm:
                mt.move_to_trash(d)
        self.assertEqual(cm.exception.code, "denied")
        self.assertTrue(d.is_dir())

    def test_the_sentences_are_plain(self):
        self.assertEqual(mt.plain("busy", "Q4 roadmap"),
                         "“Q4 roadmap” is open in another program. Close it and try again.")
        self.assertIn("isn't allowed", mt.plain("denied", "Q4"))
        self.assertIn("left alone", mt.plain("outside", "Q4"))
        self.assertIn("already gone", mt.plain("missing"))
        for code in ("busy", "denied", "outside", "link", "exists", "missing", "failed", "whatever"):
            text = mt.plain(code, "Name")
            self.assertTrue(text.endswith("."), text)
            self.assertNotIn("Traceback", text)


if __name__ == "__main__":
    unittest.main()
