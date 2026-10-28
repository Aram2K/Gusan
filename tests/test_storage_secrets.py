"""storage.write_secret on macOS: the secret goes to `security -i` on stdin, never
into the process arguments (another user of a shared Mac reads those with ps)."""
import subprocess
import unittest
from types import SimpleNamespace
from unittest import mock

import storage


def _ok(stdout=""):
    return SimpleNamespace(returncode=0, stdout=stdout, stderr="")


class TestMacWriteSecret(unittest.TestCase):
    TOKEN = '1//0gP"q\\x-secret_value'

    def _write(self, value=None, read_back=None, returncode=0):
        value = self.TOKEN if value is None else value
        calls = []

        def run(args, **kw):
            calls.append((args, kw))
            if args[:2] == ["security", "-i"]:
                return SimpleNamespace(returncode=returncode, stdout="", stderr="")
            return _ok(value if read_back is None else read_back)      # find-generic-password

        with mock.patch.object(storage.subprocess, "run", side_effect=run):
            ok = storage._mac_write_secret("calendar_google_refresh_token", value)
        return ok, calls

    def test_the_value_is_in_stdin_and_not_in_any_argument(self):
        ok, calls = self._write()
        self.assertTrue(ok)
        for args, _kw in calls:
            self.assertFalse(any(self.TOKEN in str(a) for a in args), args)
        add = next(c for c in calls if c[0] == ["security", "-i"])
        self.assertIn('-w "1//0gP\\"q\\\\x-secret_value"', add[1]["input"])
        self.assertTrue(add[1]["input"].endswith("\n"))
        self.assertEqual(add[1]["input"].count("\n"), 1)

    def test_service_and_name_are_quoted_words(self):
        _ok_, calls = self._write(value="v")
        text = next(c for c in calls if c[0] == ["security", "-i"])[1]["input"]
        self.assertEqual(text, 'add-generic-password -U -s "TranscribeApp" '
                               '-a "calendar_google_refresh_token" -w "v"\n')

    def test_a_value_with_a_line_break_is_refused_without_running_anything(self):
        with mock.patch.object(storage.subprocess, "run") as run:
            self.assertFalse(storage._mac_write_secret("n", "abc\nadd-generic-password -w x"))
            self.assertFalse(storage._mac_write_secret("n", "abc\rdef"))
            self.assertFalse(storage._mac_write_secret("bad\nname", "abc"))
        run.assert_not_called()

    def test_a_failed_command_is_not_a_success(self):
        ok, _calls = self._write(returncode=1)
        self.assertFalse(ok)

    def test_a_zero_exit_that_saved_nothing_is_not_a_success(self):
        ok, _calls = self._write(read_back="")
        self.assertFalse(ok)

    def test_deleting_carries_no_secret_and_still_works(self):
        with mock.patch.object(storage.subprocess, "run", return_value=_ok()) as run:
            self.assertTrue(storage._mac_write_secret("n", ""))
        self.assertEqual(run.call_args.args[0][:2], ["security", "delete-generic-password"])


if __name__ == "__main__":
    unittest.main()
