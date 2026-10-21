"""History is written from several threads (a meeting's worker, a dictation's
thread, the History tab): every entry must survive."""
import os
import sys
import tempfile
import threading
import time
import unittest

os.environ["TRANSCRIBE_DISABLE_KEYRING"] = "1"
os.environ["TRANSCRIBE_SKIP_MIGRATION"] = "1"

sys.modules.pop("history", None)          # tests/test_core.py stubs it for the whole session
import history


class TestHistoryWriters(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "history.json")
        self.old_path = history.HISTORY_PATH
        self.old_load = history.load
        history.HISTORY_PATH = self.path
        self.addCleanup(self._restore)

    def _restore(self):
        history.HISTORY_PATH = self.old_path
        history.load = self.old_load
        try:
            os.remove(self.path)
            os.rmdir(self.dir)
        except OSError:
            pass

    def _slow_reads(self):
        """Widen the read-modify-write window so a missing lock shows every time."""
        real = self.old_load

        def slow_load():
            out = real()
            time.sleep(0.003)
            return out
        history.load = slow_load

    def test_entries_saved_at_the_same_time_are_all_kept(self):
        self._slow_reads()
        errors = []

        def writer(n):
            try:
                for i in range(12):
                    history.save_entry("entry %d-%d" % (n, i), "en", "local")
            except Exception as e:                      # pragma: no cover
                errors.append(e)
        threads = [threading.Thread(target=writer, args=(n,)) for n in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(60)
        self.assertEqual(errors, [])
        history.load = self.old_load
        texts = {e["text"] for e in history.load()}
        self.assertEqual(len(texts), 72)


if __name__ == "__main__":
    unittest.main()
